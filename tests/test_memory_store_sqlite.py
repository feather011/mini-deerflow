"""SQLiteMemoryStore 单元测试。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from mini_deerflow.memory import (
    Memory,
    MemoryAlreadyExistsError,
    MemoryNotFoundError,
    MemoryStore,
)
from mini_deerflow.memory_store_sqlite import (
    MemoryDataCorruptedError,
    MemoryStoreClosedError,
    SQLiteMemoryStore,
)


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


class TestSQLiteMemoryStoreBasic(unittest.TestCase):
    """基础行为测试。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "store.db"
        self.store = SQLiteMemoryStore(self.path)
        self.addCleanup(self.store.close)

    def test_init_creates_table(self):
        """初始化自动建表。"""
        conn = sqlite3.connect(self.path)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='memories'"
            ).fetchone()
            self.assertIsNotNone(row)
        finally:
            conn.close()

    def test_add_and_get(self):
        """add + get。"""
        m = _memory("m1", content="第一条记忆")
        self.store.add(m)
        result = self.store.get("m1")
        self.assertEqual(result.memory_id, "m1")
        self.assertEqual(result.content, "第一条记忆")

    def test_exists(self):
        """exists。"""
        self.store.add(_memory("m1"))
        self.assertTrue(self.store.exists("m1"))
        self.assertFalse(self.store.exists("nonexistent"))

    def test_delete(self):
        """delete。"""
        self.store.add(_memory("m1"))
        self.store.delete("m1")
        self.assertFalse(self.store.exists("m1"))

    def test_update(self):
        """update。"""
        self.store.add(_memory("m1", content="旧内容"))
        self.store.update(_memory("m1", content="新内容"))
        result = self.store.get("m1")
        self.assertEqual(result.content, "新内容")

    def test_list_memories(self):
        """list_memories。"""
        self.store.add(_memory("m1"))
        self.store.add(_memory("m2"))
        self.store.add(_memory("m3"))
        result = self.store.list_memories()
        self.assertEqual(len(result), 3)

    def test_list_by_kind(self):
        """list_by_kind。"""
        self.store.add(_memory("m1", kind="fact"))
        self.store.add(_memory("m2", kind="preference"))
        self.store.add(_memory("m3", kind="fact"))
        result = self.store.list_by_kind("fact")
        self.assertEqual(len(result), 2)
        self.assertTrue(all(m.kind == "fact" for m in result))

    def test_list_by_source_thread(self):
        """list_by_source_thread。"""
        self.store.add(_memory("m1", source_thread_id="thread-a"))
        self.store.add(_memory("m2", source_thread_id="thread-b"))
        self.store.add(_memory("m3", source_thread_id="thread-a"))
        result = self.store.list_by_source_thread("thread-a")
        self.assertEqual(len(result), 2)
        self.assertTrue(
            all(m.source_thread_id == "thread-a" for m in result)
        )

    def test_chinese_content(self):
        """中文内容。"""
        self.store.add(_memory("m1", content="中文内容测试"))
        result = self.store.get("m1")
        self.assertEqual(result.content, "中文内容测试")

    def test_multiple_memories_isolated(self):
        """多条记忆隔离。"""
        self.store.add(_memory("a", content="记忆A"))
        self.store.add(_memory("b", content="记忆B"))
        self.assertEqual(self.store.get("a").content, "记忆A")
        self.assertEqual(self.store.get("b").content, "记忆B")


