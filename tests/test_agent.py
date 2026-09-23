"""agent.py 单元测试。"""

import unittest

from mini_deerflow.agent import Agent
from mini_deerflow.models import FakeModel, FinalAnswer, Message, ToolCall
from mini_deerflow.tools import ToolNotFoundError, create_default_registry


class AlwaysToolCallModel:
    """总是返回工具调用的模型，用于测试 max_steps 限制。"""

    def decide(self, messages: list[Message]) -> ToolCall:
        return ToolCall(name="calculator", arguments={"expression": "1 + 1"})


class UnknownToolModel:
    """返回未知工具名的模型，用于测试错误处理。"""

    def decide(self, messages: list[Message]) -> ToolCall:
        return ToolCall(name="hack", arguments={"expression": "x"})


class TestAgent(unittest.TestCase):
    """测试 Agent 循环的各种场景。"""

    def setUp(self):
        self.agent = Agent(model=FakeModel(), registry=create_default_registry())

    def test_calculation(self):
        """计算 (12 + 8) * 3 最终得到 60"""
        answer, trace = self.agent.run("计算 (12 + 8) * 3")
        self.assertIn("60", answer)

    def test_trace_has_tool_call(self):
        """执行轨迹包含一次 calculator 调用（字典参数）"""
        answer, trace = self.agent.run("计算 10 + 5")
        tool_calls = [t for t in trace if t["action"] == "tool_call"]
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0]["detail"]["name"], "calculator")
        self.assertEqual(tool_calls[0]["detail"]["args"], {"expression": "10 + 5"})

    def test_plain_text_no_tool(self):
        """普通文本不会调用工具"""
        answer, trace = self.agent.run("你好")
        tool_calls = [t for t in trace if t["action"] == "tool_call"]
        self.assertEqual(len(tool_calls), 0)
        self.assertIn("只支持计算", answer)

    def test_unknown_tool_raises(self):
        """未知工具应由 ToolRegistry 抛出 ToolNotFoundError"""
        agent = Agent(model=UnknownToolModel(), registry=create_default_registry())
        with self.assertRaises(ToolNotFoundError) as ctx:
            agent.run("任意输入")
        self.assertIn("hack", str(ctx.exception))

    def test_max_steps_limit(self):
        """不断请求工具的模型会触发 max_steps 限制"""
        agent = Agent(
            model=AlwaysToolCallModel(),
            registry=create_default_registry(),
            max_steps=3,
        )
        with self.assertRaises(RuntimeError) as ctx:
            agent.run("计算 1 + 1")
        self.assertIn("3", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
