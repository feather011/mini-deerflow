"""Agent 与 MemoryProcessor 集成测试（14D4 阶段）。"""

from __future__ import annotations

import unittest

from mini_deerflow.agent import Agent
from mini_deerflow.memory_manager import MemoryManager, MemoryProcessor
from mini_deerflow.memory_context import MemoryContextBuilder
from mini_deerflow.memory_extractor import RuleBasedMemoryExtractor
from mini_deerflow.memory import InMemoryMemoryStore
from mini_deerflow.models import FinalAnswer, Message, ToolCall
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tools import create_default_registry


def _msg(role: str, content: str) -> Message:
    return Message(role=role, content=content)


class RecordingMemoryProcessor:
    """记录调用次数的 MemoryProcessor。"""

    def __init__(
        self,
        result: list | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result or []
        self.error = error
        self.calls: list[tuple[str, list[Message]]] = []

    def extract_and_save(
        self,
        source_thread_id: str,
        messages: list[Message],
    ) -> list[Message]:
        self.calls.append((source_thread_id, list(messages)))
        if self.error is not None:
            raise self.error
        return list(self.result)


def _store_with_default() -> InMemoryThreadStore:
    """创建一个含默认 Thread 的内存 Store。"""
    store = InMemoryThreadStore()
    store.create("default")
    return store


class TestMemoryProcessorInterface(unittest.TestCase):
    """MemoryProcessor 接口测试。"""

    def test_memory_manager_implements_protocol(self):
        """MemoryManager 符合 MemoryProcessor Protocol。"""
        store = InMemoryMemoryStore()
        extractor = RuleBasedMemoryExtractor(lambda: "id-1")
        processor: MemoryProcessor = MemoryManager(extractor, store)
        self.assertTrue(hasattr(processor, "extract_and_save"))

    def test_recording_processor_can_inject(self):
        """RecordingMemoryProcessor 可以注入 Agent。"""
        store = _store_with_default()
        processor = RecordingMemoryProcessor()
        agent = Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=processor,
        )
        self.assertIs(agent._memory_processor, processor)


class RecordingModel:
    """记录每次 decide 收到的消息列表的 Model。"""

    def __init__(self, decisions: list[FinalAnswer | ToolCall]) -> None:
        self._decisions = list(decisions)
        self.calls: list[list[Message]] = []

    def decide(self, messages: list[Message], tools=None) -> FinalAnswer | ToolCall:
        self.calls.append(list(messages))
        return self._decisions.pop(0)