class TestSQLiteMemoryStoreExceptions(unittest.TestCase):
    """异常测试。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "store.db"
        self.store = SQLiteMemoryStore(self.path)
        self.addCleanup(self.store.close)

    def test_add_non_memory_raises(self):
        """add 非 Memory 抛 TypeError。"""
        for bad in ("string", 42, None, {}, []):
            with self.assertRaises(TypeError):
                self.store.add(bad)  # type: ignore[arg-type]

    def test_update_non_memory_raises(self):
        """update 非 Memory 抛 TypeError。"""
        self.store.add(_memory("m1"))
        for bad in ("string", 42, None, {}, []):
            with self.assertRaises(TypeError):
                self.store.update(bad)  # type: ignore[arg-type]

    def test_duplicate_id_raises(self):
        """重复 ID 抛 MemoryAlreadyExistsError。"""
        self.store.add(_memory("m1"))
        with self.assertRaises(MemoryAlreadyExistsError) as ctx:
            self.store.add(_memory("m1"))
        self.assertEqual(ctx.exception.memory_id, "m1")

    def test_get_unknown_raises(self):
        """get 不存在 ID 抛 MemoryNotFoundError。"""
        with self.assertRaises(MemoryNotFoundError) as ctx:
            self.store.get("nonexistent")
        self.assertEqual(ctx.exception.memory_id, "nonexistent")

    def test_update_unknown_raises(self):
        """update 不存在 ID 抛 MemoryNotFoundError。"""
        with self.assertRaises(MemoryNotFoundError) as ctx:
            self.store.update(_memory("nonexistent"))
        self.assertEqual(ctx.exception.memory_id, "nonexistent")

    def test_delete_unknown_raises(self):
        """delete 不存在 ID 抛 MemoryNotFoundError。"""
        with self.assertRaises(MemoryNotFoundError) as ctx:
            self.store.delete("nonexistent")
        self.assertEqual(ctx.exception.memory_id, "nonexistent")

    def test_invalid_max_memories_zero(self):
        """max_memories=0 抛 ValueError。"""
        with self.assertRaises(ValueError):
            SQLiteMemoryStore(self.path, max_memories=0)

    def test_invalid_max_memories_negative(self):
        """max_memories 负数抛 ValueError。"""
        with self.assertRaises(ValueError):
            SQLiteMemoryStore(self.path, max_memories=-1)

    def test_invalid_max_memories_bool(self):
        """max_memories bool 抛 TypeError。"""
        with self.assertRaises(TypeError):
            SQLiteMemoryStore(self.path, max_memories=True)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            SQLiteMemoryStore(self.path, max_memories=False)  # type: ignore[arg-type]

    def test_invalid_max_memories_float(self):
        """max_memories float 抛 TypeError。"""
        with self.assertRaises(TypeError):
            SQLiteMemoryStore(self.path, max_memories=3.14)  # type: ignore[arg-type]

    def test_invalid_max_memories_str(self):
        """max_memories str 抛 TypeError。"""
        with self.assertRaises(TypeError):
            SQLiteMemoryStore(self.path, max_memories="10")  # type: ignore[arg-type]


class TestSQLiteMemoryStorePersistence(unittest.TestCase):
    """持久化测试。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "store.db"

    def test_close_reopen_reads_memories(self):
        """close 后重新打开仍能读取。"""
        store1 = SQLiteMemoryStore(self.path)
        store1.add(_memory("m1", content="内容"))
        store1.close()

        store2 = SQLiteMemoryStore(self.path)
        result = store2.get("m1")
        self.assertEqual(result.content, "内容")
        store2.close()

    def test_update_persists_across_restart(self):
        """更新跨重启保留。"""
        store1 = SQLiteMemoryStore(self.path)
        store1.add(_memory("m1", content="旧内容"))
        store1.update(_memory("m1", content="新内容"))
        store1.close()

        store2 = SQLiteMemoryStore(self.path)
        result = store2.get("m1")
        self.assertEqual(result.content, "新内容")
        store2.close()

    def test_delete_persists_across_restart(self):
        """删除跨重启保留。"""
        store1 = SQLiteMemoryStore(self.path)
        store1.add(_memory("m1"))
        store1.delete("m1")
        store1.close()

        store2 = SQLiteMemoryStore(self.path)
        self.assertFalse(store2.exists("m1"))
        store2.close()


