"""RunEventMiddleware（19D 阶段）。

把 ExecutionContext 事件翻译成 RunEvent 并发布到 Broker：
- AGENT_RUN：run.started / run.completed / run.failed（仅 started/completed；终态失败由 RunStreamManager 统一发）
- MODEL_CALL：model.started / model.completed / model.failed
- TOOL_CALL：tool.started / tool.completed / tool.failed
- project_execute 工具成功：额外发出 run.result（携带 changes/verification 等结构化结果）

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
from mini_deerflow.thread_store import ThreadStore


_LOGGER = logging.getLogger(__name__)


class RunEventMiddleware(Middleware[Any]):
    """把 ExecutionContext 转换为 RunEvent 发布到 broker。

    线程局部栈关联嵌套操作（AGENT_RUN → MODEL_CALL/TOOL_CALL）。
    26：额外将 project_execute 的 run.result 持久化到 ThreadState。
    """

    def __init__(self, broker: RunEventBroker, thread_store: ThreadStore | None = None) -> None:
        self._broker = broker
        self._thread_store = thread_store
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
            # 25C: 发布 token 流式事件（token_accumulator 在 Agent.run 中累积）
            if ctx.token_accumulator:
                for text in ctx.token_accumulator:
                    self._publish(
                        run_id,
                        thread_id,
                        RunEventType.RUN_MODEL_TOKEN,
                        operation="model",
                        delta=text,
                    )
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
            # 25：project_execute 成功时发出 run.result（携带 changes/verification）
            # 只在返回字符串且形似 JSON 时发出；其余工具直接忽略
            if ctx.operation_name == "project_execute" and isinstance(result, str) and result.strip().startswith("{"):
                self._publish(
                    run_id,
                    thread_id,
                    RunEventType.RUN_RESULT,
                    tool_result=result,
                )
                # 26：持久化到 ThreadState（页面刷新后可恢复 diff 展示）
                self._persist_run_result(thread_id, result)
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

    # ---------------------------------------------------------------- persist

    def _persist_run_result(self, thread_id: str, tool_result: str) -> None:
        """将 project_execute 结果持久化到 ThreadState。

        失败不影响主流程：只记录日志，不抛异常。
        """
        if self._thread_store is None:
            return
        try:
            thread = self._thread_store.get(thread_id)
            thread.last_run_result = tool_result
            self._thread_store.save(thread)
        except Exception:
            _LOGGER.exception("Failed to persist run_result for thread=%s", thread_id)
