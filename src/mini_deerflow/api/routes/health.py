"""健康检查与就绪检查路由（19A 阶段，19F 统一 OpenAPI）。

- GET /health：进程存活检查（HTTP 200）。不调用模型/数据库/Docker。
- GET /ready：lifespan 是否已成功创建 Runtime。
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel

from mini_deerflow.api.dependencies import get_runtime
from mini_deerflow.api.schemas.errors import ErrorResponse


router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """GET /health 响应。"""
    status: Literal["ok"]
    service: str


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="进程存活检查",
    description="返回 200 表示进程存活。不依赖 Runtime 或任何外部资源。",
    responses={
        200: {"description": "进程存活"},
    },
)
def get_health() -> HealthResponse:
    return HealthResponse(status="ok", service="mini-deerflow")


class ReadyResponse(BaseModel):
    """GET /ready 响应。"""
    status: Literal["ready"]
    runtime: bool


@router.get(
    "/ready",
    response_model=ReadyResponse,
    summary="Runtime 就绪检查",
    description="返回 200 表示 Runtime 已初始化。503 表示 lifespan 未成功启动。",
    responses={
        503: {
            "model": ErrorResponse,
            "description": "Runtime 未就绪",
        },
    },
    dependencies=[Depends(get_runtime)],
)
def get_ready() -> ReadyResponse:
    return ReadyResponse(status="ready", runtime=True)
