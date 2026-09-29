"""记忆容量限制与 FIFO 淘汰测试（14E3 阶段）。"""

from __future__ import annotations

import unittest
from unittest import mock

from mini_deerflow.memory import (
    InMemoryMemoryStore,
    Memory,
    MemoryAlreadyExistsError,
    MemoryNotFoundError,
    MemoryStore,
)
from mini_deerflow.memory_manager import MemoryManager


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
    """桩提取器。"""

    def __init__(
        self,
        memories: list[Memory] | None = None,
        exception: Exception | None = None,
    ) -> None:
        self._memories = memories or []
        self._exception = exception
        self.calls: list[tuple[str, list]] = []

    def extract(self, source_thread_id: str, messages: list):
        self.calls.append((source_thread_id, list(messages)))
        if self._exception is not None:
            raise self._exception
        return list(self._memories)


# ==================== A. 参数校验 ====================


class TestMaxMemoriesValidation(unittest.TestCase):
    """max_memories 参数校验。"""

    def test_none_allowed(self):
        """None 允许。"""
        store = InMemoryMemoryStore(None)
        self.assertIsNone(store._max_memories)

    def test_positive_int_allowed(self):
        """正整数允许。"""
        store = InMemoryMemoryStore(10)
        self.assertEqual(store._max_memories, 10)

    def test_zero_rejected(self):
        """0 拒绝。"""
        with self.assertRaises(ValueError) as ctx:
            InMemoryMemoryStore(0)
        self.assertIn("max_memories", str(ctx.exception))

    def test_negative_rejected(self):
        """负数拒绝。"""
        with self.assertRaises(ValueError) as ctx:
            InMemoryMemoryStore(-1)
        self.assertIn("max_memories", str(ctx.exception))

    def test_bool_rejected(self):
        """bool 拒绝（bool 是 int 子类）。"""
        with self.assertRaises(TypeError):
            InMemoryMemoryStore(True)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            InMemoryMemoryStore(False)  # type: ignore[arg-type]

    def test_float_rejected(self):
        """float 拒绝。"""
        with self.assertRaises(TypeError):
            InMemoryMemoryStore(3.14)  # type: ignore[arg-type]

    def test_str_rejected(self):
        """str 拒绝。"""
        with self.assertRaises(TypeError):
            InMemoryMemoryStore("10")  # type: ignore[arg-type]


# ==================== B. 基本容量 ====================


class TestBasicCapacity(unittest.TestCase):
    """基本容量功能。"""

    def test_no_eviction_under_capacity(self):
        """未达到容量不淘汰。"""
        store = InMemoryMemoryStore(max_memories=3)
        store.add(_memory("a"))
        store.add(_memory("b"))
        self.assertEqual(len(store.list_memories()), 2)
        self.assertTrue(store.exists("a"))
        self.assertTrue(store.exists("b"))

    def test_eviction_when_full(self):
        """达到容量后新增淘汰最旧 Memory。"""
        store = InMemoryMemoryStore(max_memories=2)
        store.add(_memory("a"))
        store.add(_memory("b"))
        evicted = store.add(_memory("c"))  # 淘汰 a
        self.assertEqual(evicted.memory_id, "a")
        self.assertFalse(store.exists("a"))
        self.assertTrue(store.exists("b"))
        self.assertTrue(store.exists("c"))

    def test_add_returns_evicted_memory(self):
        """add 返回被淘汰的 Memory。"""
        store = InMemoryMemoryStore(max_memories=2)
        store.add(_memory("a"))
        store.add(_memory("b"))
        evicted = store.add(_memory("c"))
        self.assertIsInstance(evicted, Memory)
        self.assertEqual(evicted.memory_id, "a")

    def test_add_returns_none_when_no_eviction(self):
        """未淘汰时 add 返回 None。"""
        store = InMemoryMemoryStore(max_memories=3)
        store.add(_memory("a"))
        store.add(_memory("b"))
        result = store.add(_memory("c"))
        self.assertIsNone(result)

    def test_count_never_exceeds_max(self):
        """Store 数量始终不超过 max_memories。"""
        store = InMemoryMemoryStore(max_memories=2)
        for i in range(10):
            store.add(_memory(f"m{i}"))
        self.assertEqual(len(store.list_memories()), 2)

    def test_capacity_one(self):
        """容量 1 行为正确。"""
        store = InMemoryMemoryStore(max_memories=1)
        store.add(_memory("a"))
        self.assertTrue(store.exists("a"))
        evicted = store.add(_memory("b"))
        self.assertEqual(evicted.memory_id, "a")
        self.assertFalse(store.exists("a"))
        self.assertTrue(store.exists("b"))

    def test_no_limit_allows_unlimited(self):
        """无容量限制时可以无限添加。"""
        store = InMemoryMemoryStore(max_memories=None)
        for i in range(100):
            store.add(_memory(f"m{i}"))
        self.assertEqual(len(store.list_memories()), 100)


