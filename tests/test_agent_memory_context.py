"""Agent 与 MemoryContextProvider 集成测试（14D3 阶段）。"""

from __future__ import annotations

import unittest

from mini_deerflow.agent import Agent
from mini_deerflow.memory_context import MemoryContextProvider
from mini_deerflow.models import FinalAnswer, Message, ToolCall
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tools import (
    ToolRegistry,
    create_default_registry,
)


class RecordingContextProvider:
    """记录调用次数的 ContextProvider。"""

    def __init__(self, value: str) -> None:
        self.value = value
        self.call_count = 0

    def build(self) -> str:
        self.call_count += 1
        return self.value


class RecordingModel:
    """记录每次 decide 收到的消息列表的 Model。"""

    def __init__(self, decisions: list[FinalAnswer | ToolCall]) -> None:
        self._decisions = list(decisions)
        self.calls: list[list[Message]] = []

    def decide(self, messages: list[Message]) -> FinalAnswer | ToolCall:
        self.calls.append(list(messages))
        return self._decisions.pop(0)


def _store_with_default() -> InMemoryThreadStore:
    """创建一个含默认 Thread 的内存 Store。"""
    store = InMemoryThreadStore()
    store.create("default")
    return store


def _agent_with_context(
    context_value: str,
    store: InMemoryThreadStore | None = None,
) -> tuple[Agent, RecordingModel, RecordingContextProvider]:
    """创建带 ContextProvider 的 Agent 和 RecordingModel。"""
    store = store or _store_with_default()
    context_provider = RecordingContextProvider(context_value)
    model = RecordingModel([FinalAnswer(content="完成")])
    agent = Agent(
        model=model,
        registry=create_default_registry(),
        store=store,
        context_provider=context_provider,
    )
    return agent, model, context_provider


class TestSystemMessageSupport(unittest.TestCase):
    """system Message 支持测试。"""

    def test_message_from_dict_system(self):
        """Message.from_dict 支持 system 角色。"""
        msg = Message.from_dict({"role": "system", "content": "上下文"})
        self.assertEqual(msg.role, "system")
        self.assertEqual(msg.content, "上下文")

    def test_system_message_to_dict_roundtrip(self):
        """system Message to_dict / from_dict 往返。"""
        original = Message(role="system", content="长期记忆上下文")
        restored = Message.from_dict(original.to_dict())
        self.assertEqual(restored, original)
        self.assertEqual(restored.role, "system")

    def test_unknown_role_still_rejected(self):
        """未知 role 仍然被拒绝。"""
        with self.assertRaises(ValueError) as ctx:
            Message.from_dict({"role": "admin", "content": "内容"})
        self.assertIn("admin", str(ctx.exception))


class TestAgentContextProviderNone(unittest.TestCase):
    """context_provider=None 时 Agent 行为不变。"""

    def test_no_context_provider_works(self):
        """没有 ContextProvider 时 Agent 正常工作。"""
        from mini_deerflow.models import FakeModel

        store = _store_with_default()
        agent = Agent(
            model=FakeModel(),
            registry=create_default_registry(),
            store=store,
            context_provider=None,
        )
        answer, _ = agent.run("default", "计算 1 + 1")
        self.assertIn("2", answer)

    def test_backward_compatible_four_args(self):
        """原有 Agent 构造函数第四个位置参数 max_steps 仍然有效。"""
        from mini_deerflow.models import FakeModel

        store = _store_with_default()
        # 使用四个位置参数（原有方式）
        agent = Agent(
            FakeModel(),
            create_default_registry(),
            store,
            5,  # max_steps
        )
        self.assertEqual(agent._max_steps, 5)
        self.assertIsNone(agent._context_provider)


