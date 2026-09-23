"""Agent：驱动模型与工具之间的调用循环，基于 ThreadStore 维护会话状态。"""

from __future__ import annotations

from mini_deerflow.models import FinalAnswer, Message, Model, ToolCall
from mini_deerflow.thread_store import ThreadStore
from mini_deerflow.tools import ToolRegistry


class Agent:
    """Agent 通过依赖注入接收 Model、ToolRegistry 和 ThreadStore，不依赖具体实现。"""

    def __init__(
        self,
        model: Model,
        registry: ToolRegistry,
        store: ThreadStore,
        max_steps: int = 10,
    ):
        self._model = model
        self._registry = registry
        self._store = store
        self._max_steps = max_steps

    def run(self, thread_id: str, user_input: str) -> tuple[str, list[dict]]:
        """对指定 thread_id 执行一轮 Agent 循环；成功后原子写入，失败时 Thread 不变。"""
        thread = self._store.get(thread_id)  # ThreadNotFoundError 原样传播

        # 读取历史为快照；working_messages 是本轮累计（喂给模型），pending 是待提交的本轮增量
        working_messages: list[Message] = list(thread.snapshot())
        pending: list[Message] = []

        user_message = Message(role="user", content=user_input)
        working_messages.append(user_message)
        pending.append(user_message)

        trace: list[dict] = []

        for step in range(1, self._max_steps + 1):
            decision = self._model.decide(working_messages)

            if isinstance(decision, FinalAnswer):
                assistant_message = Message(role="assistant", content=decision.content)
                pending.append(assistant_message)
                trace.append({
                    "step": step,
                    "action": "final_answer",
                    "detail": decision.content,
                })
                # 原子提交：先全部 extend，再 save
                thread.extend(pending)
                self._store.save(thread)
                return decision.content, trace

            if isinstance(decision, ToolCall):
                trace.append({
                    "step": step,
                    "action": "model_decision",
                    "detail": f"调用工具 {decision.name}，参数: {decision.arguments}",
                })

                result = self._registry.execute(decision.name, decision.arguments)
                trace.append({
                    "step": step,
                    "action": "tool_call",
                    "detail": {"name": decision.name, "args": decision.arguments, "result": result},
                })

                tool_message = Message(role="tool", content=result)
                working_messages.append(tool_message)
                pending.append(tool_message)

        raise RuntimeError(f"Agent 在 {self._max_steps} 步内未完成")