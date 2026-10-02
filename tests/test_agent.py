"""agent.py 单元测试。"""

import unittest

from mini_deerflow.agent import Agent
from mini_deerflow.models import FakeModel, FinalAnswer, Message, ToolCall
from mini_deerflow.thread_state import ThreadState
from mini_deerflow.thread_store import (
    InMemoryThreadStore,
    ThreadAlreadyExistsError,
    ThreadNotFoundError,
)
from mini_deerflow.tools import (
    CALCULATOR_TOOL,
    Tool,
    ToolNotFoundError,
    ToolRegistry,
    create_default_registry,
)


class AlwaysToolCallModel:
    """总是返回工具调用的模型，用于测试 max_steps 限制。"""

    def decide(self, messages: list[Message], tools=None) -> ToolCall:
        return ToolCall(name="calculator", arguments={"expression": "1 + 1"})


class UnknownToolModel:
    """返回未知工具名的模型。"""

    def decide(self, messages: list[Message], tools=None) -> ToolCall:
        return ToolCall(name="hack", arguments={"expression": "x"})


class ScriptedModel:
    """按预定决策序列返回，并记录每次收到的消息历史。"""

    def __init__(self, decisions: list[FinalAnswer | ToolCall]) -> None:
        self._decisions = list(decisions)
        self.calls: list[list[Message]] = []

    def decide(self, messages: list[Message], tools=None) -> FinalAnswer | ToolCall:
        self.calls.append(list(messages))
        return self._decisions.pop(0)


def _store_with_default() -> InMemoryThreadStore:
    """创建一个含默认 Thread 的内存 Store。"""
    store = InMemoryThreadStore()
    store.create("default")
    return store


def _agent_with_default(store=None) -> Agent:
    """创建基于默认 Thread 的 Agent。"""
    return Agent(
        model=FakeModel(),
        registry=create_default_registry(),
        store=store if store is not None else _store_with_default(),
    )


class TestAgentBasics(unittest.TestCase):
    """保留原有 Agent 循环行为测试。"""

    def test_calculation(self):
        """计算 (12 + 8) * 3 最终得到 60"""
        store = _store_with_default()
        agent = Agent(model=FakeModel(), registry=create_default_registry(), store=store)
        answer, trace = agent.run("default", "计算 (12 + 8) * 3")
        self.assertIn("60", answer)

    def test_trace_has_tool_call(self):
        """执行轨迹包含一次 calculator 调用（字典参数）"""
        store = _store_with_default()
        agent = Agent(model=FakeModel(), registry=create_default_registry(), store=store)
        answer, trace = agent.run("default", "计算 10 + 5")
        tool_calls = [t for t in trace if t["action"] == "tool_call"]
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0]["detail"]["name"], "calculator")
        self.assertEqual(tool_calls[0]["detail"]["args"], {"expression": "10 + 5"})

    def test_plain_text_no_tool(self):
        """普通文本不会调用工具"""
        agent = _agent_with_default()
        answer, trace = agent.run("default", "你好")
        tool_calls = [t for t in trace if t["action"] == "tool_call"]
        self.assertEqual(len(tool_calls), 0)
        self.assertIn("只支持计算", answer)

    def test_unknown_tool_raises(self):
        """未知工具应抛出 ToolNotFoundError"""
        agent = Agent(
            model=UnknownToolModel(),
            registry=create_default_registry(),
            store=_store_with_default(),
        )
        with self.assertRaises(ToolNotFoundError):
            agent.run("default", "任意输入")

    def test_max_steps_limit(self):
        """不断请求工具的模型会触发 max_steps 限制"""
        agent = Agent(
            model=AlwaysToolCallModel(),
            registry=create_default_registry(),
            store=_store_with_default(),
            max_steps=3,
        )
        with self.assertRaises(RuntimeError) as ctx:
            agent.run("default", "计算 1 + 1")
        self.assertIn("3", str(ctx.exception))