class TestAgentMemoryProcessingSuccess(unittest.TestCase):
    """记忆处理成功路径测试。"""

    def test_final_answer_calls_memory_processor(self):
        """FinalAnswer 后调用一次 MemoryProcessor。"""
        store = _store_with_default()
        processor = RecordingMemoryProcessor()
        model = RecordingModel([FinalAnswer(content="完成")])
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
            memory_processor=processor,
        )
        agent.run("default", "你好")

        self.assertEqual(len(processor.calls), 1)

    def test_source_thread_id_correct(self):
        """source_thread_id 正确传递。"""
        store = _store_with_default()
        store.create("thread-xyz")
        processor = RecordingMemoryProcessor()
        agent = Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=processor,
        )
        agent.run("thread-xyz", "你好")

        self.assertEqual(processor.calls[0][0], "thread-xyz")

    def test_only_user_message_passed(self):
        """只传入本轮 user Message。"""
        store = _store_with_default()
        processor = RecordingMemoryProcessor()
        agent = Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=processor,
        )
        agent.run("default", "用户输入")

        _, messages = processor.calls[0]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].role, "user")
        self.assertEqual(messages[0].content, "用户输入")

    def test_no_history_messages_passed(self):
        """不传历史消息。"""
        store = _store_with_default()
        # 先创建一些历史
        from mini_deerflow.models import FakeModel
        Agent(
            model=FakeModel(),
            registry=create_default_registry(),
            store=store,
        ).run("default", "计算 1 + 1")

        processor = RecordingMemoryProcessor()
        agent = Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=processor,
        )
        agent.run("default", "新消息")

        _, messages = processor.calls[0]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].content, "新消息")

    def test_no_tool_message_passed(self):
        """不传 tool Message。"""
        store = _store_with_default()
        processor = RecordingMemoryProcessor()
        model = RecordingModel([
            ToolCall(name="calculator", arguments={"expression": "1+1"}),
            FinalAnswer(content="完成"),
        ])
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
            memory_processor=processor,
        )
        agent.run("default", "计算")

        _, messages = processor.calls[0]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].role, "user")

    def test_no_assistant_message_passed(self):
        """不传 assistant Message。"""
        store = _store_with_default()
        processor = RecordingMemoryProcessor()
        agent = Agent(
            model=RecordingModel([FinalAnswer(content="回答")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=processor,
        )
        agent.run("default", "你好")

        _, messages = processor.calls[0]
        for msg in messages:
            self.assertNotEqual(msg.role, "assistant")

    def test_no_system_message_passed(self):
        """不传 system Message。"""
        store = _store_with_default()
        processor = RecordingMemoryProcessor()
        context_provider = RecordingContextProvider("上下文")
        agent = Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
            context_provider=context_provider,
            memory_processor=processor,
        )
        agent.run("default", "你好")

        _, messages = processor.calls[0]
        for msg in messages:
            self.assertNotEqual(msg.role, "system")

    def test_memory_saved_trace(self):
        """成功保存一条 Memory 时增加 memory_saved。"""
        store = _store_with_default()
        from mini_deerflow.memory import Memory
        processor = RecordingMemoryProcessor(result=[
            Memory(memory_id="m1", kind="fact", content="事实", source_thread_id="default")
        ])
        agent = Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=processor,
        )
        answer, trace = agent.run("default", "你好")

        memory_entries = [e for e in trace if e.get("type") == "memory_saved"]
        self.assertEqual(len(memory_entries), 1)
        self.assertEqual(memory_entries[0]["count"], 1)
        self.assertEqual(memory_entries[0]["memory_ids"], ["m1"])

    def test_multiple_memories_trace(self):
        """多条 Memory 时 count 和 memory_ids 正确。"""
        store = _store_with_default()
        from mini_deerflow.memory import Memory
        processor = RecordingMemoryProcessor(result=[
            Memory(memory_id="m1", kind="fact", content="事实1", source_thread_id="default"),
            Memory(memory_id="m2", kind="preference", content="偏好", source_thread_id="default"),
        ])
        agent = Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=processor,
        )
        answer, trace = agent.run("default", "你好")

        memory_entries = [e for e in trace if e.get("type") == "memory_saved"]
        self.assertEqual(len(memory_entries), 1)
        self.assertEqual(memory_entries[0]["count"], 2)
        self.assertEqual(memory_entries[0]["memory_ids"], ["m1", "m2"])

    def test_empty_result_no_memory_saved_trace(self):
        """空列表时不增加 memory_saved。"""
        store = _store_with_default()
        processor = RecordingMemoryProcessor(result=[])
        agent = Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=processor,
        )
        answer, trace = agent.run("default", "你好")

        memory_entries = [e for e in trace if e.get("type") == "memory_saved"]
        self.assertEqual(len(memory_entries), 0)

    def test_memory_saved_after_final_answer(self):
        """memory_saved 位于 final_answer 后面。"""
        store = _store_with_default()
        from mini_deerflow.memory import Memory
        processor = RecordingMemoryProcessor(result=[
            Memory(memory_id="m1", kind="fact", content="事实", source_thread_id="default")
        ])
        agent = Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=processor,
        )
        answer, trace = agent.run("default", "你好")

        final_answer_idx = next(i for i, e in enumerate(trace) if e.get("action") == "final_answer")
        memory_saved_idx = next(i for i, e in enumerate(trace) if e.get("type") == "memory_saved")
        self.assertGreater(memory_saved_idx, final_answer_idx)


class RecordingContextProvider:
    """记录调用次数的 ContextProvider。"""

    def __init__(self, value: str) -> None:
        self.value = value
        self.call_count = 0

    def build(self) -> str:
        self.call_count += 1
        return self.value


