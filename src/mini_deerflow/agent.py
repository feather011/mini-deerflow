"""Agent：驱动模型与工具注册表之间的调用循环。"""

from __future__ import annotations

from mini_deerflow.models import FinalAnswer, Message, Model, ToolCall
from mini_deerflow.tools import ToolRegistry


class Agent:
    """Agent 通过依赖注入接收模型和工具注册表，不依赖具体实现。"""

    def __init__(
        self,
        model: Model,
        registry: ToolRegistry,
        max_steps: int = 10,
    ):
        self._model = model
        self._registry = registry
        self._max_steps = max_steps

    def run(self, user_input: str) -> tuple[str, list[dict]]:
        """执行 Agent 循环，返回 (最终答案, 执行轨迹)。"""
        messages: list[Message] = [Message(role="user", content=user_input)]
        trace: list[dict] = []

        for step in range(1, self._max_steps + 1):
            decision = self._model.decide(messages)

            if isinstance(decision, FinalAnswer):
                trace.append({
                    "step": step,
                    "action": "final_answer",
                    "detail": decision.content,
                })
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
                    "detail": {
                        "name": decision.name,
                        "args": decision.arguments,
                        "result": result,
                    },
                })

                messages.append(Message(role="tool", content=result))

        raise RuntimeError(f"Agent 在 {self._max_steps} 步内未完成")
