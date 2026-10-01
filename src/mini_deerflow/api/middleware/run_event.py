"""RunEventMiddleware（19D 阶段）。

把 ExecutionContext 事件翻译成 RunEvent 并发布到 Broker：
- AGENT_RUN：run.started / run.completed / run.failed（仅 started/completed；终态失败由 RunStreamManager 统一发）
- MODEL_CALL：model.started / model.completed / model.failed
- TOOL_CALL：tool.started / tool.completed / tool.failed

挂载点：
- 必须挂在 Agent 的 pipeline（看到 MODEL/TOOL）
- 也必须挂在 IntegratedAgent 的 pipeline（看到 AGENT_RUN 生命周期）
- 同一实例共享 thread-local 栈，跨 pipeline 关联 run_id
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from mini_deerflow.api.schemas.streaming import RunEvent, RunEventType
from mini_deerflow.api.services.event_broker import RunEventBroker
from mini_deerflow.middleware import ExecutionContext, Middleware, OperationType


_LOGGER = logging.getLogger(__name__)


class RunEventMiddleware(Middleware[Any]):
    """把 ExecutionContext 转换为 RunEvent 发布到 broker。

    线程局部栈关联嵌套操作（AGENT_RUN → MODEL_CALL/TOOL_CALL）。
    """

    def __init__(self, broker: RunEventBroker) -> None:
        self._broker = broker
        self._local = threading.local()

    # ---------------------------------------------------------------- stack

    def _stack(self) -> list[tuple[str, str]]:
        if not hasattr(self._local, "stack"):
            self._local.stack = []
        return self._local.stack

    def _top(self) -> tuple[str, str] | None:
        s = self._stack()
        return s[-1] if s else None

    # ---------------------------------------------------------------- publish helper

    def _publish(
        self,
        run_id: str,
        thread_id: str,
        event_type: RunEventType,
        **fields: Any,
    ) -> bool:
        seq = self._broker.next_sequence(run_id)
        event = RunEvent(
            run_id=run_id,
            thread_id=thread_id,
            event_type=event_type,
            sequence=seq,
            **fields,
        )
        return self._broker.publish(run_id, event)

    # ---------------------------------------------------------------- middleware hooks

    def before(self, ctx: ExecutionContext) -> None:
        op = ctx.operation
        if op == OperationType.AGENT_RUN:
            self._stack().append((ctx.run_id, ctx.thread_id))
            self._publish(
                ctx.run_id,
                ctx.thread_id,
                RunEventType.RUN_STARTED,
                operation="agent",
            )
            return

        top = self._top()
        if top is None:
            return
        run_id, thread_id = top

        if op == OperationType.MODEL_CALL:
            self._publish(
                run_id,
                thread_id,
                RunEventType.MODEL_STARTED,
                operation="model",
            )
        elif op == OperationType.TOOL_CALL:
            self._publish(
                run_id,
                thread_id,
                RunEventType.TOOL_STARTED,
                operation="tool",
                tool_name=ctx.operation_name,
            )

    def success(self, ctx: ExecutionContext, result: Any) -> Any:
        op = ctx.operation
        if op == OperationType.AGENT_RUN:
            # AGENT_RUN 终态由 RunStreamManager 统一发；不在此处重复
            return result

        top = self._top()
        if top is None:
            return result
        run_id, thread_id = top

        if op == OperationType.MODEL_CALL:
            self._publish(
                run_id,
                thread_id,
                RunEventType.MODEL_COMPLETED,
                operation="model",
            )
        elif op == OperationType.TOOL_CALL:
            self._publish(
                run_id,
                thread_id,
                RunEventType.TOOL_COMPLETED,
                operation="tool",
                tool_name=ctx.operation_name,
            )
        return result

    def error(self, ctx: ExecutionContext, exc: Exception) -> Exception:
        op = ctx.operation
        if op == OperationType.AGENT_RUN:
            # 终态失败由 RunStreamManager 统一发；不在此处发
            return exc

        top = self._top()
        if top is None:
            return exc
        run_id, thread_id = top

        if op == OperationType.MODEL_CALL:
            self._publish(
                run_id,
                thread_id,
                RunEventType.MODEL_FAILED,
                operation="model",
                error=f"Model 调用失败 ({exc.__class__.__name__})",
            )
        elif op == OperationType.TOOL_CALL:
            self._publish(
                run_id,
                thread_id,
                RunEventType.TOOL_FAILED,
                operation="tool",
                tool_name=ctx.operation_name,
                error=f"Tool 调用失败 ({exc.__class__.__name__})",
            )
        return exc

    def finally_(self, ctx: ExecutionContext) -> None:
        if ctx.operation == OperationType.AGENT_RUN:
            s = self._stack()
            if s:
                s.pop()
            # 序列号在 RunStreamManager 确认终止后回收（避免误清理）