class TestSQLiteMemoryStoreCapacity(unittest.TestCase):
    """容量与 FIFO 测试。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "store.db"

    def test_unlimited_no_eviction(self):
        """无限容量不淘汰。"""
        store = SQLiteMemoryStore(self.path, max_memories=None)
        for i in range(10):
            store.add(_memory(f"m{i}"))
        self.assertEqual(len(store.list_memories()), 10)
        store.close()

    def test_returns_none_when_no_eviction(self):
        """容量未满返回 None。"""
        store = SQLiteMemoryStore(self.path, max_memories=3)
        result = store.add(_memory("m1"))
        self.assertIsNone(result)
        store.close()

    def test_eviction_returns_oldest(self):
        """容量已满返回最旧 Memory。"""
        store = SQLiteMemoryStore(self.path, max_memories=2)
        store.add(_memory("a"))
        store.add(_memory("b"))
        evicted = store.add(_memory("c"))
        self.assertEqual(evicted.memory_id, "a")
        self.assertFalse(store.exists("a"))
        store.close()

    def test_update_does_not_refresh_order(self):
        """update 不刷新 FIFO 顺序。"""
        store = SQLiteMemoryStore(self.path, max_memories=2)
        store.add(_memory("a"))
        store.add(_memory("b"))
        store.update(_memory("a", content="updated"))
        evicted = store.add(_memory("c"))
        # a 仍然是最旧的，应该被淘汰
        self.assertEqual(evicted.memory_id, "a")
        store.close()

    def test_get_does_not_refresh_order(self):
        """get 不刷新 FIFO 顺序。"""
        store = SQLiteMemoryStore(self.path, max_memories=2)
        store.add(_memory("a"))
        store.add(_memory("b"))
        store.get("a")  # 不改变顺序
        evicted = store.add(_memory("c"))
        self.assertEqual(evicted.memory_id, "a")
        store.close()

    def test_list_does_not_refresh_order(self):
        """list 不刷新 FIFO 顺序。"""
        store = SQLiteMemoryStore(self.path, max_memories=2)
        store.add(_memory("a"))
        store.add(_memory("b"))
        store.list_memories()  # 不改变顺序
        evicted = store.add(_memory("c"))
        self.assertEqual(evicted.memory_id, "a")
        store.close()

    def test_delete_corrects_order(self):
        """delete 后顺序正确。"""
        store = SQLiteMemoryStore(self.path, max_memories=3)
        store.add(_memory("a"))
        store.add(_memory("b"))
        store.add(_memory("c"))
        store.delete("a")  # 删除 a
        store.add(_memory("d"))  # 不应淘汰
        self.assertTrue(store.exists("b"))
        self.assertTrue(store.exists("c"))
        self.assertTrue(store.exists("d"))
        store.close()

    def test_fifo_order_persists_across_restart(self):
        """FIFO 顺序跨重启保持。"""
        store1 = SQLiteMemoryStore(self.path, max_memories=3)
        store1.add(_memory("a"))
        store1.add(_memory("b"))
        store1.add(_memory("c"))
        store1.close()

        store2 = SQLiteMemoryStore(self.path, max_memories=3)
        store2.add(_memory("d"))  # 应淘汰 a
        self.assertFalse(store2.exists("a"))
        self.assertTrue(store2.exists("b"))
        self.assertTrue(store2.exists("c"))
        self.assertTrue(store2.exists("d"))
        store2.close()

    def test_update_order_persists_across_restart(self):
        """update 后的顺序跨重启保持。"""
        store1 = SQLiteMemoryStore(self.path, max_memories=2)
        store1.add(_memory("a"))
        store1.add(_memory("b"))
        store1.update(_memory("a", content="updated"))
        store1.close()

        store2 = SQLiteMemoryStore(self.path, max_memories=2)
        store2.add(_memory("c"))  # 应淘汰 a（即使刚更新过）
        self.assertFalse(store2.exists("a"))
        self.assertTrue(store2.exists("b"))
        self.assertTrue(store2.exists("c"))
        store2.close()

    def test_duplicate_id_no_eviction(self):
        """重复 ID 失败时不淘汰。"""
        store = SQLiteMemoryStore(self.path, max_memories=2)
        store.add(_memory("a"))
        store.add(_memory("b"))
        try:
            store.add(_memory("a"))
        except MemoryAlreadyExistsError:
            pass
        evicted = store.add(_memory("c"))
        # 应淘汰 a，不是 b
        self.assertEqual(evicted.memory_id, "a")
        store.close()

    def test_capacity_one(self):
        """max_memories=1。"""
        store = SQLiteMemoryStore(self.path, max_memories=1)
        store.add(_memory("a"))
        evicted = store.add(_memory("b"))
        self.assertEqual(evicted.memory_id, "a")
        self.assertEqual(len(store.list_memories()), 1)
        store.close()

    def test_sequential_evictions(self):
        """多次连续淘汰。"""
        store = SQLiteMemoryStore(self.path, max_memories=2)
        store.add(_memory("a"))
        store.add(_memory("b"))
        evicted1 = store.add(_memory("c"))
        self.assertEqual(evicted1.memory_id, "a")
        evicted2 = store.add(_memory("d"))
        self.assertEqual(evicted2.memory_id, "b")
        self.assertEqual({m.memory_id for m in store.list_memories()}, {"c", "d"})
        store.close()


class TestSQLiteMemoryStoreProtocol(unittest.TestCase):
    """Protocol 兼容性测试。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "store.db"

    def test_implements_protocol(self):
        """满足 MemoryStore Protocol。"""
        store: MemoryStore = SQLiteMemoryStore(self.path)
        self.assertTrue(hasattr(store, "add"))
        self.assertTrue(hasattr(store, "get"))
        self.assertTrue(hasattr(store, "update"))
        self.assertTrue(hasattr(store, "delete"))
        self.assertTrue(hasattr(store, "list_memories"))
        self.assertTrue(hasattr(store, "list_by_kind"))
        self.assertTrue(hasattr(store, "list_by_source_thread"))
        self.assertTrue(hasattr(store, "exists"))
        store.close()

    def test_add_returns_memory_or_none(self):
        """add 返回 Memory | None。"""
        store = SQLiteMemoryStore(self.path, max_memories=2)
        result = store.add(_memory("m1"))
        self.assertIsNone(result)
        evicted = store.add(_memory("m2"))
        self.assertIsNone(evicted)  # 容量刚好满，未触发淘汰
        evicted2 = store.add(_memory("m3"))
        self.assertIsInstance(evicted2, Memory)  # 超过容量，应该淘汰
        store.close()


