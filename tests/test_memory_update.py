"""记忆更新功能测试（14E2 阶段）。"""

from __future__ import annotations

import copy
import unittest
from unittest import mock

from mini_deerflow.memory import (
    InMemoryMemoryStore,
    Memory,
    MemoryAlreadyExistsError,
    MemoryContentConflictError,
    MemoryNotFoundError,
    MemoryStore,
)
from mini_deerflow.memory_dedup import ExactMemoryDeduplicator
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


# ==================== MemoryStore Protocol 测试 ====================


class TestMemoryStoreProtocolHasUpdate(unittest.TestCase):
    """验证 MemoryStore Protocol 包含 update 方法。"""

    def test_protocol_has_update(self):
        """MemoryStore Protocol 必须包含 update 方法。"""
        self.assertTrue(hasattr(MemoryStore, "update"))


class TestInMemoryMemoryStoreImplementsUpdate(unittest.TestCase):
    """验证 InMemoryMemoryStore 实现了 update 方法。"""

    def test_has_update_method(self):
        """InMemoryMemoryStore 有 update 方法。"""
        store = InMemoryMemoryStore()
        self.assertTrue(hasattr(store, "update"))


# ==================== InMemoryMemoryStore.update 测试 ====================


class TestInMemoryMemoryStoreUpdateBasic(unittest.TestCase):
    """update 基本功能测试。"""

    def test_normal_update_content(self):
        """正常更新记忆内容。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1", content="旧内容"))
        store.update(_memory("m1", content="新内容"))
        result = store.get("m1")
        self.assertEqual(result.content, "新内容")

    def test_normal_update_kind(self):
        """正常更新记忆类型。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1", kind="fact"))
        store.update(_memory("m1", kind="preference"))
        result = store.get("m1")
        self.assertEqual(result.kind, "preference")

    def test_normal_update_source_thread_id(self):
        """正常更新来源会话 ID。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1", source_thread_id="thread-a"))
        store.update(_memory("m1", source_thread_id="thread-b"))
        result = store.get("m1")
        self.assertEqual(result.source_thread_id, "thread-b")

    def test_memory_id_unchanged(self):
        """更新后 memory_id 保持不变。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1"))
        store.update(_memory("m1", content="新内容"))
        result = store.get("m1")
        self.assertEqual(result.memory_id, "m1")

    def test_update_multiple_fields(self):
        """更新多个字段。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1", kind="fact", content="旧", source_thread_id="t1"))
        store.update(_memory("m1", kind="preference", content="新", source_thread_id="t2"))
        result = store.get("m1")
        self.assertEqual(result.kind, "preference")
        self.assertEqual(result.content, "新")
        self.assertEqual(result.source_thread_id, "t2")


class TestInMemoryMemoryStoreUpdateGet(unittest.TestCase):
    """update 后 get 返回新对象。"""

    def test_get_returns_updated_object(self):
        """更新后 get 返回新对象。"""
        store = InMemoryMemoryStore()
        old_memory = _memory("m1", content="旧内容")
        store.add(old_memory)
        new_memory = _memory("m1", content="新内容")
        store.update(new_memory)
        result = store.get("m1")
        self.assertEqual(result.content, "新内容")


class TestInMemoryMemoryStoreUpdateList(unittest.TestCase):
    """update 后 list_* 方法返回新对象。"""

    def test_list_memories_returns_updated(self):
        """更新后 list_memories 返回新对象。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1", content="旧"))
        store.update(_memory("m1", content="新"))
        result = store.list_memories()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].content, "新")

    def test_list_by_kind_returns_updated(self):
        """更新后 list_by_kind 返回新对象。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1", kind="fact", content="旧"))
        store.update(_memory("m1", kind="fact", content="新"))
        result = store.list_by_kind("fact")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].content, "新")

    def test_list_by_kind_reflects_kind_change(self):
        """更新 kind 后 list_by_kind 反映变化。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1", kind="fact"))
        store.update(_memory("m1", kind="preference"))
        fact_result = store.list_by_kind("fact")
        pref_result = store.list_by_kind("preference")
        self.assertEqual(len(fact_result), 0)
        self.assertEqual(len(pref_result), 1)
        self.assertEqual(pref_result[0].memory_id, "m1")

    def test_list_by_source_thread_returns_updated(self):
        """更新后 list_by_source_thread 返回新对象。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1", source_thread_id="thread-a", content="旧"))
        store.update(_memory("m1", source_thread_id="thread-a", content="新"))
        result = store.list_by_source_thread("thread-a")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].content, "新")

    def test_list_by_source_thread_reflects_change(self):
        """更新 source_thread_id 后 list_by_source_thread 反映变化。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1", source_thread_id="thread-a"))
        store.update(_memory("m1", source_thread_id="thread-b"))
        thread_a_result = store.list_by_source_thread("thread-a")
        thread_b_result = store.list_by_source_thread("thread-b")
        self.assertEqual(len(thread_a_result), 0)
        self.assertEqual(len(thread_b_result), 1)
        self.assertEqual(thread_b_result[0].memory_id, "m1")


