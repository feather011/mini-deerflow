"""MemoryManager 协调服务测试。"""

from __future__ import annotations

import copy
import unittest
from typing import Iterator

from mini_deerflow.memory import (
    InMemoryMemoryStore,
    Memory,
    MemoryAlreadyExistsError,
    MemoryStore,
)
from mini_deerflow.memory_extractor import MemoryExtractor
from mini_deerflow.memory_manager import MemoryManager
from mini_deerflow.models import Message


def _msg(role: str, content: str) -> Message:
    return Message(role=role, content=content)


def _memory(
    memory_id: str,
    kind: str = "fact",
    content: str = "测试内容",
    source_thread_id: str = "thread-1",
) -> Memory:
    return Memory(
        memory_id=memory_id,
        kind=kind,
        content=content,
        source_thread_id=source_thread_id,
    )


class StubExtractor:
    """桩提取器，用于测试协调逻辑。"""

    def __init__(
        self,
        memories: list[Memory] | None = None,
        exception: Exception | None = None,
    ) -> None:
        self._memories = memories or []
        self._exception = exception
        self.calls: list[tuple[str, list[Message]]] = []

    def extract(
        self,
        source_thread_id: str,
        messages: list[Message],
    ) -> list[Memory]:
        self.calls.append((source_thread_id, list(messages)))
        if self._exception is not None:
            raise self._exception
        return list(self._memories)


class TestMemoryManagerInit(unittest.TestCase):
    """MemoryManager 初始化测试。"""

    def test_init_stores_dependencies(self):
        """构造函数正确保存 extractor 和 store。"""
        extractor = StubExtractor()
        store = InMemoryMemoryStore()
        manager = MemoryManager(extractor, store)
        self.assertIs(manager._extractor, extractor)
        self.assertIs(manager._store, store)


class TestMemoryManagerExtractAndSave(unittest.TestCase):
    """extract_and_save 方法测试。"""

    def test_calls_extractor(self):
        """正确调用 extractor.extract。"""
        extractor = StubExtractor([_memory("m1")])
        store = InMemoryMemoryStore()
        manager = MemoryManager(extractor, store)

        messages = [_msg("user", "测试消息")]
        manager.extract_and_save("thread-1", messages)

        self.assertEqual(len(extractor.calls), 1)
        call_thread_id, call_messages = extractor.calls[0]
        self.assertEqual(call_thread_id, "thread-1")
        self.assertEqual(len(call_messages), 1)

    def test_source_thread_id_passed_correctly(self):
        """source_thread_id 正确传递给 extractor。"""
        extractor = StubExtractor([])
        store = InMemoryMemoryStore()
        manager = MemoryManager(extractor, store)

        manager.extract_and_save("my-thread-id", [])
        self.assertEqual(extractor.calls[0][0], "my-thread-id")

    def test_messages_passed_correctly(self):
        """messages 正确传递给 extractor。"""
        extractor = StubExtractor([])
        store = InMemoryMemoryStore()
        manager = MemoryManager(extractor, store)

        messages = [
            _msg("user", "消息1"),
            _msg("assistant", "消息2"),
        ]
        manager.extract_and_save("thread-1", messages)

        call_messages = extractor.calls[0][1]
        self.assertEqual(len(call_messages), 2)
        self.assertEqual(call_messages[0].content, "消息1")
        self.assertEqual(call_messages[1].content, "消息2")

    def test_single_memory_saved(self):
        """一条 Memory 被正确保存。"""
        memory = _memory("m1", kind="fact", content="测试事实")
        extractor = StubExtractor([memory])
        store = InMemoryMemoryStore()
        manager = MemoryManager(extractor, store)

        result = manager.extract_and_save("thread-1", [])

        self.assertEqual(len(result), 1)
        self.assertTrue(store.exists("m1"))
        self.assertEqual(store.get("m1"), memory)

    def test_multiple_memories_saved_in_order(self):
        """多条 Memory 按顺序保存。"""
        memories = [
            _memory("m1", content="第一条"),
            _memory("m2", content="第二条"),
            _memory("m3", content="第三条"),
        ]
        extractor = StubExtractor(memories)
        store = InMemoryMemoryStore()
        manager = MemoryManager(extractor, store)

        result = manager.extract_and_save("thread-1", [])

        self.assertEqual(len(result), 3)
        self.assertEqual(result[0].memory_id, "m1")
        self.assertEqual(result[1].memory_id, "m2")
        self.assertEqual(result[2].memory_id, "m3")

    def test_returns_saved_memories(self):
        """返回本次保存的 Memory 列表。"""
        memory = _memory("m1", content="测试")
        extractor = StubExtractor([memory])
        store = InMemoryMemoryStore()
        manager = MemoryManager(extractor, store)

        result = manager.extract_and_save("thread-1", [])

        self.assertEqual(result[0].memory_id, "m1")
        self.assertEqual(result[0].content, "测试")

    def test_empty_extraction_result(self):
        """空提取结果返回空列表，不调用 store.add。"""
        extractor = StubExtractor([])
        store = InMemoryMemoryStore()
        manager = MemoryManager(extractor, store)

        result = manager.extract_and_save("thread-1", [])

        self.assertEqual(result, [])
        self.assertEqual(len(store.list_memories()), 0)


