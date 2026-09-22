"""Agent：驱动模型与工具之间的调用循环。"""

from __future__ import annotations

from mini_deerflow.models import FinalAnswer, Message, Model, ToolCall


class Agent:
    """Agent 通过依赖注入接收模型和工具注册表，不依赖具体实现。"""

    def __init__(
        self,
        model: Model,
        tools: dict[str, callable],
        max_steps: int = 10,
    ):
        self._model = model
        self._tools = tools
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
                    "detail": f"调用工具 {decision.name}，参数: {decision.args}",
                })

                tool_fn = self._tools.get(decision.name)
                if tool_fn is None:
                    raise ValueError(f"未知工具: {decision.name}")

                result = tool_fn(decision.args)
                trace.append({
                    "step": step,
                    "action": "tool_call",
                    "detail": {"name": decision.name, "args": decision.args, "result": result},
                })

                messages.append(Message(role="tool", content=result))

        raise RuntimeError(f"Agent 在 {self._max_steps} 步内未完成")
