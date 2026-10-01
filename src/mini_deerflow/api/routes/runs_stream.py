"""SSE Run Stream 路由（19D 阶段，19F 统一 OpenAPI）。

POST /api/threads/{thread_id}/runs/stream

返回 text/event-stream。
使用说明：
- 使用 POST 方法（EventSource 不支持发送 body）。
- Content-Type 为 text/event-stream。
- 需在浏览器/客户端使用 fetch + ReadableStream 解析。
- 当前不支持 token-level streaming。
- 客户端断开时 Server 端停止 publish，但不取消 Agent。
"""
from __future__ import annotations

import logging
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from mini_deerflow.api.dependencies import get_runtime
from mini_deerflow.api.schemas.errors import ErrorResponse
from mini_deerflow.api.schemas.runs import AgentRunRequest
from mini_deerflow.api.schemas.streaming import (
    RunEvent,
    encode_comment,
    encode_sse,
)
from mini_deerflow.api.services.run_stream import (
    StreamThreadBusyError,
    StreamThreadNotFoundError,
    StreamValidationError,
)
from mini_deerflow.runtime import RuntimeContext
from mini_deerflow.thread_run_coordinator import ThreadRunConflictError


_LOGGER = logging.getLogger(__name__)

router = APIRouter(tags=["streaming"])


@router.post(
    "/threads/{thread_id}/runs/stream",
    summary="流式执行 Agent Run（SSE）",
    description=(
        "在指定 Thread 上启动一次流式 Agent Run，"
        "以 Server-Sent Events 推送 run.started / model.* / tool.* / "
        "run.completed | run.failed | run.timed_out | run.cancelled 事件。\n\n"
        "**注意**：\n"
        "- 使用 POST 方法（EventSource 不支持发送 body）\n"
        "- Content-Type 为 text/event-stream\n"
        "- 需使用 fetch + ReadableStream 解析，不支持 EventSource\n"
        "- 当前不支持 token streaming\n"
        "- 当前不支持断线重连\n"
        "- 客户端断开时 Server 停止 publish，但不取消 Agent\n"
        "- 响应头 X-Run-ID 携带本次 Run ID"
    ),
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "SSE 事件流",
        },
        404: {"model": ErrorResponse, "description": "线程不存在"},
        409: {"model": ErrorResponse, "description": "线程已有 Run 进行中"},
        422: {"model": ErrorResponse, "description": "请求参数校验失败"},
        503: {"model": ErrorResponse, "description": "Stream 服务未就绪"},
    },
)
async def stream_agent_run(
    thread_id: str,
    payload: AgentRunRequest,
    runtime: RuntimeContext = Depends(get_runtime),
) -> StreamingResponse:
    manager = runtime.run_stream_manager
    if manager is None:
        raise HTTPException(
            status_code=503,
            detail="Stream 服务未初始化",
        )

    try:
        handle = manager.submit(thread_id, payload.message)
    except StreamValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from exc
    except StreamThreadNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"未知线程: {exc}",
        ) from exc
    except StreamThreadBusyError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"线程 {exc.thread_id} 正在运行",
        ) from exc
    except ThreadRunConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc

    headers = {
        "Cache-Control": "no-cache",
        "X-Run-ID": handle.run_id,
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }

    return StreamingResponse(
        _iter_sse(handle, runtime),
        media_type="text/event-stream",
        headers=headers,
    )


async def _iter_sse(handle, runtime: RuntimeContext) -> AsyncIterator[str]:
    q = handle.queue
    try:
        while True:
            try:
                item = await _async_queue_get(q, timeout=0.5)
            except _AsyncTimeout:
                yield encode_comment("keepalive")
                continue
            if item is None:
                return
            if isinstance(item, RunEvent):
                yield encode_sse(item)
                if _is_terminal(item):
                    return
    finally:
        if runtime.run_event_broker is not None:
            runtime.run_event_broker.unsubscribe(handle.run_id)


def _is_terminal(event: RunEvent) -> bool:
    from mini_deerflow.api.schemas.streaming import TERMINAL_EVENT_TYPES
    return event.event_type.value in TERMINAL_EVENT_TYPES


class _AsyncTimeout(Exception):
    pass


async def _async_queue_get(q, timeout: float):
    import asyncio
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _queue_get_with_timeout, q, timeout
    )


def _queue_get_with_timeout(q, timeout: float):
    import queue as _q
    try:
        return q.get(timeout=timeout)
    except _q.Empty:
        raise _AsyncTimeout()