class TestInMemoryMemoryStoreUpdateOtherUnaffected(unittest.TestCase):
    """更新不影响其他 Memory。"""

    def test_other_memories_unchanged(self):
        """更新一个 Memory 不影响其他 Memory。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1", content="内容1"))
        store.add(_memory("m2", content="内容2"))
        store.update(_memory("m1", content="新内容1"))
        self.assertEqual(store.get("m2").content, "内容2")

    def test_total_count_unchanged(self):
        """更新后总数量不变。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1"))
        store.add(_memory("m2"))
        store.update(_memory("m1", content="新"))
        self.assertEqual(len(store.list_memories()), 2)


class TestInMemoryMemoryStoreUpdateErrors(unittest.TestCase):
    """update 错误处理。"""

    def test_nonexistent_id_raises_not_found(self):
        """更新不存在的 ID 抛出 MemoryNotFoundError。"""
        store = InMemoryMemoryStore()
        with self.assertRaises(MemoryNotFoundError) as ctx:
            store.update(_memory("nonexistent"))
        self.assertEqual(ctx.exception.memory_id, "nonexistent")

    def test_non_memory_raises_type_error(self):
        """参数不是 Memory 时抛出 TypeError。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1"))
        for bad in ("string", 42, None, {}, []):
            with self.assertRaises(TypeError):
                store.update(bad)  # type: ignore[arg-type]

    def test_error_message_contains_memory_id(self):
        """错误信息包含 memory_id。"""
        store = InMemoryMemoryStore()
        with self.assertRaises(MemoryNotFoundError) as ctx:
            store.update(_memory("my-id"))
        self.assertIn("my-id", str(ctx.exception))


class TestInMemoryMemoryStoreUpdateDoesNotModifyInput(unittest.TestCase):
    """update 不修改传入的 Memory。"""

    def test_input_memory_not_modified(self):
        """传入的 Memory 对象不被修改。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1", content="原始"))
        input_memory = _memory("m1", content="新内容")
        input_content_before = input_memory.content
        store.update(input_memory)
        self.assertEqual(input_memory.content, input_content_before)


# ==================== MemoryManager.update_memory 测试 ====================


class TestMemoryManagerUpdateMemory(unittest.TestCase):
    """MemoryManager.update_memory 基本功能。"""

    def test_normal_update(self):
        """正常更新记忆。"""
        extractor = unittest.mock.MagicMock()
        store = InMemoryMemoryStore()
        store.add(_memory("m1", content="旧"))
        manager = MemoryManager(extractor, store)
        new_memory = _memory("m1", content="新")
        result = manager.update_memory(new_memory)
        self.assertEqual(result.content, "新")
        self.assertEqual(store.get("m1").content, "新")

    def test_update_returns_new_memory(self):
        """update_memory 返回新 Memory。"""
        extractor = unittest.mock.MagicMock()
        store = InMemoryMemoryStore()
        store.add(_memory("m1"))
        manager = MemoryManager(extractor, store)
        new_memory = _memory("m1", content="新")
        result = manager.update_memory(new_memory)
        self.assertIs(result, new_memory)


