"""Agent Run 路由（19C 阶段，19F 统一 OpenAPI）。

POST /api/threads/{thread_id}/runs

依赖：RuntimeContext.integrated_agent + ThreadStore + ThreadRunCoordinator

注意：SSE 流使用 /threads/{thread_id}/runs/stream。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from mini_deerflow.api.dependencies import get_runtime
from mini_deerflow.api.schemas.errors import ErrorResponse
from mini_deerflow.api.schemas.runs import (
    AgentRunRequest,
    AgentRunResponse,
    RunStatus,
)
from mini_deerflow.api.services.agent_runs import (
    AgentBudgetExceededError,
    AgentRunCancelledError,
    AgentRunFailedError,
    AgentRunService,
    AgentRunTimeoutError,
    AgentRunValidationError,
    AgentThreadNotFoundError,
)
from mini_deerflow.runtime import RuntimeContext
from mini_deerflow.thread_run_coordinator import ThreadRunConflictError


router = APIRouter(tags=["runs"])


def get_agent_run_service(
    runtime: RuntimeContext = Depends(get_runtime),
) -> AgentRunService:
    return AgentRunService(
        runner=runtime.integrated_agent.run,
        store=runtime.store,
        coordinator=runtime.thread_run_coordinator,
    )


@router.post(
    "/threads/{thread_id}/runs",
    response_model=AgentRunResponse,
    status_code=status.HTTP_200_OK,
    summary="同步执行 Agent Run",
    description=(
        "在指定 Thread 上同步执行一次 Agent Run，"
        "等待完成后返回结果。\n\n"
        "注意：如需流式响应，请使用 POST /threads/{thread_id}/runs/stream。"
    ),
    responses={
        200: {"description": "Run 执行完成，返回 answer 或 error"},
        404: {"model": ErrorResponse, "description": "线程不存在"},
        409: {"model": ErrorResponse, "description": "线程已有 Run 进行中"},
        422: {"model": ErrorResponse, "description": "请求参数校验失败"},
        429: {"model": ErrorResponse, "description": "Budget 超限"},
        500: {"model": ErrorResponse, "description": "Run 执行失败"},
        503: {"model": ErrorResponse, "description": "Runtime 未就绪或 Run 已取消"},
        504: {"model": ErrorResponse, "description": "Run 超时"},
    },
)
def create_agent_run(
    thread_id: str,
    payload: AgentRunRequest,
    service: AgentRunService = Depends(get_agent_run_service),
) -> AgentRunResponse:
    try:
        result = service.run(thread_id, payload.message)
    except AgentThreadNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未知线程: {exc.thread_id}",
        ) from exc
    except AgentRunValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except ThreadRunConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except AgentBudgetExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
            headers={"Retry-After": "0"},
        ) from exc
    except AgentRunTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=str(exc),
        ) from exc
    except AgentRunCancelledError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except AgentRunFailedError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc.message),
        ) from exc

    return AgentRunResponse(
        run_id=result.run_id,
        thread_id=result.thread_id,
        status=RunStatus(result.status),
        answer=result.answer,
        error=result.error,
    )