class TestAgentContextIntegration(unittest.TestCase):
    """Agent 与 ContextProvider 集成测试。"""

    def test_empty_context_no_system_message(self):
        """空字符串时不创建 system Message。"""
        store = _store_with_default()
        model = RecordingModel([FinalAnswer(content="完成")])
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
            context_provider=RecordingContextProvider(""),
        )
        agent.run("default", "你好")

        first_call = model.calls[0]
        roles = [m.role for m in first_call]
        self.assertNotIn("system", roles)

    def test_whitespace_only_context_no_system_message(self):
        """只有空白时不创建 system Message。"""
        store = _store_with_default()
        model = RecordingModel([FinalAnswer(content="完成")])
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
            context_provider=RecordingContextProvider("   \n\t  "),
        )
        agent.run("default", "你好")

        first_call = model.calls[0]
        roles = [m.role for m in first_call]
        self.assertNotIn("system", roles)

    def test_context_system_message_first(self):
        """有上下文时 system Message 位于模型输入第一条。"""
        agent, model, _ = _agent_with_context("长期记忆内容")
        agent.run("default", "你好")

        first_call = model.calls[0]
        self.assertEqual(first_call[0].role, "system")
        self.assertEqual(first_call[0].content, "长期记忆内容")

    def test_context_system_message_before_history(self):
        """system Message 后面是原有会话历史。"""
        store = _store_with_default()
        # 先运行一次创建历史
        from mini_deerflow.models import FakeModel
        Agent(
            model=FakeModel(),
            registry=create_default_registry(),
            store=store,
        ).run("default", "计算 1 + 1")

        # 第二次运行带上下文
        context_provider = RecordingContextProvider("用户偏好：喜欢简洁")
        model = RecordingModel([FinalAnswer(content="完成")])
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
            context_provider=context_provider,
        )
        agent.run("default", "你好")

        first_call = model.calls[0]
        # 第一条是 system，第二条是历史中的 user
        self.assertEqual(first_call[0].role, "system")
        self.assertEqual(first_call[1].role, "user")
        self.assertEqual(first_call[1].content, "计算 1 + 1")

    def test_context_system_message_before_user_input(self):
        """system Message 后面包含本轮 user Message。"""
        agent, model, _ = _agent_with_context("上下文")
        agent.run("default", "用户输入")

        first_call = model.calls[0]
        # 第一条是 system，第二条是本轮 user
        self.assertEqual(first_call[0].role, "system")
        self.assertEqual(first_call[1].role, "user")
        self.assertEqual(first_call[1].content, "用户输入")

    def test_tool_call_keeps_same_system_message(self):
        """工具调用后的第二次 decide 仍能看到同一 system Message。"""
        store = _store_with_default()
        model = RecordingModel([
            ToolCall(name="calculator", arguments={"expression": "1+1"}),
            FinalAnswer(content="完成"),
        ])
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
            context_provider=RecordingContextProvider("上下文"),
        )
        agent.run("default", "计算")

        # 第一次 decide
        first_call = model.calls[0]
        self.assertEqual(first_call[0].role, "system")
        self.assertEqual(first_call[0].content, "上下文")

        # 第二次 decide
        second_call = model.calls[1]
        self.assertEqual(second_call[0].role, "system")
        self.assertEqual(second_call[0].content, "上下文")
        # 第二次应该包含 tool response
        self.assertEqual(second_call[-2].role, "user")
        self.assertEqual(second_call[-1].role, "tool")


class TestAgentContextCallCount(unittest.TestCase):
    """ContextProvider 调用次数测试。"""

    def test_build_called_once_per_run(self):
        """每次 run 只调用一次 build()。"""
        agent, _, context_provider = _agent_with_context("上下文")
        agent.run("default", "你好")
        self.assertEqual(context_provider.call_count, 1)

    def test_multiple_runs_call_multiple_times(self):
        """连续两次 run 分别调用一次 build()。"""
        store = _store_with_default()
        context_provider = RecordingContextProvider("上下文")
        model = RecordingModel([FinalAnswer(content="完成"), FinalAnswer(content="完成")])
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
            context_provider=context_provider,
        )
        agent.run("default", "第一次")
        agent.run("default", "第二次")
        self.assertEqual(context_provider.call_count, 2)