class TestMemoryManagerUpdateMemoryIdempotent(unittest.TestCase):
    """幂等更新测试。"""

    def test_identical_memory_returns_old(self):
        """新旧对象相同时返回旧对象。"""
        extractor = unittest.mock.MagicMock()
        store = InMemoryMemoryStore()
        old_memory = _memory("m1", kind="fact", content="内容")
        store.add(old_memory)
        manager = MemoryManager(extractor, store)
        result = manager.update_memory(old_memory)
        self.assertIs(result, old_memory)

    def test_idempotent_no_list_memories(self):
        """幂等更新不调用 list_memories。"""
        class CountingStore(InMemoryMemoryStore):
            def __init__(self):
                super().__init__()
                self.list_count = 0

            def list_memories(self):
                self.list_count += 1
                return super().list_memories()

        extractor = unittest.mock.MagicMock()
        store = CountingStore()
        memory = _memory("m1")
        store.add(memory)
        manager = MemoryManager(extractor, store)
        manager.update_memory(memory)
        self.assertEqual(store.list_count, 0)

    def test_idempotent_no_deduplicator(self):
        """幂等更新不调用 deduplicator。"""
        class CountingDeduplicator:
            def __init__(self):
                self.filter_count = 0

            def filter_new(self, candidates, existing):
                self.filter_count += 1
                return list(candidates)

        extractor = unittest.mock.MagicMock()
        store = InMemoryMemoryStore()
        memory = _memory("m1")
        store.add(memory)
        dedup = CountingDeduplicator()
        manager = MemoryManager(extractor, store, deduplicator=dedup)
        manager.update_memory(memory)
        self.assertEqual(dedup.filter_count, 0)

    def test_idempotent_no_store_update(self):
        """幂等更新不调用 store.update。"""
        class CountingStore(InMemoryMemoryStore):
            def __init__(self):
                super().__init__()
                self.update_count = 0

            def update(self, memory):
                self.update_count += 1
                super().update(memory)

        extractor = unittest.mock.MagicMock()
        store = CountingStore()
        memory = _memory("m1")
        store.add(memory)
        manager = MemoryManager(extractor, store)
        manager.update_memory(memory)
        self.assertEqual(store.update_count, 0)


class TestMemoryManagerUpdateMemoryContentConflict(unittest.TestCase):
    """内容冲突测试。"""

    def test_duplicate_content_raises_conflict(self):
        """新内容与其他 Memory 重复时抛出 MemoryContentConflictError。"""
        extractor = unittest.mock.MagicMock()
        store = InMemoryMemoryStore()
        store.add(_memory("m1", kind="fact", content="用户喜欢 uv"))
        store.add(_memory("m2", kind="fact", content="其他内容"))
        manager = MemoryManager(extractor, store)

        # 新内容和 m1 重复
        new_memory = _memory("m2", kind="fact", content="用户喜欢 UV")
        with self.assertRaises(MemoryContentConflictError) as ctx:
            manager.update_memory(new_memory)
        self.assertEqual(ctx.exception.memory_id, "m2")

    def test_conflict_preserves_old_data(self):
        """冲突时旧数据保持不变。"""
        extractor = unittest.mock.MagicMock()
        store = InMemoryMemoryStore()
        m1 = _memory("m1", kind="fact", content="用户喜欢 uv")
        m2 = _memory("m2", kind="fact", content="其他内容")
        store.add(m1)
        store.add(m2)
        manager = MemoryManager(extractor, store)

        try:
            manager.update_memory(_memory("m2", kind="fact", content="用户喜欢 UV"))
        except MemoryContentConflictError:
            pass

        # 两条旧 Memory 都保持不变
        self.assertEqual(store.get("m1").content, "用户喜欢 uv")
        self.assertEqual(store.get("m2").content, "其他内容")

    def test_different_kind_same_content_allowed(self):
        """不同 kind 相同 content 允许更新。"""
        extractor = unittest.mock.MagicMock()
        store = InMemoryMemoryStore()
        store.add(_memory("m1", kind="fact", content="用户喜欢 poetry"))
        manager = MemoryManager(extractor, store)
        result = manager.update_memory(
            _memory("m1", kind="preference", content="用户喜欢 poetry")
        )
        self.assertEqual(result.kind, "preference")
        self.assertEqual(store.get("m1").kind, "preference")

    def test_source_thread_id_not_in_dedup(self):
        """source_thread_id 不参与内容去重。"""
        extractor = unittest.mock.MagicMock()
        store = InMemoryMemoryStore()
        store.add(_memory("m1", kind="fact", content="内容", source_thread_id="thread-a"))
        manager = MemoryManager(extractor, store)
        # 相同内容但不同 source_thread_id，不应冲突
        result = manager.update_memory(
            _memory("m1", kind="fact", content="内容", source_thread_id="thread-b")
        )
        self.assertEqual(result.content, "内容")
        self.assertEqual(result.source_thread_id, "thread-b")


