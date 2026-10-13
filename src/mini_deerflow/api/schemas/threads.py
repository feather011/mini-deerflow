"""Thread REST API Pydantic Schema (19B 阶段).

设计原则：
- 公开契约只暴露稳定字段；ThreadState 内部的 schema_version 不外泄。
- 响应序列化复用 Message/ThreadState 的 to_dict()，避免手工复制字段。
- 请求体与响应体分离，便于在升级时独立演进。
- 不依赖 mini_deerflow.models（避免其潜在副作用被传播到 API 子包）。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


MessageRole = Literal["system", "user", "assistant", "tool"]


class MessageResponse(BaseModel):
    """对话消息的对外视图。"""

    role: MessageRole
    content: str


class ThreadSummary(BaseModel):
    """列表项：仅包含 thread_id，不暴露消息内容或内部版本。"""

    thread_id: str


class ThreadDetail(BaseModel):
    """详情：thread_id + 完整消息列表。"""

    thread_id: str
    project_id: str | None = None  # 如果属于项目，则为项目 ID
    messages: list[MessageResponse] = Field(default_factory=list)


class ThreadListResponse(BaseModel):
    """GET /api/threads 的响应包装。"""

    threads: list[ThreadSummary] = Field(default_factory=list)


class CreateThreadRequest(BaseModel):
    """POST /api/threads 请求体。thread_id 可选；缺省由服务端生成 UUID4 风格的 ID。

    project_id 可选；指定时创建项目内会话。
    """

    thread_id: str | None = Field(default=None)
    project_id: str | None = Field(default=None)


def message_to_response(role: str, content: str) -> MessageResponse:
    """由 Message 的 role/content 字段构造响应对象；不耦合 Message 类型。"""
    return MessageResponse(role=role, content=content)


def thread_to_summary(thread_id: str) -> ThreadSummary:
    """由已知 ID 构造摘要；不读取 Store，避免在 Schema 层产生副作用。"""
    return ThreadSummary(thread_id=thread_id)


def thread_to_detail(
    thread_id: str,
    messages: list[tuple[str, str]],
    project_id: str | None = None,
) -> ThreadDetail:
    """由 ID + 消息字段列表构造详情。

    入参 messages 是 (role, content) 元组列表，避免 Schema 层依赖 Message 类。
    """
    return ThreadDetail(
        thread_id=thread_id,
        project_id=project_id,
        messages=[message_to_response(role, content) for role, content in messages],
    )
