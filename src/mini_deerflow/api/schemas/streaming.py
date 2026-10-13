"""SSE RunEvent Schema（19D 阶段）。

定义流式事件模型。所有事件走同一形状，event_type 区分语义。
绝不暴露 prompt 全文、模型凭据、完整工具参数、Traceback。
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


# 终态事件：每个 run 只能有其中一个
TERMINAL_EVENT_TYPES = frozenset(
    {"run.completed", "run.failed", "run.timed_out", "run.cancelled"}
)


class RunEventType(str, Enum):
    """RunEvent 事件类型枚举。"""

    RUN_STARTED = "run.started"
    MODEL_STARTED = "model.started"
    MODEL_COMPLETED = "model.completed"
    MODEL_FAILED = "model.failed"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    RUN_RESULT = "run.result"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_TIMED_OUT = "run.timed_out"
    RUN_CANCELLED = "run.cancelled"
    HEARTBEAT = "heartbeat"


class RunEvent(BaseModel):
    """单条 RunEvent；与 SSE 一一对应。"""

    run_id: str
    thread_id: str
    event_type: RunEventType
    sequence: int = Field(ge=0)
    timestamp: float = Field(default_factory=lambda: time.time())

    # 终态事件携带；其余事件为 None
    answer: str | None = None
    error: str | None = None

    # 可选 metadata；只放确定安全的字段（如 attempt）
    attempt: int | None = None
    operation: str | None = None
    tool_name: str | None = None
    duration_seconds: float | None = None

    # run.result 事件携带 project_execute 的结构化结果（JSON 字符串形式）
    tool_result: str | None = None


def encode_sse(event: RunEvent) -> str:
    """单条 RunEvent → SSE 消息块。

    格式：
        id: <sequence>
        event: <event_type>
        data: <JSON>

        （空行结尾）
    """
    # Pydantic 已过滤敏感字段；这里再 dump 一次确保只暴露对外字段
    payload = event.model_dump(exclude_none=True)
    data = _json_dumps(payload)
    return f"id: {event.sequence}\nevent: {event.event_type.value}\ndata: {data}\n\n"


def encode_comment(text: str) -> str:
    """SSE 注释行；用于心跳或 keepalive，浏览器不解析。"""
    return f": {text}\n\n"


def _json_dumps(obj: object) -> str:
    """标准 JSON dump；ensure_ascii=False 保留 Unicode。"""
    import json

    return json.dumps(obj, ensure_ascii=False)


def is_terminal(event_type: RunEventType) -> bool:
    """判断事件是否为终态事件。"""
    return event_type.value in TERMINAL_EVENT_TYPES


def terminal_status_from_exception(exc: Exception) -> RunEventType:
    """根据异常类型推断终态事件类型。

    TimeoutError → run.timed_out
    AgentRunCancelledError → run.cancelled
    其余 → run.failed
    """
    from mini_deerflow.api.services.agent_runs import AgentRunCancelledError

    if isinstance(exc, TimeoutError):
        return RunEventType.RUN_TIMED_OUT
    if isinstance(exc, AgentRunCancelledError):
        return RunEventType.RUN_CANCELLED
    return RunEventType.RUN_FAILED


# 类型别名供外部引用
RunEventStatus = Literal[
    "succeeded",
    "failed",
    "timed_out",
    "cancelled",
]


def safe_failure_message(exc: Exception) -> str:
    """生成对客户端安全的失败文案。

    策略：固定短语 + 类型名，绝不暴露原始 message / Traceback / 凭据。
    """
    return f"Run 失败 ({exc.__class__.__name__})"