class TestAgentThreadStoreIntegration(unittest.TestCase):
    """5C 阶段：Agent 与 ThreadStore 的集成行为。"""

    def test_thread_not_found_propagates(self):
        """Thread 不存在时 ThreadNotFoundError 原样传播"""
        store = _store_with_default()
        agent = Agent(model=FakeModel(), registry=create_default_registry(), store=store)
        with self.assertRaises(ThreadNotFoundError):
            agent.run("missing", "你好")

    def test_messages_saved_on_success(self):
        """成功运行后 Thread 只保存 user 与最终 assistant。

        中间工具往返（assistant tool_use + tool 结果）只存在于本轮
        working_messages / Trace / SSE，**不进 ThreadState**——否则会留下
        没有配对 tool_use 的孤儿 tool_result，污染下一轮模型请求。
        """
        store = _store_with_default()
        agent = Agent(model=FakeModel(), registry=create_default_registry(), store=store)
        agent.run("default", "计算 1 + 1")
        snapshot = store.get("default").snapshot()
        roles = [m.role for m in snapshot]
        self.assertEqual(roles, ["user", "assistant"])

    def test_message_order(self):
        """消息顺序应严格按 user→assistant（工具结果只进 Trace）"""
        store = _store_with_default()
        agent = Agent(model=FakeModel(), registry=create_default_registry(), store=store)
        answer, trace = agent.run("default", "计算 1 + 1")
        snapshot = store.get("default").snapshot()
        self.assertEqual(snapshot[0].content, "计算 1 + 1")
        self.assertEqual(snapshot[1].role, "assistant")
        self.assertEqual(snapshot[1].content, "计算结果: 2")
        # 工具结果没有丢：它在 Trace 里
        self.assertEqual(answer, "计算结果: 2")
        tool_entries = [t for t in trace if t.get("action") == "tool_call"]
        self.assertEqual(len(tool_entries), 1)
        self.assertEqual(tool_entries[0]["detail"]["result"], "2")

    def test_second_run_reads_first_history(self):
        """第二次运行应能读取第一次的历史"""
        store = _store_with_default()
        agent = Agent(model=FakeModel(), registry=create_default_registry(), store=store)
        agent.run("default", "计算 1 + 1")
        agent.run("default", "你好")
        snapshot = store.get("default").snapshot()
        roles = [m.role for m in snapshot]
        self.assertEqual(roles, ["user", "assistant", "user", "assistant"])
        self.assertEqual(snapshot[0].content, "计算 1 + 1")
        # 工具结果不再落库，第二次 user 输入的下标由 3 前移到 2
        self.assertEqual(snapshot[2].content, "你好")

    def test_two_threads_isolated(self):
        """两个 Thread 的历史互相隔离"""
        store = InMemoryThreadStore()
        store.create("a")
        store.create("b")
        agent = Agent(model=FakeModel(), registry=create_default_registry(), store=store)
        agent.run("a", "计算 1 + 1")
        agent.run("b", "计算 2 + 3")
        a_msgs = store.get("a").snapshot()
        b_msgs = store.get("b").snapshot()
        self.assertEqual(a_msgs[0].content, "计算 1 + 1")
        self.assertEqual(b_msgs[0].content, "计算 2 + 3")
        self.assertEqual(len(a_msgs), 2)
        self.assertEqual(len(b_msgs), 2)

    def test_unknown_tool_no_partial_write(self):
        """未知工具时 Thread 不改变"""
        store = _store_with_default()
        agent = Agent(
            model=UnknownToolModel(),
            registry=create_default_registry(),
            store=store,
        )
        with self.assertRaises(ToolNotFoundError):
            agent.run("default", "任意输入")
        self.assertEqual(store.get("default").message_count, 0)

    def test_tool_execution_failure_no_partial_write(self):
        """工具执行失败时 Thread 不改变"""
        registry = ToolRegistry()
        registry.register(Tool(
            name="boom",
            description="总是失败的工具",
            parameters={"type": "object"},
            handler=lambda args: (_ for _ in ()).throw(ValueError("boom")),
        ))

        class BoomModel:
            def decide(self, messages, tools=None):
                return ToolCall(name="boom", arguments={})

        store = _store_with_default()
        agent = Agent(model=BoomModel(), registry=registry, store=store)
        with self.assertRaises(Exception):
            agent.run("default", "触发失败")
        self.assertEqual(store.get("default").message_count, 0)

    def test_max_steps_no_partial_write(self):
        """max_steps 时 Thread 不改变"""
        store = _store_with_default()
        agent = Agent(
            model=AlwaysToolCallModel(),
            registry=create_default_registry(),
            store=store,
            max_steps=2,
        )
        with self.assertRaises(RuntimeError):
            agent.run("default", "计算 1 + 1")
        self.assertEqual(store.get("default").message_count, 0)

    def test_does_not_auto_create_thread(self):
        """Agent 不应自动创建 Thread"""
        store = InMemoryThreadStore()  # 没有任何 Thread
        agent = Agent(model=FakeModel(), registry=create_default_registry(), store=store)
        with self.assertRaises(ThreadNotFoundError):
            agent.run("anything", "你好")
        # Store 仍为空
        self.assertEqual(len(store.list_threads()), 0)

    def test_scripted_model_sees_prior_history(self):
        """ScriptedModel 应能看到第一轮累积的全部消息"""
        store = _store_with_default()
        scripted = ScriptedModel([
            ToolCall(name="calculator", arguments={"expression": "2 + 3"}),
            FinalAnswer(content="5"),
        ])
        agent = Agent(model=scripted, registry=create_default_registry(), store=store)
        agent.run("default", "计算 2 + 3")

        # 第二次：模型应看到 上一轮的 user/assistant + 本轮 user
        # （工具往返已随本轮结束丢弃，不会作为孤立 tool_result 传下去）
        second_scripted = ScriptedModel([FinalAnswer(content="收到历史了")])
        agent2 = Agent(model=second_scripted, registry=create_default_registry(), store=store)
        agent2.run("default", "再来一次")
        last_call = second_scripted.calls[0]
        roles = [m.role for m in last_call]
        self.assertEqual(roles, ["user", "assistant", "user"])


if __name__ == "__main__":
    unittest.main()