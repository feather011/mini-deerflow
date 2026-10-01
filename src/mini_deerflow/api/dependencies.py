"""FastAPI 依赖注入（19A 阶段，19F 扩展）。

提供统一的 RuntimeContext 和 Request ID 获取方式。
"""
from __future__ import annotations

from fastapi import HTTPException, Request, status

from mini_deerflow.runtime import RuntimeContext


def get_runtime(request: Request) -> RuntimeContext:
    """从 app.state.runtime 读取 RuntimeContext。

    Runtime 不存在（lifespan 未启动或启动失败）时返回 503。
    """
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Runtime not initialized",
        )
    return runtime


def get_request_id(request: Request) -> str:
    """从 request.state 读取 Request ID（由 RequestIdMiddleware 写入）。"""
    return getattr(request.state, "request_id", "unknown")
