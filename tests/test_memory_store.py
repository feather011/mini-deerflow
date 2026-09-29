"""MemoryStore 接口与 InMemoryMemoryStore 实现测试。"""

from __future__ import annotations

import unittest

from mini_deerflow.memory import (
    InMemoryMemoryStore,
    Memory,
    MemoryAlreadyExistsError,
    MemoryNotFoundError,
    MemoryStore,
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


class TestMemoryStoreInterface(unittest.TestCase):
    """验证 InMemoryMemoryStore 实现了 MemoryStore Protocol。"""

    def test_implements_protocol(self):
        """InMemoryMemoryStore 必须实现 MemoryStore 协议所有方法。"""
        store: MemoryStore = InMemoryMemoryStore()
        self.assertTrue(hasattr(store, "add"))
        self.assertTrue(hasattr(store, "get"))
        self.assertTrue(hasattr(store, "delete"))
        self.assertTrue(hasattr(store, "list_memories"))
        self.assertTrue(hasattr(store, "list_by_kind"))
        self.assertTrue(hasattr(store, "list_by_source_thread"))
        self.assertTrue(hasattr(store, "exists"))


class TestInMemoryMemoryStoreAddGet(unittest.TestCase):
    """add 和 get 基本功能。"""

    def test_add_and_get(self):
        """添加后可以正确获取。"""
        store = InMemoryMemoryStore()
        m = _memory("m1", content="第一条记忆")
        store.add(m)
        result = store.get("m1")
        self.assertEqual(result, m)
        self.assertEqual(result.content, "第一条记忆")

    def test_add_multiple_memories(self):
        """添加多条记忆。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1", content="记忆一"))
        store.add(_memory("m2", content="记忆二"))
        store.add(_memory("m3", content="记忆三"))
        self.assertEqual(len(store.list_memories()), 3)

    def test_duplicate_memory_id(self):
        """重复 memory_id 抛出 MemoryAlreadyExistsError。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1"))
        with self.assertRaises(MemoryAlreadyExistsError) as ctx:
            store.add(_memory("m1"))
        self.assertEqual(ctx.exception.memory_id, "m1")

    def test_get_nonexistent(self):
        """获取不存在的记忆抛出 MemoryNotFoundError。"""
        store = InMemoryMemoryStore()
        with self.assertRaises(MemoryNotFoundError) as ctx:
            store.get("nonexistent")
        self.assertEqual(ctx.exception.memory_id, "nonexistent")


class TestInMemoryMemoryStoreDelete(unittest.TestCase):
    """delete 操作。"""

    def test_delete_existing(self):
        """删除已存在的记忆。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1"))
        store.delete("m1")
        self.assertFalse(store.exists("m1"))
        self.assertEqual(len(store.list_memories()), 0)

    def test_delete_nonexistent(self):
        """删除不存在的记忆抛出 MemoryNotFoundError。"""
        store = InMemoryMemoryStore()
        with self.assertRaises(MemoryNotFoundError) as ctx:
            store.delete("nonexistent")
        self.assertEqual(ctx.exception.memory_id, "nonexistent")


class TestInMemoryMemoryStoreExists(unittest.TestCase):
    """exists 操作。"""

    def test_exists_true(self):
        """存在的记忆返回 True。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1"))
        self.assertTrue(store.exists("m1"))

    def test_exists_false(self):
        """不存在的记忆返回 False。"""
        store = InMemoryMemoryStore()
        self.assertFalse(store.exists("nonexistent"))


class TestInMemoryMemoryStoreListMemories(unittest.TestCase):
    """list_memories 操作。"""

    def test_list_memories_sorted(self):
        """list_memories 按 memory_id 排序。"""
        store = InMemoryMemoryStore()
        store.add(_memory("z-memory"))
        store.add(_memory("a-memory"))
        store.add(_memory("m-memory"))
        result = store.list_memories()
        self.assertEqual(
            [m.memory_id for m in result],
            ["a-memory", "m-memory", "z-memory"],
        )

    def test_empty_store(self):
        """空 Store 返回空列表。"""
        store = InMemoryMemoryStore()
        self.assertEqual(store.list_memories(), [])


class TestInMemoryMemoryStoreListByKind(unittest.TestCase):
    """list_by_kind 操作。"""

    def test_list_by_kind(self):
        """只返回指定类型的记忆。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1", kind="fact"))
        store.add(_memory("m2", kind="preference"))
        store.add(_memory("m3", kind="fact"))
        store.add(_memory("m4", kind="instruction"))

        fact_memories = store.list_by_kind("fact")
        self.assertEqual(len(fact_memories), 2)
        self.assertTrue(all(m.kind == "fact" for m in fact_memories))

    def test_list_by_kind_sorted(self):
        """list_by_kind 结果按 memory_id 排序。"""
        store = InMemoryMemoryStore()
        store.add(_memory("z-m", kind="fact"))
        store.add(_memory("a-m", kind="fact"))
        result = store.list_by_kind("fact")
        self.assertEqual([m.memory_id for m in result], ["a-m", "z-m"])

    def test_list_by_kind_empty(self):
        """指定 kind 无记忆时返回空列表。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1", kind="fact"))
        self.assertEqual(store.list_by_kind("preference"), [])


class TestInMemoryMemoryStoreListBySourceThread(unittest.TestCase):
    """list_by_source_thread 操作。"""

    def test_list_by_source_thread(self):
        """只返回指定来源会话的记忆。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1", source_thread_id="thread-a"))
        store.add(_memory("m2", source_thread_id="thread-b"))
        store.add(_memory("m3", source_thread_id="thread-a"))

        thread_a_memories = store.list_by_source_thread("thread-a")
        self.assertEqual(len(thread_a_memories), 2)
        self.assertTrue(all(m.source_thread_id == "thread-a" for m in thread_a_memories))

    def test_list_by_source_thread_sorted(self):
        """list_by_source_thread 结果按 memory_id 排序。"""
        store = InMemoryMemoryStore()
        store.add(_memory("z-m", source_thread_id="t1"))
        store.add(_memory("a-m", source_thread_id="t1"))
        result = store.list_by_source_thread("t1")
        self.assertEqual([m.memory_id for m in result], ["a-m", "z-m"])

    def test_list_by_source_thread_empty(self):
        """指定来源会话无记忆时返回空列表。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1", source_thread_id="thread-a"))
        self.assertEqual(store.list_by_source_thread("thread-b"), [])


class TestInMemoryMemoryStoreTypeChecks(unittest.TestCase):
    """类型检查。"""

    def test_non_memory_input(self):
        """add 只能接受 Memory 类型。"""
        store = InMemoryMemoryStore()
        for bad in ("string", 42, None, {}, []):
            with self.assertRaises(TypeError):
                store.add(bad)  # type: ignore[arg-type]


class TestInMemoryMemoryStoreIsolation(unittest.TestCase):
    """隔离性测试。"""

    def test_returned_list_does_not_affect_store(self):
        """返回的列表修改不影响 Store 内部状态。"""
        store = InMemoryMemoryStore()
        store.add(_memory("m1"))

        memories = store.list_memories()
        memories.clear()

        self.assertEqual(len(store.list_memories()), 1)

    def test_different_store_instances_isolated(self):
        """不同 Store 实例互相隔离。"""
        store_a = InMemoryMemoryStore()
        store_b = InMemoryMemoryStore()

        store_a.add(_memory("m1"))

        self.assertTrue(store_a.exists("m1"))
        self.assertFalse(store_b.exists("m1"))
        self.assertEqual(len(store_b.list_memories()), 0)


if __name__ == "__main__":
    unittest.main()
