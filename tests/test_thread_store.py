"""thread_store.py 单元测试。"""

import unittest

from mini_deerflow.models import Message
from mini_deerflow.thread_state import ThreadState
from mini_deerflow.thread_store import (
    InMemoryThreadStore,
    ThreadAlreadyExistsError,
    ThreadNotFoundError,
    ThreadStoreError,
)


class TestInMemoryThreadStore(unittest.TestCase):
    """测试 InMemoryThreadStore 的各项行为。"""

    def setUp(self):
        self.store = InMemoryThreadStore()

    def test_create_and_get(self):
        """创建后应能按 ID 取回同一个 Thread"""
        thread = self.store.create("t1")
        self.assertIsInstance(thread, ThreadState)
        self.assertEqual(thread.thread_id, "t1")
        self.assertIs(self.store.get("t1"), thread)

    def test_duplicate_create_fails(self):
        """重复 ID 创建应抛出 ThreadAlreadyExistsError"""
        self.store.create("t1")
        with self.assertRaises(ThreadAlreadyExistsError):
            self.store.create("t1")

    def test_get_unknown_fails(self):
        """获取未知 Thread 应抛出 ThreadNotFoundError"""
        with self.assertRaises(ThreadNotFoundError):
            self.store.get("missing")

    def test_exists(self):
        """exists 应返回 bool，且未知 ID 不抛异常"""
        self.store.create("t1")
        self.assertTrue(self.store.exists("t1"))
        self.assertFalse(self.store.exists("missing"))

    def test_save_existing_thread(self):
        """save 已存在 Thread 应成功更新"""
        thread = self.store.create("t1")
        thread.append(Message(role="user", content="hi"))
        self.store.save(thread)
        self.assertEqual(self.store.get("t1").message_count, 1)

    def test_save_unknown_thread_fails(self):
        """save 未知 Thread 应抛出 ThreadNotFoundError"""
        stranger = ThreadState(thread_id="not-in-store")
        with self.assertRaises(ThreadNotFoundError):
            self.store.save(stranger)

    def test_save_non_thread_state_fails(self):
        """save 非 ThreadState 应抛出 TypeError"""
        with self.assertRaises(TypeError):
            self.store.save("not a thread")  # type: ignore[arg-type]

    def test_delete_thread(self):
        """delete 已存在 Thread 应成功移除"""
        self.store.create("t1")
        self.store.delete("t1")
        self.assertFalse(self.store.exists("t1"))

    def test_delete_unknown_fails(self):
        """delete 未知 Thread 应抛出 ThreadNotFoundError"""
        with self.assertRaises(ThreadNotFoundError):
            self.store.delete("missing")

    def test_list_threads_sorted_by_id(self):
        """list_threads 应按 thread_id 排序"""
        self.store.create("zeta")
        self.store.create("alpha")
        self.store.create("mid")
        ids = [t.thread_id for t in self.store.list_threads()]
        self.assertEqual(ids, ["alpha", "mid", "zeta"])

    def test_list_threads_returns_tuple(self):
        """list_threads 应返回 tuple，不是列表或可变视图"""
        self.store.create("t1")
        result = self.store.list_threads()
        self.assertIsInstance(result, tuple)

    def test_two_stores_do_not_share_data(self):
        """两个 Store 实例的内部数据互不共享"""
        a = InMemoryThreadStore()
        b = InMemoryThreadStore()
        a.create("t1")
        self.assertFalse(b.exists("t1"))
        self.assertIsNot(a._threads, b._threads)

    def test_messages_persist_via_get(self):
        """Thread 中保存的消息通过 get 仍可读取"""
        thread = self.store.create("t1")
        thread.append(Message(role="user", content="hello"))
        thread.append(Message(role="tool", content="42"))
        restored = self.store.get("t1")
        self.assertEqual(restored.message_count, 2)
        self.assertEqual(restored.messages[0].content, "hello")
        self.assertEqual(restored.messages[1].content, "42")

    def test_specific_errors_inherit_thread_store_error(self):
        """具体异常应统一继承 ThreadStoreError"""
        for exc_type in (ThreadAlreadyExistsError, ThreadNotFoundError):
            self.assertTrue(issubclass(exc_type, ThreadStoreError))


if __name__ == "__main__":
    unittest.main()