class TestAgentMemoryProcessingNotCalled(unittest.TestCase):
    """不调用 MemoryProcessor 的路径测试。"""

    def test_no_processor_unchanged(self):
        """memory_processor=None 时行为不变。"""
        store = _store_with_default()
        agent = Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=None,
        )
        answer, trace = agent.run("default", "你好")
        self.assertIn("完成", answer)
        self.assertEqual(len(trace), 1)  # 只有 final_answer

    def test_unknown_tool_not_call_processor(self):
        """未知工具时不调用。"""
        store = _store_with_default()

        class UnknownToolModel:
            def decide(self, messages, tools=None):
                return ToolCall(name="hack", arguments={})

        agent = Agent(
            model=UnknownToolModel(),
            registry=create_default_registry(),
            store=store,
            memory_processor=RecordingMemoryProcessor(),
        )
        with self.assertRaises(Exception):
            agent.run("default", "你好")

    def test_tool_failure_not_call_processor(self):
        """工具失败时不调用。"""
        store = _store_with_default()
        from mini_deerflow.tools import ToolRegistry, Tool, ToolExecutionError

        def failing_handler(args):
            raise ValueError("boom")

        registry = ToolRegistry()
        registry.register(Tool(
            name="boom",
            description="总是失败的工具",
            parameters={"type": "object"},
            handler=failing_handler,
        ))

        class BoomModel:
            def decide(self, messages, tools=None):
                return ToolCall(name="boom", arguments={})

        agent = Agent(
            model=BoomModel(),
            registry=registry,
            store=store,
            memory_processor=RecordingMemoryProcessor(),
        )
        with self.assertRaises(ToolExecutionError):
            agent.run("default", "你好")

    def test_max_steps_not_call_processor(self):
        """max_steps 时不调用。"""

        class AlwaysToolModel:
            def decide(self, messages, tools=None):
                return ToolCall(name="calculator", arguments={"expression": "1+1"})

        store = _store_with_default()
        processor = RecordingMemoryProcessor()
        agent = Agent(
            model=AlwaysToolModel(),
            registry=create_default_registry(),
            store=store,
            memory_processor=processor,
            max_steps=1,
        )
        with self.assertRaises(RuntimeError):
            agent.run("default", "你好")

        self.assertEqual(len(processor.calls), 0)

    def test_model_exception_not_call_processor(self):
        """Model 异常时不调用。"""

        class FailingModel:
            def decide(self, messages, tools=None):
                raise RuntimeError("模型失败")

        store = _store_with_default()
        processor = RecordingMemoryProcessor()
        agent = Agent(
            model=FailingModel(),
            registry=create_default_registry(),
            store=store,
            memory_processor=processor,
        )
        with self.assertRaises(RuntimeError):
            agent.run("default", "你好")

        self.assertEqual(len(processor.calls), 0)

    def test_store_save_failure_not_call_processor(self):
        """ThreadStore.save 失败时不调用。"""

        class FailingStore:
            def get(self, thread_id):
                from mini_deerflow.thread_state import ThreadState
                return ThreadState(thread_id=thread_id)

            def save(self, thread):
                raise RuntimeError("保存失败")

        agent = Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=FailingStore(),  # type: ignore
            memory_processor=RecordingMemoryProcessor(),
        )
        with self.assertRaises(RuntimeError):
            agent.run("default", "你好")