class TestMemoryManagerUpdateMemoryExceptionPropagation(unittest.TestCase):
    """异常传播测试。"""

    def test_store_get_exception_propagates(self):
        """store.get 异常原样传播。"""
        class FailingStore(InMemoryMemoryStore):
            def get(self, memory_id):
                raise RuntimeError("store get error")

        extractor = unittest.mock.MagicMock()
        store = FailingStore()
        manager = MemoryManager(extractor, store)

        with self.assertRaises(RuntimeError) as ctx:
            manager.update_memory(_memory("m1"))
        self.assertEqual(str(ctx.exception), "store get error")

    def test_store_list_memories_exception_propagates(self):
        """store.list_memories 异常原样传播。"""
        class FailingStore(InMemoryMemoryStore):
            def __init__(self):
                self._memories = {"m1": _memory("m1")}

            def list_memories(self):
                raise RuntimeError("list error")

        extractor = unittest.mock.MagicMock()
        store = FailingStore()
        manager = MemoryManager(extractor, store)

        with self.assertRaises(RuntimeError) as ctx:
            manager.update_memory(_memory("m1", content="新"))
        self.assertEqual(str(ctx.exception), "list error")

    def test_deduplicator_exception_propagates(self):
        """deduplicator 异常原样传播。"""
        class FailingDeduplicator:
            def filter_new(self, candidates, existing):
                raise RuntimeError("dedup error")

        extractor = unittest.mock.MagicMock()
        store = InMemoryMemoryStore()
        store.add(_memory("m1"))
        dedup = FailingDeduplicator()
        manager = MemoryManager(extractor, store, deduplicator=dedup)

        with self.assertRaises(RuntimeError) as ctx:
            manager.update_memory(_memory("m1", content="新"))
        self.assertEqual(str(ctx.exception), "dedup error")

    def test_store_update_exception_propagates(self):
        """store.update 异常原样传播。"""
        class FailingStore(InMemoryMemoryStore):
            def __init__(self):
                self._memories = {"m1": _memory("m1")}

            def update(self, memory):
                raise RuntimeError("update error")

        extractor = unittest.mock.MagicMock()
        store = FailingStore()
        manager = MemoryManager(extractor, store)

        with self.assertRaises(RuntimeError) as ctx:
            manager.update_memory(_memory("m1", content="新"))
        self.assertEqual(str(ctx.exception), "update error")


class TestMemoryManagerUpdateMemoryEdgeCases(unittest.TestCase):
    """边界情况测试。"""

    def test_original_memory_not_modified(self):
        """原 Memory 对象不被修改。"""
        extractor = unittest.mock.MagicMock()
        store = InMemoryMemoryStore()
        store.add(_memory("m1", content="旧"))
        manager = MemoryManager(extractor, store)

        new_memory = _memory("m1", content="新")
        original_content = new_memory.content

        try:
            manager.update_memory(new_memory)
        except MemoryContentConflictError:
            pass

        self.assertEqual(new_memory.content, original_content)

    def test_update_does_not_add_second_memory(self):
        """更新不会新增第二条 Memory。"""
        extractor = unittest.mock.MagicMock()
        store = InMemoryMemoryStore()
        store.add(_memory("m1", content="旧"))
        manager = MemoryManager(extractor, store)
        manager.update_memory(_memory("m1", content="新"))
        self.assertEqual(len(store.list_memories()), 1)

    def test_old_content_can_be_readded(self):
        """更新后旧内容可以重新添加。"""
        extractor = unittest.mock.MagicMock()
        store = InMemoryMemoryStore()
        store.add(_memory("m1", kind="fact", content="内容A"))
        manager = MemoryManager(extractor, store)

        # 更新 m1 为内容B
        manager.update_memory(_memory("m1", kind="fact", content="内容B"))
        self.assertEqual(store.get("m1").content, "内容B")

        # 现在可以添加一个不同 ID 的相同内容
        store.add(_memory("m2", kind="fact", content="内容A"))
        self.assertEqual(store.get("m2").content, "内容A")

    def test_updated_content_blocks_readding(self):
        """更新后的内容可以阻止重复新增。"""
        extractor = unittest.mock.MagicMock()
        store = InMemoryMemoryStore()
        store.add(_memory("m1", kind="fact", content="内容A"))
        manager = MemoryManager(extractor, store)

        # 更新 m1 为内容B
        manager.update_memory(_memory("m1", kind="fact", content="内容B"))

        # 现在内容A可以添加新记忆
        extractor.extract.return_value = [_memory("m2", kind="fact", content="内容A")]
        result = manager.extract_and_save("thread-1", [])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].memory_id, "m2")


if __name__ == "__main__":
    unittest.main()