# ==================== C. FIFO 顺序 ====================


class TestFIFOOrder(unittest.TestCase):
    """FIFO 顺序维护。"""

    def test_consecutive_add_fifo(self):
        """连续新增按 FIFO 淘汰。"""
        store = InMemoryMemoryStore(max_memories=3)
        store.add(_memory("a"))
        store.add(_memory("b"))
        store.add(_memory("c"))
        store.add(_memory("d"))  # 淘汰 a
        store.add(_memory("e"))  # 淘汰 b
        self.assertEqual([m.memory_id for m in store.list_memories()], ["c", "d", "e"])

    def test_list_memories_sorting_unchanged(self):
        """list_memories 原有 memory_id 排序契约不变。"""
        store = InMemoryMemoryStore(max_memories=3)
        store.add(_memory("z"))
        store.add(_memory("a"))
        store.add(_memory("m"))
        store.add(_memory("b"))  # 淘汰 z
        result = store.list_memories()
        self.assertEqual([m.memory_id for m in result], ["a", "b", "m"])

    def test_get_does_not_change_order(self):
        """get 不改变淘汰顺序。"""
        store = InMemoryMemoryStore(max_memories=2)
        store.add(_memory("a"))
        store.add(_memory("b"))
        store.get("a")  # 不应改变顺序
        store.add(_memory("c"))  # 应淘汰 a
        self.assertFalse(store.exists("a"))
        self.assertTrue(store.exists("b"))
        self.assertTrue(store.exists("c"))

    def test_list_queries_do_not_change_order(self):
        """list 查询不改变淘汰顺序。"""
        store = InMemoryMemoryStore(max_memories=2)
        store.add(_memory("a", kind="fact"))
        store.add(_memory("b", kind="preference"))
        store.list_memories()
        store.list_by_kind("fact")
        store.list_by_source_thread("thread-1")
        store.add(_memory("c"))  # 应淘汰 a
        self.assertFalse(store.exists("a"))
        self.assertTrue(store.exists("b"))
        self.assertTrue(store.exists("c"))

    def test_failed_add_does_not_change_order(self):
        """失败的 add 不改变淘汰顺序。"""
        store = InMemoryMemoryStore(max_memories=2)
        store.add(_memory("a"))
        store.add(_memory("b"))
        with self.assertRaises(MemoryAlreadyExistsError):
            store.add(_memory("a"))  # 失败
        store.add(_memory("c"))  # 应淘汰 a，不是 b
        self.assertFalse(store.exists("a"))
        self.assertTrue(store.exists("b"))

    def test_failed_add_with_invalid_type_does_not_change_order(self):
        """非法类型的 add 失败不改变顺序。"""
        store = InMemoryMemoryStore(max_memories=2)
        store.add(_memory("a"))
        store.add(_memory("b"))
        with self.assertRaises(TypeError):
            store.add("not a memory")  # type: ignore
        store.add(_memory("c"))  # 应淘汰 a
        self.assertFalse(store.exists("a"))


# ==================== D. delete 和 update ====================