class TestMemoryManagerExceptionPropagation(unittest.TestCase):
    """异常传播测试。"""

    def test_extractor_exception_propagates(self):
        """extractor 抛出的异常原样传播。"""
        error = ValueError("extractor 错误")
        extractor = StubExtractor(exception=error)
        store = InMemoryMemoryStore()
        manager = MemoryManager(extractor, store)

        with self.assertRaises(ValueError) as ctx:
            manager.extract_and_save("thread-1", [])
        self.assertEqual(str(ctx.exception), "extractor 错误")

    def test_store_unchanged_on_extractor_exception(self):
        """extractor 异常时 Store 保持不变。"""
        extractor = StubExtractor(exception=ValueError("错误"))
        store = InMemoryMemoryStore()
        manager = MemoryManager(extractor, store)

        try:
            manager.extract_and_save("thread-1", [])
        except ValueError:
            pass

        self.assertEqual(len(store.list_memories()), 0)


class TestMemoryManagerConflictDetection(unittest.TestCase):
    """批次冲突检测测试。"""

    def test_duplicate_id_in_batch(self):
        """本批次存在重复 ID 时抛出 MemoryAlreadyExistsError。"""
        memories = [
            _memory("m1", content="内容一"),
            _memory("m2", content="内容二"),
            _memory("m1", content="内容三"),  # 重复 ID
        ]
        extractor = StubExtractor(memories)
        store = InMemoryMemoryStore()
        manager = MemoryManager(extractor, store)

        with self.assertRaises(MemoryAlreadyExistsError) as ctx:
            manager.extract_and_save("thread-1", [])
        self.assertEqual(ctx.exception.memory_id, "m1")

    def test_duplicate_id_in_store(self):
        """Store 已存在相同 ID 时抛出 MemoryAlreadyExistsError。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m2", content="内容二"))  # Store 已有 m2

        memories = [
            _memory("m1", content="内容一"),
            _memory("m2", content="内容三"),  # 与 Store 冲突
        ]
        extractor = StubExtractor(memories)
        manager = MemoryManager(extractor, store)

        with self.assertRaises(MemoryAlreadyExistsError) as ctx:
            manager.extract_and_save("thread-1", [])
        self.assertEqual(ctx.exception.memory_id, "m2")

    def test_batch_conflict_saves_nothing(self):
        """批次冲突时一条都不保存。"""
        memories = [
            _memory("m1"),
            _memory("m2"),  # m2 会与 Store 冲突
        ]
        extractor = StubExtractor(memories)
        store = InMemoryMemoryStore()
        store.add(_memory("m2"))  # Store 已有 m2
        manager = MemoryManager(extractor, store)

        try:
            manager.extract_and_save("thread-1", [])
        except MemoryAlreadyExistsError:
            pass

        # m1 不应该被保存（批次冲突阻止了写入）
        self.assertFalse(store.exists("m1"))
        # Store 中的 m2 应该还在（冲突检查只阻止写入，不删除已有数据）
        self.assertTrue(store.exists("m2"))


class TestMemoryManagerReturnValueProtection(unittest.TestCase):
    """返回值保护测试。"""

    def test_returns_new_list(self):
        """返回新的 list，不是 extractor 内部列表。"""
        memories = [_memory("m1")]
        extractor = StubExtractor(memories)
        store = InMemoryMemoryStore()
        manager = MemoryManager(extractor, store)

        result = manager.extract_and_save("thread-1", [])

        # 修改返回值不应影响 extractor
        result.clear()
        self.assertEqual(len(extractor._memories), 1)

    def test_input_messages_not_modified(self):
        """不修改输入的 messages 列表。"""
        memories = [_memory("m1")]
        extractor = StubExtractor(memories)
        store = InMemoryMemoryStore()
        manager = MemoryManager(extractor, store)

        messages = [_msg("user", "原始内容")]
        original_content = messages[0].content

        manager.extract_and_save("thread-1", messages)

        self.assertEqual(messages[0].content, original_content)

    def test_generator_input_supported(self):
        """支持生成器作为 messages 输入。"""
        def msg_generator() -> Iterator[Message]:
            yield _msg("user", "消息1")
            yield _msg("user", "消息2")

        memories = [_memory("m1")]
        extractor = StubExtractor(memories)
        store = InMemoryMemoryStore()
        manager = MemoryManager(extractor, store)

        result = manager.extract_and_save("thread-1", msg_generator())
        self.assertEqual(len(result), 1)


class TestMemoryManagerIntegration(unittest.TestCase):
    """与 RuleBasedMemoryExtractor 和 InMemoryMemoryStore 的真实组合测试。"""

    def test_real_extractor_and_store_integration(self):
        """完整集成测试。"""
        from mini_deerflow.memory_extractor import RuleBasedMemoryExtractor

        id_counter = iter(["memory-001", "memory-002"])
        extractor = RuleBasedMemoryExtractor(lambda: next(id_counter))
        store = InMemoryMemoryStore()
        manager = MemoryManager(extractor, store)

        messages = [
            _msg("user", "记住偏好：用户喜欢使用 uv"),
            _msg("user", "记住事实：用户住在上海"),
            _msg("assistant", "助手消息"),  # 会被忽略
            _msg("user", "普通用户消息"),  # 会被忽略
        ]

        result = manager.extract_and_save("python-learning", messages)

        # 验证返回结果
        self.assertEqual(len(result), 2)

        # 验证第一条记忆
        pref = store.get("memory-001")
        self.assertEqual(pref.kind, "preference")
        self.assertEqual(pref.content, "用户喜欢使用 uv")
        self.assertEqual(pref.source_thread_id, "python-learning")

        # 验证第二条记忆
        fact = store.get("memory-002")
        self.assertEqual(fact.kind, "fact")
        self.assertEqual(fact.content, "用户住在上海")
        self.assertEqual(fact.source_thread_id, "python-learning")

    def test_integration_with_leading_whitespace(self):
        """测试带前导空白的真实提取。"""
        from mini_deerflow.memory_extractor import RuleBasedMemoryExtractor

        extractor = RuleBasedMemoryExtractor(lambda: "m1")
        store = InMemoryMemoryStore()
        manager = MemoryManager(extractor, store)

        messages = [
            _msg("user", "   记住事实：用户来自北京   "),
        ]

        result = manager.extract_and_save("thread-1", messages)

        self.assertEqual(len(result), 1)
        saved = store.get("m1")
        self.assertEqual(saved.kind, "fact")
        self.assertEqual(saved.content, "用户来自北京")

    def test_integration_conflict_detection(self):
        """测试真实场景下的冲突检测。"""
        from mini_deerflow.memory_extractor import RuleBasedMemoryExtractor

        # 使用固定 ID 工厂，每次返回相同的 ID 以便测试冲突
        extractor = RuleBasedMemoryExtractor(lambda: "fixed-id")
        store = InMemoryMemoryStore()
        manager = MemoryManager(extractor, store)

        # 第一次提取
        messages1 = [_msg("user", "记住事实：事实一")]
        result1 = manager.extract_and_save("thread-1", messages1)
        self.assertEqual(len(result1), 1)
        self.assertEqual(result1[0].memory_id, "fixed-id")

        # 第二次提取相同内容（ID 会重复，内容相同会被去重）
        # 由于去重后会检查 ID，但 ID 相同会被去重跳过...
        # 所以这里用不同的内容来测试 ID 冲突
        extractor2 = RuleBasedMemoryExtractor(lambda: "fixed-id")
        manager2 = MemoryManager(extractor2, store)
        messages2 = [_msg("user", "记住事实：不同事实")]
        # 这里会抛 MemoryAlreadyExistsError，因为 ID 相同
        with self.assertRaises(MemoryAlreadyExistsError):
            manager2.extract_and_save("thread-1", messages2)


class TestMemoryManagerDeduplication(unittest.TestCase):
    """记忆去重测试。"""

    def test_default_uses_exact_deduplicator(self):
        """默认使用 ExactMemoryDeduplicator。"""
        extractor = StubExtractor()
        store = InMemoryMemoryStore()
        manager = MemoryManager(extractor, store)
        from mini_deerflow.memory_dedup import ExactMemoryDeduplicator
        self.assertIsInstance(manager._deduplicator, ExactMemoryDeduplicator)

    def test_can_inject_custom_deduplicator(self):
        """可以注入自定义 MemoryDeduplicator。"""
        from mini_deerflow.memory_dedup import MemoryDeduplicator

        class NoOpDeduplicator:
            def filter_new(self, candidates, existing):
                return list(candidates)

        extractor = StubExtractor()
        store = InMemoryMemoryStore()
        manager = MemoryManager(extractor, store, deduplicator=NoOpDeduplicator())  # type: ignore
        self.assertIsInstance(manager._deduplicator, NoOpDeduplicator)

    def test_list_memories_called_at_most_once(self):
        """每次最多调用一次 list_memories。"""
        class CountingStore(InMemoryMemoryStore):
            def __init__(self):
                super().__init__()
                self.list_count = 0

            def list_memories(self):
                self.list_count += 1
                return super().list_memories()

        extractor = StubExtractor([_memory("m1", content="内容A")])
        store = CountingStore()
        manager = MemoryManager(extractor, store)

        manager.extract_and_save("thread-1", [])
        self.assertEqual(store.list_count, 1)

    def test_no_candidates_no_list_memories(self):
        """无候选时不调用 list_memories。"""
        class CountingStore(InMemoryMemoryStore):
            def __init__(self):
                super().__init__()
                self.list_count = 0

            def list_memories(self):
                self.list_count += 1
                return super().list_memories()

        extractor = StubExtractor([])
        store = CountingStore()
        manager = MemoryManager(extractor, store)

        manager.extract_and_save("thread-1", [])
        self.assertEqual(store.list_count, 0)

    def test_all_duplicates_no_add(self):
        """全部重复时不调用 add。"""
        class CountingStore(InMemoryMemoryStore):
            def __init__(self):
                super().__init__()
                self.add_count = 0
                self._initialized = False

            def add(self, memory):
                if self._initialized:
                    self.add_count += 1
                super().add(memory)

            def list_memories(self):
                self._initialized = True
                return super().list_memories()

        # Store 已有相同内容
        store = CountingStore()
        store.add(_memory("m0", kind="fact", content="用户喜欢 uv"))
        # 重置计数
        store.add_count = 0
        store._initialized = False

        # 新候选内容相同
        extractor = StubExtractor([_memory("m1", kind="fact", content="用户喜欢 UV")])
        manager = MemoryManager(extractor, store)

        result = manager.extract_and_save("thread-1", [])
        self.assertEqual(result, [])
        self.assertEqual(store.add_count, 0)

    def test_returns_only_truly_saved(self):
        """返回值只包含真正保存的 Memory。"""
        extractor = StubExtractor([
            _memory("m1", content="新内容A"),
            _memory("m2", content="新内容B"),
        ])
        store = InMemoryMemoryStore()
        # Store 已有部分内容
        store.add(_memory("m0", kind="fact", content="新内容A"))

        manager = MemoryManager(extractor, store)
        result = manager.extract_and_save("thread-1", [])

        # 只返回真正保存的（m2）
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].memory_id, "m2")

    def test_dedup_before_id_conflict_check(self):
        """去重发生在 ID 冲突检查之前。"""
        # Store 已有相同内容，但不同的 ID
        store = InMemoryMemoryStore()
        store.add(_memory("m0", kind="fact", content="内容"))

        # 新候选与已有内容相同，但 ID 也相同（会被去重跳过）
        extractor = StubExtractor([
            _memory("m0", kind="fact", content="内容"),  # 内容重复，会被去重
            _memory("m1", kind="fact", content="新内容"),  # 新内容
        ])
        manager = MemoryManager(extractor, store)

        # 应该成功保存 m1（m0 被去重跳过，不会触发 ID 冲突）
        result = manager.extract_and_save("thread-1", [])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].memory_id, "m1")

    def test_same_id_same_content_skipped(self):
        """相同 ID 相同内容可以作为重复跳过。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1", kind="fact", content="内容"))

        extractor = StubExtractor([_memory("m1", kind="fact", content="内容")])
        manager = MemoryManager(extractor, store)

        # 应该被去重跳过，不抛 ID 冲突
        result = manager.extract_and_save("thread-1", [])
        self.assertEqual(result, [])

    def test_same_id_different_content_still_conflicts(self):
        """相同 ID 不同内容仍抛 ID 冲突。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1", kind="fact", content="旧内容"))

        extractor = StubExtractor([_memory("m1", kind="fact", content="新内容")])
        manager = MemoryManager(extractor, store)

        with self.assertRaises(MemoryAlreadyExistsError):
            manager.extract_and_save("thread-1", [])

    def test_id_conflict_no_partial_write(self):
        """ID 冲突时不部分写入。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m0", kind="fact", content="已有"))

        extractor = StubExtractor([
            _memory("m1", content="新内容"),
            _memory("m0", kind="fact", content="触发冲突"),  # 会冲突
        ])
        manager = MemoryManager(extractor, store)

        try:
            manager.extract_and_save("thread-1", [])
        except MemoryAlreadyExistsError:
            pass

        # m1 不应该被保存
        self.assertFalse(store.exists("m1"))
        # m0 还在
        self.assertTrue(store.exists("m0"))


if __name__ == "__main__":
    unittest.main()
