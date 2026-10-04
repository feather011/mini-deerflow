"""Phase 24 各工具与 Trace 发射的共享小工具。

抽出来的原因是**重复**：在此之前 `context_tools.py` 与 `history_tools.py`
各写一份 `_ok`/`_err`，`context_assembler.py` 与 `context_compactor.py`
各写一份 `_emit`，其中 `_err` 两版的 `kind` 还各按各的理解用——一个当键名、
一个当值，调用方就要写两套解析。合并后行为与签名一致，调用方只写一套。

本模块只做「拼响应 / 读上下文字段 / 发一条 Trace」三件事，不含业务判断。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from mini_deerflow.middleware import TraceEvent, TraceSink

__all__ = [
    "context_attr",
    "emit_event",
    "err_response",
    "ok_response",
]


def ok_response(**payload: Any) -> str:
    """成功响应。`ok: True` 在最前，便于一眼扫过。"""
    return json.dumps({"ok": True, **payload}, ensure_ascii=False)


def err_response(action: str, error: str, *, kind: str = "error") -> str:
    """失败响应。

    `error` 恒为文案、`kind` 恒为类型，**两者是并列的两个键**：
    把 kind 当键名会让调用方一会儿读 `result["error"]`、
    一会儿读 `result["usage_error"]`。

    - `usage_error`：参数不合法，模型改一下就能重试
    - `error`：领域错误，见文案
    - `index_error`：索引落后且补齐失败
    """
    return json.dumps(
        {"ok": False, "action": action, "error": error, "kind": kind},
        ensure_ascii=False,
    )


def context_attr(context: object, name: str) -> str | None:
    """从 `ExecutionContext` 取一个非空字符串字段，取不到返回 None。

    工具拿 `thread_id` / `run_id` 的唯一入口：字段名由调用方给，
    因为这两个工具读的是同一个上下文里的不同字段。
    """
    value = getattr(context, name, None) if context is not None else None
    return value if isinstance(value, str) and value else None


def emit_event(
    sink: TraceSink | None,
    *,
    name: str,
    run_id: str,
    thread_id: str,
    operation: str,
    status: str,
    metadata: dict[str, object] | None = None,
    error_type: str | None = None,
    start: float | None = None,
    parent_id: str | None = None,
    task_id: str | None = None,
) -> None:
    """发一条 Trace 事件；`sink` 为 None 时静默跳过（测试与无追踪场景）。

    `start` 省略时用当前时刻，因此「已完成」类事件只传一次时间即可；
    需要跨越一段耗时的事件（压缩、索引补齐）由调用方传入起始时刻。
    """
    if sink is None:
        return
    now = time.monotonic()
    sink.emit(
        TraceEvent(
            event_id=uuid.uuid4().hex,
            parent_id=parent_id,
            run_id=run_id,
            thread_id=thread_id,
            task_id=task_id,
            operation=operation,
            name=name,
            start=start if start is not None else now,
            end=now,
            status=status,
            error_type=error_type,
            metadata=metadata or {},
        )
    )