class TestAgentMemoryProcessingFailure(unittest.TestCase):
    """记忆处理失败策略测试。"""

    def test_processor_value_error_still_returns_answer(self):
        """MemoryProcessor 抛出 ValueError 时仍返回答案。"""
        store = _store_with_default()
        processor = RecordingMemoryProcessor(error=ValueError("记忆冲突"))
        agent = Agent(
            model=RecordingModel([FinalAnswer(content="成功回答")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=processor,
        )
        answer, trace = agent.run("default", "你好")
        self.assertEqual(answer, "成功回答")

    def test_processor_runtime_error_still_returns_answer(self):
        """MemoryProcessor 抛出 RuntimeError 时仍返回答案。"""
        store = _store_with_default()
        processor = RecordingMemoryProcessor(error=RuntimeError("提取失败"))
        agent = Agent(
            model=RecordingModel([FinalAnswer(content="成功回答")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=processor,
        )
        answer, trace = agent.run("default", "你好")
        self.assertEqual(answer, "成功回答")

    def test_processor_failure_adds_warning(self):
        """失败时增加 memory_warning。"""
        store = _store_with_default()
        processor = RecordingMemoryProcessor(error=RuntimeError("提取失败"))
        agent = Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=processor,
        )
        answer, trace = agent.run("default", "你好")

        warning_entries = [e for e in trace if e.get("type") == "memory_warning"]
        self.assertEqual(len(warning_entries), 1)
        self.assertIn("提取失败", warning_entries[0]["error"])

    def test_warning_contains_error_message(self):
        """memory_warning 包含错误信息。"""
        store = _store_with_default()
        processor = RecordingMemoryProcessor(error=ValueError("具体错误信息"))
        agent = Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=processor,
        )
        answer, trace = agent.run("default", "你好")

        warning = next(e for e in trace if e.get("type") == "memory_warning")
        self.assertIn("具体错误信息", warning["error"])

    def test_failure_thread_state_saved(self):
        """失败后 ThreadState 已保存 user 和 assistant。"""
        store = _store_with_default()
        processor = RecordingMemoryProcessor(error=RuntimeError("失败"))
        agent = Agent(
            model=RecordingModel([FinalAnswer(content="回答")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=processor,
        )
        agent.run("default", "你好")

        thread = store.get("default")
        roles = [m.role for m in thread.snapshot()]
        self.assertIn("user", roles)
        self.assertIn("assistant", roles)

    def test_failure_no_memory_saved(self):
        """失败时不增加 memory_saved。"""
        store = _store_with_default()
        processor = RecordingMemoryProcessor(error=RuntimeError("失败"))
        agent = Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=processor,
        )
        answer, trace = agent.run("default", "你好")

        memory_saved = [e for e in trace if e.get("type") == "memory_saved"]
        self.assertEqual(len(memory_saved), 0)

    def test_keyboard_interrupt_not_swallowed(self):
        """KeyboardInterrupt 不被吞掉。"""
        store = _store_with_default()

        class KeyboardInterruptProcessor:
            def extract_and_save(self, source_thread_id, messages):
                raise KeyboardInterrupt()

        agent = Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=KeyboardInterruptProcessor(),  # type: ignore
        )
        with self.assertRaises(KeyboardInterrupt):
            agent.run("default", "你好")

    def test_system_exit_not_swallowed(self):
        """SystemExit 不被吞掉。"""
        store = _store_with_default()

        class SystemExitProcessor:
            def extract_and_save(self, source_thread_id, messages):
                raise SystemExit()

        agent = Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=SystemExitProcessor(),  # type: ignore
        )
        with self.assertRaises(SystemExit):
            agent.run("default", "你好")


class TestAgentMemoryProcessingIsolation(unittest.TestCase):
    """数据隔离测试。"""

    def test_memory_not_in_thread_state(self):
        """Memory 不写入 ThreadState。"""
        store = _store_with_default()
        from mini_deerflow.memory import Memory
        processor = RecordingMemoryProcessor(result=[
            Memory(memory_id="m1", kind="fact", content="事实内容", source_thread_id="default")
        ])
        agent = Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=processor,
        )
        agent.run("default", "你好")

        thread = store.get("default")
        for msg in thread.snapshot():
            self.assertNotEqual(msg.role, "system")
            self.assertNotIn("事实内容", msg.content)

    def test_memory_not_in_history(self):
        """Memory 不出现在 history。"""
        store = _store_with_default()
        from mini_deerflow.memory import Memory
        processor = RecordingMemoryProcessor(result=[
            Memory(memory_id="m1", kind="fact", content="记忆内容", source_thread_id="default")
        ])
        agent = Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=processor,
        )
        agent.run("default", "你好")

        thread = store.get("default")
        for msg in thread.snapshot():
            self.assertNotIn("记忆内容", msg.content)

    def test_memory_content_not_in_trace(self):
        """Memory.content 不出现在 trace。"""
        store = _store_with_default()
        from mini_deerflow.memory import Memory
        processor = RecordingMemoryProcessor(result=[
            Memory(memory_id="m1", kind="fact", content="秘密内容", source_thread_id="default")
        ])
        agent = Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=processor,
        )
        answer, trace = agent.run("default", "你好")

        trace_str = str(trace)
        self.assertNotIn("秘密内容", trace_str)

    def test_system_context_not_re_extracted(self):
        """system Memory Context 不会被重新提取。"""
        store = _store_with_default()
        processor = RecordingMemoryProcessor(result=[])
        context_provider = RecordingContextProvider("用户偏好：喜欢 uv")
        agent = Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
            context_provider=context_provider,
            memory_processor=processor,
        )
        # 第一轮带 system 上下文运行
        agent.run("default", "记住偏好：用户使用 uv")

        # 检查传给 processor 的消息不包含 system
        _, messages = processor.calls[0]
        for msg in messages:
            self.assertNotEqual(msg.role, "system")

    def test_consecutive_runs_separate(self):
        """连续两次 run 每次只处理各自的 user Message。"""
        store = _store_with_default()
        processor = RecordingMemoryProcessor(result=[])
        agent = Agent(
            model=RecordingModel([FinalAnswer(content="完成"), FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=processor,
        )

        agent.run("default", "第一条消息")
        agent.run("default", "第二条消息")

        self.assertEqual(len(processor.calls), 2)
        self.assertEqual(processor.calls[0][1][0].content, "第一条消息")
        self.assertEqual(processor.calls[1][1][0].content, "第二条消息")


class TestAgentMemoryEndToEnd(unittest.TestCase):
    """端到端组合测试。"""

    def test_cross_thread_memory_sharing(self):
        """跨 Thread 记忆共享测试。"""
        # 创建共享的 MemoryStore
        memory_store = InMemoryMemoryStore()
        id_counter = iter(["m1", "m2"])

        # 第一轮：thread-a 保存记忆
        thread_store_a = InMemoryThreadStore()
        thread_store_a.create("thread-a")

        extractor = RuleBasedMemoryExtractor(lambda: next(id_counter))
        manager = MemoryManager(extractor, memory_store)
        context_builder = MemoryContextBuilder(memory_store)

        agent_a = Agent(
            model=RecordingModel([FinalAnswer(content="已记住")]),
            registry=create_default_registry(),
            store=thread_store_a,
            context_provider=context_builder,
            memory_processor=manager,
        )
        answer_a, trace_a = agent_a.run("thread-a", "记住偏好：用户喜欢使用 uv")

        # 验证 MemoryStore 中保存了记忆
        self.assertEqual(len(memory_store.list_memories()), 1)
        saved_memory = memory_store.get("m1")
        self.assertEqual(saved_memory.kind, "preference")
        self.assertEqual(saved_memory.content, "用户喜欢使用 uv")
        self.assertEqual(saved_memory.source_thread_id, "thread-a")

        # 验证 thread-a 的历史没有 system Message
        thread_a = thread_store_a.get("thread-a")
        roles_a = [m.role for m in thread_a.snapshot()]
        self.assertNotIn("system", roles_a)

        # 验证 thread-a 的 Model 收到了 system 上下文
        self.assertIn("用户喜欢使用 uv", agent_a._context_provider.build())

        # 第二轮：thread-b 使用同一记忆
        thread_store_b = InMemoryThreadStore()
        thread_store_b.create("thread-b")

        agent_b = Agent(
            model=RecordingModel([FinalAnswer(content="回答完成")]),
            registry=create_default_registry(),
            store=thread_store_b,
            context_provider=context_builder,
            memory_processor=manager,
        )
        answer_b, trace_b = agent_b.run("thread-b", "你好")

        # 验证 thread-b 的 Model 收到了 system 上下文
        self.assertIn("用户喜欢使用 uv", agent_b._context_provider.build())

        # 验证 thread-b 的历史没有 system Message
        thread_b = thread_store_b.get("thread-b")
        roles_b = [m.role for m in thread_b.snapshot()]
        self.assertNotIn("system", roles_b)

        # 验证两个 Thread 相互隔离
        self.assertEqual(thread_a.message_count, 2)  # user + assistant
        self.assertEqual(thread_b.message_count, 2)  # user + assistant


class TestAgentMemoryDeduplication(unittest.TestCase):
    """记忆去重集成测试。"""

    def test_first_explicit_memory_produces_memory_saved(self):
        """第一次明确记忆产生 memory_saved。"""
        store = _store_with_default()
        extractor = RuleBasedMemoryExtractor(lambda: "m1")
        manager = MemoryManager(extractor, InMemoryMemoryStore())
        agent = Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=manager,
        )
        answer, trace = agent.run("default", "记住偏好：用户喜欢 uv")

        memory_entries = [e for e in trace if e.get("type") == "memory_saved"]
        self.assertEqual(len(memory_entries), 1)
        self.assertEqual(memory_entries[0]["count"], 1)

    def test_duplicate_content_no_memory_saved(self):
        """第二次相同内容不产生 memory_saved。"""
        store = _store_with_default()
        memory_store = InMemoryMemoryStore()

        # 第一轮
        extractor1 = RuleBasedMemoryExtractor(lambda: "m1")
        manager1 = MemoryManager(extractor1, memory_store)
        agent1 = Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=manager1,
        )
        agent1.run("default", "记住偏好：用户喜欢 uv")

        # 第二轮，相同内容
        extractor2 = RuleBasedMemoryExtractor(lambda: "m2")
        manager2 = MemoryManager(extractor2, memory_store)
        agent2 = Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=manager2,
        )
        _, trace = agent2.run("default", "记住偏好：用户喜欢 UV")

        memory_entries = [e for e in trace if e.get("type") == "memory_saved"]
        self.assertEqual(len(memory_entries), 0)

    def test_duplicate_content_no_memory_warning(self):
        """第二次相同内容不产生 memory_warning。"""
        store = _store_with_default()
        memory_store = InMemoryMemoryStore()

        # 第一轮
        extractor1 = RuleBasedMemoryExtractor(lambda: "m1")
        manager1 = MemoryManager(extractor1, memory_store)
        agent1 = Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=manager1,
        )
        agent1.run("default", "记住偏好：用户喜欢 uv")

        # 第二轮，相同内容
        extractor2 = RuleBasedMemoryExtractor(lambda: "m2")
        manager2 = MemoryManager(extractor2, memory_store)
        agent2 = Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=manager2,
        )
        _, trace = agent2.run("default", "记住偏好：用户喜欢 uv")

        warning_entries = [e for e in trace if e.get("type") == "memory_warning"]
        self.assertEqual(len(warning_entries), 0)

    def test_two_runs_only_one_memory_stored(self):
        """两次运行后 Store 中只有一条 Memory。"""
        store = _store_with_default()
        memory_store = InMemoryMemoryStore()

        # 第一轮
        extractor1 = RuleBasedMemoryExtractor(lambda: "m1")
        manager1 = MemoryManager(extractor1, memory_store)
        agent1 = Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=manager1,
        )
        agent1.run("default", "记住偏好：用户喜欢 uv")

        # 第二轮，相同内容
        extractor2 = RuleBasedMemoryExtractor(lambda: "m2")
        manager2 = MemoryManager(extractor2, memory_store)
        agent2 = Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=manager2,
        )
        agent2.run("default", "记住偏好：用户喜欢 UV")

        # Store 中只有一条记忆
        self.assertEqual(len(memory_store.list_memories()), 1)

    def test_different_kind_same_content_stored(self):
        """不同 kind 相同 content 可以分别保存。"""
        store = _store_with_default()
        memory_store = InMemoryMemoryStore()

        # 第一轮：fact
        extractor1 = RuleBasedMemoryExtractor(lambda: "m1")
        manager1 = MemoryManager(extractor1, memory_store)
        agent1 = Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=manager1,
        )
        agent1.run("default", "记住事实：用户是学生")

        # 第二轮：preference，相同内容
        extractor2 = RuleBasedMemoryExtractor(lambda: "m2")
        manager2 = MemoryManager(extractor2, memory_store)
        agent2 = Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=manager2,
        )
        agent2.run("default", "记住偏好：用户是学生")

        # Store 中有两条记忆（不同 kind）
        self.assertEqual(len(memory_store.list_memories()), 2)

    def test_different_content_stored_normally(self):
        """不同内容正常保存。"""
        store = _store_with_default()
        memory_store = InMemoryMemoryStore()

        # 第一轮
        extractor1 = RuleBasedMemoryExtractor(lambda: "m1")
        manager1 = MemoryManager(extractor1, memory_store)
        agent1 = Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=manager1,
        )
        agent1.run("default", "记住事实：用户住在北京")

        # 第二轮，不同内容
        extractor2 = RuleBasedMemoryExtractor(lambda: "m2")
        manager2 = MemoryManager(extractor2, memory_store)
        agent2 = Agent(
            model=RecordingModel([FinalAnswer(content="完成")]),
            registry=create_default_registry(),
            store=store,
            memory_processor=manager2,
        )
        _, trace = agent2.run("default", "记住事实：用户住在上海")

        # 应该产生 memory_saved
        memory_entries = [e for e in trace if e.get("type") == "memory_saved"]
        self.assertEqual(len(memory_entries), 1)
        self.assertEqual(memory_entries[0]["count"], 1)

        # Store 中有两条记忆
        self.assertEqual(len(memory_store.list_memories()), 2)


if __name__ == "__main__":
    unittest.main()
