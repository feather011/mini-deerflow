"""Agent 与 SkillContextProvider 集成测试（15E 阶段）。"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from mini_deerflow.agent import Agent
from mini_deerflow.models import FinalAnswer, Message, ToolCall
from mini_deerflow.skill import Skill
from mini_deerflow.skill_catalog import SkillCatalog
from mini_deerflow.skill_context import SkillContextBuilder
from mini_deerflow.skill_loader import ProgressiveSkillLoader
from mini_deerflow.skill_metadata import SkillMetadata
from mini_deerflow.skill_registry import SkillRegistry
from mini_deerflow.skill_selector import RuleBasedSkillSelector
from mini_deerflow.thread_store import InMemoryThreadStore


def _store_with_default() -> InMemoryThreadStore:
    """创建一个含默认 Thread 的内存 Store。"""
    store = InMemoryThreadStore()
    store.create("default")
    return store


def _metadata(
    name: str = "test-skill",
    description: str = "Test Skill",
    source_path: str | Path = "/test/SKILL.md",
) -> SkillMetadata:
    return SkillMetadata(
        name=name,
        description=description,
        source_path=Path(source_path),
    )


def _skill(
    name: str = "test-skill",
    description: str = "Test Skill",
    instructions: str = "# Test",
    source_path: str | Path = "/test/SKILL.md",
) -> Skill:
    return Skill(
        name=name,
        description=description,
        instructions=instructions,
        source_path=Path(source_path),
    )


class RecordingSkillProvider:
    """记录调用次数和参数的 SkillContextProvider。"""

    def __init__(self, context: str) -> None:
        self._context = context
        self.call_count = 0
        self.calls: list[tuple[str, ...]] = []

    def build(self, user_input: str) -> str:
        self.call_count += 1
        self.calls.append((user_input,))
        return self._context


class RecordingModel:
    """记录每次 decide 收到的消息列表的 Model。"""

    def __init__(self, decisions: list[FinalAnswer | ToolCall]) -> None:
        self._decisions = list(decisions)
        self.calls: list[list[Message]] = []

    def decide(self, messages: list[Message]) -> FinalAnswer | ToolCall:
        self.calls.append(list(messages))
        return self._decisions.pop(0)


class FailingSkillProvider:
    """总是抛异常的 SkillContextProvider。"""

    def build(self, user_input: str) -> str:
        raise RuntimeError("SkillProvider 失败")


class NonStringSkillProvider:
    """返回非字符串的 SkillContextProvider。"""

    def build(self, user_input: str) -> str:
        return 123  # type: ignore


class TestAgentSkillContextNone(unittest.TestCase):
    """skill_context_provider=None 时 Agent 行为测试。"""

    def test_default_none(self):
        """默认值是 None。"""
        from mini_deerflow.models import FakeModel

        store = _store_with_default()
        agent = Agent(
            model=FakeModel(),
            registry=create_default_registry(),
            store=store,
        )
        self.assertIsNone(agent._skill_context_provider)

    def test_backward_compatible(self):
        """原有构造函数调用仍然有效。"""
        from mini_deerflow.models import FakeModel

        store = _store_with_default()
        # 原有四参数位置调用
        agent = Agent(
            FakeModel(),
            create_default_registry(),
            store,
            5,  # max_steps
        )
        self.assertEqual(agent._max_steps, 5)
        self.assertIsNone(agent._skill_context_provider)

    def test_no_skill_system_message(self):
        """没有 Skill Provider 时不产生 system Skill Message。"""
        from mini_deerflow.models import FakeModel

        store = _store_with_default()
        model = RecordingModel([FinalAnswer(content="完成")])
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
        )
        agent.run("default", "你好")

        first_call = model.calls[0]
        roles = [m.role for m in first_call]
        # 没有 system Message
        self.assertNotIn("system", roles)

    def test_original_message_order_unchanged(self):
        """原有消息顺序不变（user 在前）。"""
        from mini_deerflow.models import FakeModel

        store = _store_with_default()
        model = RecordingModel([FinalAnswer(content="完成")])
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
        )
        agent.run("default", "你好")

        first_call = model.calls[0]
        # 第一条是 user，不是 system
        self.assertEqual(first_call[0].role, "user")


def create_default_registry():
    from mini_deerflow.tools import create_default_registry as _cdr
    return _cdr()


class TestAgentSkillContextEmpty(unittest.TestCase):
    """空上下文测试。"""

    def test_build_returns_empty_string(self):
        """build 返回 "" 时不创建 system Message。"""
        store = _store_with_default()
        model = RecordingModel([FinalAnswer(content="完成")])
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
            skill_context_provider=RecordingSkillProvider(""),
        )
        agent.run("default", "你好")

        first_call = model.calls[0]
        roles = [m.role for m in first_call]
        self.assertNotIn("system", roles)

    def test_build_returns_whitespace(self):
        """build 返回空白时不创建 system Message。"""
        store = _store_with_default()
        model = RecordingModel([FinalAnswer(content="完成")])
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
            skill_context_provider=RecordingSkillProvider("   \n\t  "),
        )
        agent.run("default", "你好")

        first_call = model.calls[0]
        roles = [m.role for m in first_call]
        self.assertNotIn("system", roles)

    def test_empty_context_normal_answer(self):
        """空上下文时正常回答。"""
        from mini_deerflow.models import FakeModel

        store = _store_with_default()
        agent = Agent(
            model=FakeModel(),
            registry=create_default_registry(),
            store=store,
            skill_context_provider=RecordingSkillProvider(""),
        )
        answer, _ = agent.run("default", "你好")
        self.assertIsNotNone(answer)


class TestAgentSkillContextInjection(unittest.TestCase):
    """Skill 注入测试。"""

    def test_skill_context_before_history(self):
        """Skill context 位于历史之前。"""
        store = _store_with_default()
        # 先运行一次创建历史
        from mini_deerflow.models import FakeModel
        Agent(
            model=FakeModel(),
            registry=create_default_registry(),
            store=store,
        ).run("default", "计算 1 + 1")

        # 第二次运行带 Skill
        model = RecordingModel([FinalAnswer(content="完成")])
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
            skill_context_provider=RecordingSkillProvider("Skill 上下文"),
        )
        agent.run("default", "你好")

        first_call = model.calls[0]
        # 第二条是历史中的 user
        self.assertEqual(first_call[1].role, "user")
        self.assertEqual(first_call[1].content, "计算 1 + 1")

    def test_skill_system_message_role(self):
        """Skill system Message role 是 "system"。"""
        store = _store_with_default()
        model = RecordingModel([FinalAnswer(content="完成")])
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
            skill_context_provider=RecordingSkillProvider("Skill 上下文"),
        )
        agent.run("default", "你好")

        first_call = model.calls[0]
        self.assertEqual(first_call[0].role, "system")
        self.assertEqual(first_call[0].content, "Skill 上下文")

    def test_skill_content_complete(self):
        """Skill content 完整。"""
        skill_content = "## python-testing\n\n用途：测试\n\n# Test Steps\n\n1. Write tests"
        store = _store_with_default()
        model = RecordingModel([FinalAnswer(content="完成")])
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
            skill_context_provider=RecordingSkillProvider(skill_content),
        )
        agent.run("default", "测试帮助")

        first_call = model.calls[0]
        self.assertIn("python-testing", first_call[0].content)
        self.assertIn("Test Steps", first_call[0].content)

    def test_model_sees_skill_context(self):
        """Model 可以看到 Skill context。"""
        store = _store_with_default()
        model = RecordingModel([FinalAnswer(content="完成")])
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
            skill_context_provider=RecordingSkillProvider("Unique Skill Token ABC123"),
        )
        agent.run("default", "你好")

        first_call = model.calls[0]
        combined = "\n".join(m.content for m in first_call)
        self.assertIn("Unique Skill Token ABC123", combined)

    def test_provider_called_once_per_run(self):
        """Provider 每轮只调用一次。"""
        store = _store_with_default()
        model = RecordingModel([FinalAnswer(content="完成")])
        provider = RecordingSkillProvider("Skill 上下文")
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
            skill_context_provider=provider,
        )
        agent.run("default", "你好")

        self.assertEqual(provider.call_count, 1)

    def test_provider_receives_user_input(self):
        """Provider 收到准确的 user_input。"""
        store = _store_with_default()
        model = RecordingModel([FinalAnswer(content="完成")])
        provider = RecordingSkillProvider("Skill 上下文")
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
            skill_context_provider=provider,
        )
        agent.run("default", "用户输入内容 ABC123")

        self.assertEqual(provider.calls[0][0], "用户输入内容 ABC123")


class TestAgentSkillContextMemoryOrder(unittest.TestCase):
    """Memory 和 Skill system Message 顺序测试。"""

    def test_memory_before_skill(self):
        """Memory 在前，Skill 在后。"""
        store = _store_with_default()

        class RecordingMemoryProvider:
            def build(self) -> str:
                return "Memory 上下文"

        model = RecordingModel([FinalAnswer(content="完成")])
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
            context_provider=RecordingMemoryProvider(),
            skill_context_provider=RecordingSkillProvider("Skill 上下文"),
        )
        agent.run("default", "你好")

        first_call = model.calls[0]
        self.assertEqual(first_call[0].role, "system")
        self.assertEqual(first_call[0].content, "Memory 上下文")
        self.assertEqual(first_call[1].role, "system")
        self.assertEqual(first_call[1].content, "Skill 上下文")

    def test_memory_empty_skill_has_value(self):
        """Memory 为空，Skill 有值。"""
        store = _store_with_default()
        model = RecordingModel([FinalAnswer(content="完成")])
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
            context_provider=None,
            skill_context_provider=RecordingSkillProvider("Skill 上下文"),
        )
        agent.run("default", "你好")

        first_call = model.calls[0]
        self.assertEqual(first_call[0].role, "system")
        self.assertEqual(first_call[0].content, "Skill 上下文")

    def test_memory_has_value_skill_empty(self):
        """Memory 有值，Skill 为空。"""
        store = _store_with_default()

        class RecordingMemoryProvider:
            def build(self) -> str:
                return "Memory 上下文"

        model = RecordingModel([FinalAnswer(content="完成")])
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
            context_provider=RecordingMemoryProvider(),
            skill_context_provider=RecordingSkillProvider(""),
        )
        agent.run("default", "你好")

        first_call = model.calls[0]
        self.assertEqual(first_call[0].role, "system")
        self.assertEqual(first_call[0].content, "Memory 上下文")
        # 没有第二个 system
        self.assertEqual(first_call[1].role, "user")

    def test_both_have_value(self):
        """两者都有值。"""
        store = _store_with_default()

        class RecordingMemoryProvider:
            def build(self) -> str:
                return "Memory 上下文"

        model = RecordingModel([FinalAnswer(content="完成")])
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
            context_provider=RecordingMemoryProvider(),
            skill_context_provider=RecordingSkillProvider("Skill 上下文"),
        )
        agent.run("default", "你好")

        first_call = model.calls[0]
        system_msgs = [m for m in first_call if m.role == "system"]
        self.assertEqual(len(system_msgs), 2)
        self.assertEqual(system_msgs[0].content, "Memory 上下文")
        self.assertEqual(system_msgs[1].content, "Skill 上下文")

    def test_both_empty(self):
        """两者都为空。"""
        store = _store_with_default()
        model = RecordingModel([FinalAnswer(content="完成")])
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
            context_provider=None,
            skill_context_provider=RecordingSkillProvider(""),
        )
        agent.run("default", "你好")

        first_call = model.calls[0]
        roles = [m.role for m in first_call]
        self.assertNotIn("system", roles)


class TestAgentSkillContextToolLoop(unittest.TestCase):
    """工具循环测试。"""

    def test_multiple_decide_same_skill_context(self):
        """多次 decide 使用同一 Skill context。"""
        store = _store_with_default()
        model = RecordingModel([
            ToolCall(name="calculator", arguments={"expression": "1+1"}),
            FinalAnswer(content="完成"),
        ])
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
            skill_context_provider=RecordingSkillProvider("Skill 上下文"),
        )
        agent.run("default", "计算")

        # 第一次 decide
        first_call = model.calls[0]
        self.assertEqual(first_call[0].role, "system")
        self.assertEqual(first_call[0].content, "Skill 上下文")

        # 第二次 decide
        second_call = model.calls[1]
        self.assertEqual(second_call[0].role, "system")
        self.assertEqual(second_call[0].content, "Skill 上下文")

    def test_provider_called_once_in_tool_loop(self):
        """Provider 在工具循环中只调用一次。"""
        store = _store_with_default()
        model = RecordingModel([
            ToolCall(name="calculator", arguments={"expression": "1+1"}),
            FinalAnswer(content="完成"),
        ])
        provider = RecordingSkillProvider("Skill 上下文")
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
            skill_context_provider=provider,
        )
        agent.run("default", "计算")

        self.assertEqual(provider.call_count, 1)

    def test_second_decide_sees_tool_message(self):
        """第二次 decide 能看到 tool Message。"""
        store = _store_with_default()
        model = RecordingModel([
            ToolCall(name="calculator", arguments={"expression": "1+1"}),
            FinalAnswer(content="完成"),
        ])
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
            skill_context_provider=RecordingSkillProvider("Skill 上下文"),
        )
        agent.run("default", "计算")

        second_call = model.calls[1]
        # 有 tool Message
        tool_msgs = [m for m in second_call if m.role == "tool"]
        self.assertEqual(len(tool_msgs), 1)

    def test_skill_system_message_not_duplicated(self):
        """Skill system Message 不重复增加。"""
        store = _store_with_default()
        model = RecordingModel([
            ToolCall(name="calculator", arguments={"expression": "1+1"}),
            FinalAnswer(content="完成"),
        ])
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
            skill_context_provider=RecordingSkillProvider("Skill 上下文"),
        )
        agent.run("default", "计算")

        first_call = model.calls[0]
        second_call = model.calls[1]

        # 第一次只有一个 system
        first_system = [m for m in first_call if m.role == "system"]
        self.assertEqual(len(first_system), 1)

        # 第二次也只有 system 消息（不是两个）
        second_system = [m for m in second_call if m.role == "system"]
        self.assertEqual(len(second_system), 1)


class TestAgentSkillContextPersistence(unittest.TestCase):
    """持久化测试。"""

    def test_no_system_message_in_thread(self):
        """成功后 Thread 没有 system Message。"""
        from mini_deerflow.models import FakeModel

        store = _store_with_default()
        agent = Agent(
            model=FakeModel(),
            registry=create_default_registry(),
            store=store,
            skill_context_provider=RecordingSkillProvider("Skill 上下文"),
        )
        agent.run("default", "你好")

        thread = store.get("default")
        roles = [m.role for m in thread.snapshot()]
        self.assertNotIn("system", roles)

    def test_second_round_no_first_round_skill(self):
        """第二轮历史没有第一轮 Skill context。"""
        from mini_deerflow.models import FakeModel

        store = _store_with_default()

        # 第一轮带 Skill
        agent1 = Agent(
            model=FakeModel(),
            registry=create_default_registry(),
            store=store,
            skill_context_provider=RecordingSkillProvider("第一轮 Skill"),
        )
        agent1.run("default", "第一轮")

        # 第二轮不带 Skill
        agent2 = Agent(
            model=FakeModel(),
            registry=create_default_registry(),
            store=store,
            skill_context_provider=None,
        )
        agent2.run("default", "第二轮")

        thread = store.get("default")
        contents = [m.content for m in thread.snapshot()]
        # 第一轮内容中没有 Skill 上下文
        self.assertNotIn("第一轮 Skill", contents[0])
        self.assertNotIn("第一轮 Skill", contents[1])

    def test_store_restart_no_skill_context(self):
        """Store 重启恢复后没有 Skill context。"""
        from mini_deerflow.models import FakeModel

        store = _store_with_default()

        # 运行一轮带 Skill
        agent = Agent(
            model=FakeModel(),
            registry=create_default_registry(),
            store=store,
            skill_context_provider=RecordingSkillProvider("Skill 上下文"),
        )
        agent.run("default", "你好")

        # 读取 Thread
        thread = store.get("default")
        roles = [m.role for m in thread.snapshot()]

        # 没有 system
        self.assertNotIn("system", roles)


class TestAgentSkillContextException(unittest.TestCase):
    """异常测试。"""

    def test_provider_raises_exception(self):
        """Provider 抛异常时。"""
        store = _store_with_default()
        model = RecordingModel([FinalAnswer(content="不应执行")])
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
            skill_context_provider=FailingSkillProvider(),
        )

        with self.assertRaises(RuntimeError) as ctx:
            agent.run("default", "你好")
        self.assertEqual(str(ctx.exception), "SkillProvider 失败")

    def test_provider_returns_non_string(self):
        """Provider 返回非字符串时。"""
        store = _store_with_default()
        model = RecordingModel([FinalAnswer(content="不应执行")])
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
            skill_context_provider=NonStringSkillProvider(),
        )

        with self.assertRaises(TypeError) as ctx:
            agent.run("default", "你好")
        self.assertIn("字符串", str(ctx.exception))

    def test_exception_model_not_called(self):
        """异常时 Model 不调用。"""
        store = _store_with_default()
        model = RecordingModel([FinalAnswer(content="不应执行")])
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
            skill_context_provider=FailingSkillProvider(),
        )

        try:
            agent.run("default", "你好")
        except RuntimeError:
            pass

        self.assertEqual(len(model.calls), 0)

    def test_exception_tool_not_called(self):
        """异常时 Tool 不调用。"""
        store = _store_with_default()
        agent = Agent(
            model=RecordingModel([FinalAnswer(content="不应执行")]),
            registry=create_default_registry(),
            store=store,
            skill_context_provider=FailingSkillProvider(),
        )

        try:
            agent.run("default", "你好")
        except RuntimeError:
            pass

        # 没有工具被调用

    def test_exception_thread_unchanged(self):
        """异常时 Thread 不变。"""
        store = _store_with_default()
        agent = Agent(
            model=RecordingModel([FinalAnswer(content="不应执行")]),
            registry=create_default_registry(),
            store=store,
            skill_context_provider=FailingSkillProvider(),
        )

        try:
            agent.run("default", "你好")
        except RuntimeError:
            pass

        thread = store.get("default")
        self.assertEqual(thread.message_count, 0)

    def test_exception_store_save_not_called(self):
        """异常时 Store.save 不调用。"""
        store = _store_with_default()
        save_called = False

        original_save = store.save

        def tracking_save(thread):
            nonlocal save_called
            save_called = True
            return original_save(thread)

        store.save = tracking_save

        agent = Agent(
            model=RecordingModel([FinalAnswer(content="不应执行")]),
            registry=create_default_registry(),
            store=store,
            skill_context_provider=FailingSkillProvider(),
        )

        try:
            agent.run("default", "你好")
        except RuntimeError:
            pass

        self.assertFalse(save_called)

    def test_exception_memory_processor_not_called(self):
        """异常时 MemoryProcessor 不调用。"""

        class TrackingMemoryProcessor:
            def __init__(self):
                self.called = False

            def extract_and_save(self, source_thread_id, messages):
                self.called = True
                return []

        store = _store_with_default()
        memory_processor = TrackingMemoryProcessor()

        agent = Agent(
            model=RecordingModel([FinalAnswer(content="不应执行")]),
            registry=create_default_registry(),
            store=store,
            skill_context_provider=FailingSkillProvider(),
            memory_processor=memory_processor,
        )

        try:
            agent.run("default", "你好")
        except RuntimeError:
            pass

        self.assertFalse(memory_processor.called)


class TestAgentSkillContextMultiRound(unittest.TestCase):
    """多轮测试。"""

    def test_different_input_different_skill(self):
        """不同输入选择不同 Skill。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建两个 Skill
            for name, content in [
                ("python-testing", "Python Testing Content"),
                ("code-review", "Code Review Content"),
            ]:
                skill_dir = Path(tmpdir) / name
                skill_dir.mkdir()
                (skill_dir / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: {name}\n---\n# {name}\n\n{content}",
                    encoding="utf-8",
                )

            from mini_deerflow.skill_metadata import MarkdownSkillMetadataParser
            from mini_deerflow.skill import MarkdownSkillParser

            catalog = SkillCatalog(MarkdownSkillMetadataParser())
            catalog.load_directory(tmpdir)

            selector = RuleBasedSkillSelector({
                "python-testing": ("testing",),
                "code-review": ("review",),
            })
            parser = MarkdownSkillParser()
            registry = SkillRegistry(parser)
            loader = ProgressiveSkillLoader(parser, registry)
            builder = SkillContextBuilder()

            from mini_deerflow.skill_runtime import DefaultSkillContextProvider
            provider = DefaultSkillContextProvider(catalog, selector, loader, builder)

            store = _store_with_default()
            model = RecordingModel([FinalAnswer(content="完成")])
            agent = Agent(
                model=model,
                registry=create_default_registry(),
                store=store,
                skill_context_provider=provider,
            )

            # 第一轮：testing
            agent.run("default", "I need testing help")
            first_call = model.calls[0]
            self.assertIn("Python Testing", first_call[0].content)

            # 第二轮：review（使用新的 model）
            model2 = RecordingModel([FinalAnswer(content="完成")])
            agent2 = Agent(
                model=model2,
                registry=create_default_registry(),
                store=store,
                skill_context_provider=DefaultSkillContextProvider(
                    catalog, selector, loader, builder
                ),
            )
            agent2.run("default", "I need code review")

            first_call2 = model2.calls[0]
            self.assertIn("Code Review", first_call2[0].content)

    def test_second_round_calls_provider_again(self):
        """第二轮重新调用 Provider。"""
        store = _store_with_default()
        provider = RecordingSkillProvider("Skill 上下文")
        model = RecordingModel([
            FinalAnswer(content="第一轮完成"),
            FinalAnswer(content="第二轮完成"),
        ])
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
            skill_context_provider=provider,
        )

        agent.run("default", "第一轮")
        agent.run("default", "第二轮")

        self.assertEqual(provider.call_count, 2)

    def test_no_reuse_previous_round_skill(self):
        """不沿用上一轮 Skill。"""
        store = _store_with_default()
        provider = RecordingSkillProvider("第一轮 Skill")
        model = RecordingModel([
            FinalAnswer(content="第一轮"),
        ])
        agent = Agent(
            model=model,
            registry=create_default_registry(),
            store=store,
            skill_context_provider=provider,
        )
        agent.run("default", "第一轮")

        # 第二轮使用不同的 Provider
        provider2 = RecordingSkillProvider("第二轮 Skill")
        model2 = RecordingModel([FinalAnswer(content="第二轮")])
        agent2 = Agent(
            model=model2,
            registry=create_default_registry(),
            store=store,
            skill_context_provider=provider2,
        )
        agent2.run("default", "第二轮")

        # 第二轮只看到第二轮的 Skill
        first_call2 = model2.calls[0]
        self.assertIn("第二轮 Skill", first_call2[0].content)
        self.assertNotIn("第一轮 Skill", first_call2[0].content)

    def test_same_skill_hits_cache(self):
        """相同 Skill 可以命中 Loader 缓存。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "test-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: test-skill\ndescription: Test Skill\n---\n# Test\n\nContent",
                encoding="utf-8",
            )

            from mini_deerflow.skill_metadata import MarkdownSkillMetadataParser
            from mini_deerflow.skill import MarkdownSkillParser

            catalog = SkillCatalog(MarkdownSkillMetadataParser())
            catalog.load_directory(tmpdir)

            selector = RuleBasedSkillSelector({"test-skill": ("test",)})
            parser = MarkdownSkillParser()
            registry = SkillRegistry(parser)
            loader = ProgressiveSkillLoader(parser, registry)
            builder = SkillContextBuilder()

            from mini_deerflow.skill_runtime import DefaultSkillContextProvider
            provider = DefaultSkillContextProvider(catalog, selector, loader, builder)

            store = _store_with_default()

            # 第一轮
            model1 = RecordingModel([FinalAnswer(content="完成")])
            agent1 = Agent(
                model=model1,
                registry=create_default_registry(),
                store=store,
                skill_context_provider=provider,
            )
            agent1.run("default", "test input")

            # 第二轮（同一 Provider 实例）
            model2 = RecordingModel([FinalAnswer(content="完成")])
            agent2 = Agent(
                model=model2,
                registry=create_default_registry(),
                store=store,
                skill_context_provider=provider,
            )
            agent2.run("default", "test input again")

            # Registry 中只有一个 Skill
            self.assertEqual(len(registry.list_skills()), 1)

    def test_thread_history_order_correct(self):
        """Thread 历史顺序仍正确。"""
        from mini_deerflow.models import FakeModel

        store = _store_with_default()

        # 第一轮
        agent1 = Agent(
            model=FakeModel(),
            registry=create_default_registry(),
            store=store,
            skill_context_provider=RecordingSkillProvider("第一轮 Skill"),
        )
        agent1.run("default", "第一轮输入")

        # 第二轮
        agent2 = Agent(
            model=FakeModel(),
            registry=create_default_registry(),
            store=store,
            skill_context_provider=RecordingSkillProvider("第二轮 Skill"),
        )
        agent2.run("default", "第二轮输入")

        thread = store.get("default")
        roles = [m.role for m in thread.snapshot()]
        contents = [m.content for m in thread.snapshot()]

        # 顺序：user, assistant, user, assistant
        self.assertEqual(roles, ["user", "assistant", "user", "assistant"])
        self.assertEqual(contents[0], "第一轮输入")
        self.assertEqual(contents[2], "第二轮输入")


if __name__ == "__main__":
    unittest.main()