class TestDeleteAndUpdateWithCapacity(unittest.TestCase):
    """delete 和 update 与容量交互。"""

    def test_delete_frees_capacity(self):
        """delete 后释放容量。"""
        store = InMemoryMemoryStore(max_memories=2)
        store.add(_memory("a"))
        store.add(_memory("b"))
        store.delete("a")
        store.add(_memory("c"))  # 不应淘汰
        self.assertTrue(store.exists("b"))
        self.assertTrue(store.exists("c"))

    def test_delete_syncs_order(self):
        """delete 同步移除顺序记录。"""
        store = InMemoryMemoryStore(max_memories=3)
        store.add(_memory("a"))
        store.add(_memory("b"))
        store.add(_memory("c"))
        store.delete("a")  # 释放一个位置
        store.add(_memory("d"))  # 不应淘汰
        self.assertEqual(len(store.list_memories()), 3)
        self.assertTrue(store.exists("b"))
        self.assertTrue(store.exists("c"))
        self.assertTrue(store.exists("d"))

    def test_update_does_not_change_count(self):
        """update 不改变数量。"""
        store = InMemoryMemoryStore(max_memories=2)
        store.add(_memory("a"))
        store.add(_memory("b"))
        store.update(_memory("a", content="updated"))
        self.assertEqual(len(store.list_memories()), 2)

    def test_update_does_not_refresh_order(self):
        """update 不刷新顺序。"""
        store = InMemoryMemoryStore(max_memories=2)
        store.add(_memory("a"))
        store.add(_memory("b"))
        store.update(_memory("a", content="updated"))
        store.add(_memory("c"))  # 应淘汰 a（即使刚更新过）
        self.assertFalse(store.exists("a"))
        self.assertTrue(store.exists("b"))
        self.assertTrue(store.exists("c"))

    def test_updated_oldest_still_evicted_first(self):
        """更新后的最旧 Memory 仍然最先淘汰。"""
        store = InMemoryMemoryStore(max_memories=3)
        store.add(_memory("a"))
        store.add(_memory("b"))
        store.add(_memory("c"))
        store.update(_memory("a", content="new content"))
        store.add(_memory("d"))  # 淘汰 a
        self.assertFalse(store.exists("a"))
        self.assertTrue(store.exists("b"))
        self.assertTrue(store.exists("c"))
        self.assertTrue(store.exists("d"))

    def test_failed_update_does_not_change_order(self):
        """失败的 update 不改变顺序。"""
        store = InMemoryMemoryStore(max_memories=2)
        store.add(_memory("a"))
        store.add(_memory("b"))
        with self.assertRaises(MemoryNotFoundError):
            store.update(_memory("nonexistent"))
        store.add(_memory("c"))  # 应淘汰 a
        self.assertFalse(store.exists("a"))

    def test_failed_delete_does_not_change_order(self):
        """失败的 delete 不改变顺序。"""
        store = InMemoryMemoryStore(max_memories=2)
        store.add(_memory("a"))
        store.add(_memory("b"))
        with self.assertRaises(MemoryNotFoundError):
            store.delete("nonexistent")
        store.add(_memory("c"))  # 应淘汰 a
        self.assertFalse(store.exists("a"))


# ==================== E. 去重配合 ====================


class TestDedupWithCapacity(unittest.TestCase):
    """去重与容量交互。"""

    def test_duplicate_content_no_eviction(self):
        """重复内容不触发淘汰。"""
        store = InMemoryMemoryStore(max_memories=2)
        store.add(_memory("a", content="内容"))
        extractor = StubExtractor([_memory("b", content="内容")])  # 重复
        manager = MemoryManager(extractor, store)
        result = manager.extract_and_save("thread-1", [])
        self.assertEqual(result, [])  # 无新增
        self.assertEqual(len(store.list_memories()), 1)  # 不淘汰

    def test_batch_duplicate_uses_one_slot(self):
        """批次内重复只占一个容量。"""
        store = InMemoryMemoryStore(max_memories=2)
        extractor = StubExtractor([
            _memory("a", content="内容A"),
            _memory("b", content="内容A"),  # 与上一个重复
        ])
        manager = MemoryManager(extractor, store)
        result = manager.extract_and_save("thread-1", [])
        self.assertEqual(len(result), 1)  # 只新增一条
        self.assertEqual(len(store.list_memories()), 1)

    def test_different_kind_same_content_uses_separate_slot(self):
        """不同 kind 相同 content 分别占容量。"""
        store = InMemoryMemoryStore(max_memories=2)
        extractor = StubExtractor([
            _memory("a", kind="fact", content="内容"),
            _memory("b", kind="preference", content="内容"),  # 不同 kind
        ])
        manager = MemoryManager(extractor, store)
        result = manager.extract_and_save("thread-1", [])
        self.assertEqual(len(result), 2)
        self.assertEqual(len(store.list_memories()), 2)

    def test_id_conflict_no_eviction(self):
        """ID 冲突不触发淘汰（内容不同时）。"""
        store = InMemoryMemoryStore(max_memories=2)
        store.add(_memory("a"))
        extractor = StubExtractor([_memory("a", content="新内容")])  # ID 相同，内容不同
        manager = MemoryManager(extractor, store)
        with self.assertRaises(MemoryAlreadyExistsError):
            manager.extract_and_save("thread-1", [])
        self.assertEqual(len(store.list_memories()), 1)
        self.assertTrue(store.exists("a"))


# ==================== F. MemoryManager ====================


