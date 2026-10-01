"""Thread REST 路由（19B 阶段，19F 统一 OpenAPI）。

端点：
- POST /api/threads               创建线程（201，409 冲突）
- GET  /api/threads               列出线程（200，空列表为 []）
- GET  /api/threads/{thread_id}   读取详情（200，404 不存在）
- DELETE /api/threads/{thread_id} 删除（204，404 不存在）

约定：
- 同步 Store 使用 def 路由（不阻塞事件循环）。
- Service 抛领域异常；路由层映射为 HTTPException。
- 全局异常处理器统一包装为 ErrorResponse。
- 不向响应暴露 ThreadState.schema_version 等内部字段。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status

from mini_deerflow.api.dependencies import get_runtime
from mini_deerflow.api.schemas.errors import ErrorResponse
from mini_deerflow.api.schemas.threads import (
    CreateThreadRequest,
    ThreadDetail,
    ThreadListResponse,
    ThreadSummary,
    thread_to_detail,
    thread_to_summary,
)
from mini_deerflow.api.services.threads import (
    ThreadAlreadyExistsError,
    ThreadNotFoundError,
    ThreadService,
)
from mini_deerflow.runtime import RuntimeContext


router = APIRouter(tags=["threads"])


def get_thread_service(
    runtime: RuntimeContext = Depends(get_runtime),
) -> ThreadService:
    return ThreadService(runtime.store)


@router.post(
    "/threads",
    response_model=ThreadDetail,
    status_code=status.HTTP_201_CREATED,
    summary="创建 Thread",
    description="创建新线程。thread_id 可选；不指定时由服务端生成 UUID。",
    responses={
        201: {"description": "线程创建成功"},
        409: {"model": ErrorResponse, "description": "线程 ID 已存在"},
        503: {"model": ErrorResponse, "description": "Runtime 未就绪"},
    },
)
def create_thread(
    payload: CreateThreadRequest | None = None,
    service: ThreadService = Depends(get_thread_service),
) -> ThreadDetail:
    requested_id = payload.thread_id if payload else None
    try:
        thread = service.create(requested_id)
    except ThreadAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"线程已存在: {exc.thread_id}",
        ) from exc
    messages = [(m.role, m.content) for m in thread.snapshot()]
    return thread_to_detail(thread.thread_id, messages)


@router.get(
    "/threads",
    response_model=ThreadListResponse,
    summary="列出 Threads",
    description="返回全部线程的摘要列表。",
    responses={
        200: {"description": "线程列表（可能为空）"},
        503: {"model": ErrorResponse, "description": "Runtime 未就绪"},
    },
)
def list_threads(
    service: ThreadService = Depends(get_thread_service),
) -> ThreadListResponse:
    threads = service.list()
    return ThreadListResponse(
        threads=[thread_to_summary(t.thread_id) for t in threads],
    )


@router.get(
    "/threads/{thread_id}",
    response_model=ThreadDetail,
    summary="读取 Thread 详情",
    description="返回指定线程的完整消息列表。",
    responses={
        200: {"description": "线程详情"},
        404: {"model": ErrorResponse, "description": "线程不存在"},
        503: {"model": ErrorResponse, "description": "Runtime 未就绪"},
    },
)
def get_thread(
    thread_id: str,
    service: ThreadService = Depends(get_thread_service),
) -> ThreadDetail:
    try:
        thread = service.get(thread_id)
    except ThreadNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未知线程: {exc.thread_id}",
        ) from exc
    messages = [(m.role, m.content) for m in thread.snapshot()]
    return thread_to_detail(thread.thread_id, messages)


@router.delete(
    "/threads/{thread_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="删除 Thread",
    description="删除指定线程。成功返回 204 无响应体。",
    responses={
        204: {"description": "删除成功"},
        404: {"model": ErrorResponse, "description": "线程不存在"},
        503: {"model": ErrorResponse, "description": "Runtime 未就绪"},
    },
)
def delete_thread(
    thread_id: str,
    service: ThreadService = Depends(get_thread_service),
) -> Response:
    try:
        service.delete(thread_id)
    except ThreadNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未知线程: {exc.thread_id}",
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