class TestSQLiteMemoryStoreManager(unittest.TestCase):
    """MemoryManager 集成测试。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "store.db"

    def test_works_with_memory_manager(self):
        """MemoryManager 能直接搭配 SQLiteMemoryStore 使用。"""
        from mini_deerflow.memory_manager import MemoryManager

        class StubExtractor:
            def __init__(self, memories):
                self._memories = memories

            def extract(self, source_thread_id, messages):
                return list(self._memories)

        store = SQLiteMemoryStore(self.path)
        manager = MemoryManager(StubExtractor([
            _memory("a", content="内容A"),
            _memory("b", content="内容B"),
        ]), store)
        result = manager.extract_and_save("thread-1", [])
        self.assertEqual(len(result), 2)
        self.assertEqual(store.get("a").content, "内容A")
        store.close()


class TestSQLiteMemoryStoreContextManager(unittest.TestCase):
    """上下文管理器测试。"""

    def test_context_manager(self):
        """支持上下文管理器。"""
        path = Path(tempfile.gettempdir()) / "test_context.db"
        try:
            with SQLiteMemoryStore(path) as store:
                store.add(_memory("m1"))
                self.assertTrue(store.exists("m1"))
            # 退出后应该可以重新打开
            with SQLiteMemoryStore(path) as store:
                self.assertTrue(store.exists("m1"))
        finally:
            if path.exists():
                path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