class TestAgentContextNoPersistence(unittest.TestCase):
    """system Message 不持久化测试。"""

    def test_system_not_in_thread_state(self):
        """system Message 不写入 ThreadState。"""
        agent, _, _ = _agent_with_context("长期记忆")
        agent.run("default", "你好")

        store = _store_with_default()
        agent2, _, _ = _agent_with_context("")
        # 不带 ContextProvider 运行
        Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
        ).run("default", "你好")

        # ThreadState 中不应有 system
        thread = store.get("default")
        roles = [m.role for m in thread.snapshot()]
        self.assertNotIn("system", roles)

    def test_system_not_in_history(self):
        """system Message 不出现在 history。"""
        agent, _, _ = _agent_with_context("上下文")
        agent.run("default", "你好")

        # 从头创建带上下文的 Agent 并运行
        store = _store_with_default()
        agent3, _, _ = _agent_with_context("不同的上下文", store)
        agent3.run("default", "新的输入")

        # 验证只有 user/assistant，没有 system
        thread = store.get("default")
        for msg in thread.snapshot():
            self.assertNotEqual(msg.role, "system")

    def test_system_not_in_trace(self):
        """system Message 不出现在 trace。"""
        agent, model, _ = _agent_with_context("上下文")
        answer, trace = agent.run("default", "你好")

        # trace 中不应包含 system Message 内容
        for entry in trace:
            detail = str(entry.get("detail", ""))
            self.assertNotIn("上下文", detail)
            self.assertNotIn("system", detail)

    def test_thread_state_only_has_user_tool_assistant(self):
        """成功运行后 ThreadState 仍只保存 user、tool、assistant。"""
        store = _store_with_default()
        agent = Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
            context_provider=RecordingContextProvider("上下文"),
        )
        agent.run("default", "你好")

        thread = store.get("default")
        roles = [m.role for m in thread.snapshot()]
        self.assertEqual(roles, ["user", "assistant"])


class TestAgentContextException(unittest.TestCase):
    """ContextProvider 异常测试。"""

    def test_build_exception_propagates(self):
        """build() 异常原样传播。"""

        class FailingContextProvider:
            def build(self) -> str:
                raise RuntimeError("ContextProvider 失败")

        store = _store_with_default()
        model = RecordingModel([FinalAnswer(content="不应执行")])
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
            context_provider=FailingContextProvider(),  # type: ignore
        )

        with self.assertRaises(RuntimeError) as ctx:
            agent.run("default", "你好")
        self.assertEqual(str(ctx.exception), "ContextProvider 失败")

    def test_build_exception_model_not_called(self):
        """build() 异常时 Model 不被调用。"""

        class FailingContextProvider:
            def build(self) -> str:
                raise RuntimeError("失败")

        store = _store_with_default()
        model = RecordingModel([FinalAnswer(content="不应执行")])
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
            context_provider=FailingContextProvider(),  # type: ignore
        )

        try:
            agent.run("default", "你好")
        except RuntimeError:
            pass

        self.assertEqual(len(model.calls), 0)

    def test_build_exception_thread_state_unchanged(self):
        """build() 异常时 ThreadState 不变。"""

        class FailingContextProvider:
            def build(self) -> str:
                raise RuntimeError("失败")

        store = _store_with_default()
        agent = Agent(
            model=RecordingModel([FinalAnswer(content="不应执行")]),
            registry=create_default_registry(),
            store=store,
            context_provider=FailingContextProvider(),  # type: ignore
        )

        try:
            agent.run("default", "你好")
        except RuntimeError:
            pass

        # ThreadState 应该保持空
        thread = store.get("default")
        self.assertEqual(thread.message_count, 0)

    def test_build_returns_non_string_raises_type_error(self):
        """build() 返回非字符串时抛出 TypeError。"""

        class NonStringContextProvider:
            def build(self) -> str:
                return 123  # type: ignore

        store = _store_with_default()
        agent = Agent(
            model=RecordingModel([FinalAnswer(content="不应执行")]),
            registry=create_default_registry(),
            store=store,
            context_provider=NonStringContextProvider(),  # type: ignore
        )

        with self.assertRaises(TypeError) as ctx:
            agent.run("default", "你好")
        self.assertIn("字符串", str(ctx.exception))


class TestAgentContextBackwardCompat(unittest.TestCase):
    """向后兼容性测试。"""

    def test_original_agent_with_four_positional_args(self):
        """原有 Agent 四参数位置调用仍然有效。"""
        from mini_deerflow.models import FakeModel

        store = _store_with_default()
        # 原有代码可能使用位置参数
        agent = Agent(
            FakeModel(),
            create_default_registry(),
            store,
            5,  # max_steps
        )
        self.assertEqual(agent._max_steps, 5)
        self.assertIsNone(agent._context_provider)

    def test_original_tests_still_pass(self):
        """原有 Agent 测试仍然通过。"""
        from mini_deerflow.models import FakeModel

        store = _store_with_default()
        agent = Agent(model=FakeModel(), registry=create_default_registry(), store=store)
        answer, trace = agent.run("default", "计算 1 + 1")
        self.assertIn("2", answer)


if __name__ == "__main__":
    unittest.main()