class TestMemoryManagerCapacity(unittest.TestCase):
    """MemoryManager 与容量交互。"""

    def test_returns_all_when_capacity_sufficient(self):
        """容量足够时返回全部新增 Memory。"""
        store = InMemoryMemoryStore(max_memories=5)
        extractor = StubExtractor([
            _memory("a", content="内容A"),
            _memory("b", content="内容B"),
        ])
        manager = MemoryManager(extractor, store)
        result = manager.extract_and_save("thread-1", [])
        self.assertEqual(len(result), 2)
        self.assertEqual({m.memory_id for m in result}, {"a", "b"})

    def test_returns_only_final_retained(self):
        """一次新增超过容量时只返回最终保留项。"""
        store = InMemoryMemoryStore(max_memories=3)
        extractor = StubExtractor([
            _memory("a", content="内容A"),
            _memory("b", content="内容B"),
            _memory("c", content="内容C"),
            _memory("d", content="内容D"),
            _memory("e", content="内容E"),
        ])
        manager = MemoryManager(extractor, store)
        result = manager.extract_and_save("thread-1", [])
        # 只有 c, d, e 最终保留
        self.assertEqual(len(result), 3)
        self.assertEqual({m.memory_id for m in result}, {"c", "d", "e"})

    def test_return_order_preserved(self):
        """返回顺序正确（与候选顺序一致）。"""
        store = InMemoryMemoryStore(max_memories=3)
        extractor = StubExtractor([
            _memory("a", content="内容A"),
            _memory("b", content="内容B"),
            _memory("c", content="内容C"),
            _memory("d", content="内容D"),
            _memory("e", content="内容E"),
        ])
        manager = MemoryManager(extractor, store)
        result = manager.extract_and_save("thread-1", [])
        # c, d, e 是最终保留的
        self.assertEqual([m.memory_id for m in result], ["c", "d", "e"])

    def test_evicted_batch_memory_not_in_result(self):
        """被淘汰的本批次 Memory 不在返回值中。"""
        store = InMemoryMemoryStore(max_memories=2)
        extractor = StubExtractor([
            _memory("a", content="内容A"),
            _memory("b", content="内容B"),
            _memory("c", content="内容C"),
        ])
        manager = MemoryManager(extractor, store)
        result = manager.extract_and_save("thread-1", [])
        self.assertNotIn("a", {m.memory_id for m in result})
        self.assertEqual({m.memory_id for m in result}, {"b", "c"})

    def test_unlimited_capacity_behavior_unchanged(self):
        """无容量限制时保持原有返回行为。"""
        store = InMemoryMemoryStore(max_memories=None)
        extractor = StubExtractor([
            _memory("a", content="内容A"),
            _memory("b", content="内容B"),
        ])
        manager = MemoryManager(extractor, store)
        result = manager.extract_and_save("thread-1", [])
        self.assertEqual(len(result), 2)


# ==================== G. Agent 集成 ====================


class TestAgentIntegration(unittest.TestCase):
    """Agent 集成测试（验证 MemoryManager 返回值）。"""

    def test_memory_saved_count_matches_final_count(self):
        """memory_saved count 等于最终保留数量。"""
        from mini_deerflow.memory_extractor import RuleBasedMemoryExtractor
        from mini_deerflow.models import Message

        store = InMemoryMemoryStore(max_memories=3)
        counter = iter(range(100))
        extractor = RuleBasedMemoryExtractor(lambda: f"m{next(counter)}")
        manager = MemoryManager(extractor, store)

        messages = [
            Message(role="user", content="记住事实：事实一"),
            Message(role="user", content="记住事实：事实二"),
            Message(role="user", content="记住事实：事实三"),
            Message(role="user", content="记住事实：事实四"),
            Message(role="user", content="记住事实：事实五"),
        ]

        result = manager.extract_and_save("thread-1", messages)
        self.assertEqual(len(result), 3)

    def test_fifo_eviction_no_warning(self):
        """FIFO 淘汰不产生 memory_warning。"""
        # 通过验证不抛出异常来隐式测试
        store = InMemoryMemoryStore(max_memories=2)
        extractor = StubExtractor([
            _memory("a", content="内容A"),
            _memory("b", content="内容B"),
            _memory("c", content="内容C"),
        ])
        manager = MemoryManager(extractor, store)
        result = manager.extract_and_save("thread-1", [])
        self.assertEqual(len(result), 2)  # 正常返回，无异常

    def test_only_retained_in_memory_ids(self):
        """memory_ids 只包含最终保留的 Memory。"""
        store = InMemoryMemoryStore(max_memories=2)
        extractor = StubExtractor([
            _memory("a", content="内容A"),
            _memory("b", content="内容B"),
            _memory("c", content="内容C"),
        ])
        manager = MemoryManager(extractor, store)
        result = manager.extract_and_save("thread-1", [])
        result_ids = {m.memory_id for m in result}
        self.assertEqual(result_ids, {"b", "c"})


if __name__ == "__main__":
    unittest.main()
