"""Agent Run REST API 的 Pydantic Schema（19C 阶段）。

请求/响应模型与 FastAPI 422 校验对齐；
不在 Schema 层 import FastAPI。
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator


# 集中配置：单个消息最大长度
MAX_MESSAGE_LENGTH = 8192


class RunStatus(str, Enum):
    """Run 执行结果状态。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class AgentRunRequest(BaseModel):
    """POST /api/threads/{thread_id}/runs 请求体。"""

    message: str = Field(
        ...,
        min_length=1,
        max_length=MAX_MESSAGE_LENGTH,
        description="用户输入；不能为空、不能为纯空白。",
    )

    @field_validator("message")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        """拒绝纯空白消息（含全空格 / 制表 / 换行的 trim 后为空）。"""
        if not value.strip():
            raise ValueError("message 不能为空或纯空白")
        return value


class AgentRunResponse(BaseModel):
    """POST /api/threads/{thread_id}/runs 响应体。"""

    run_id: str
    thread_id: str
    status: RunStatus
    answer: str | None = None
    error: str | None = None