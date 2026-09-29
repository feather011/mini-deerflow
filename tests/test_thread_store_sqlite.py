"""SQLiteThreadStore 单元测试。"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mini_deerflow.models import Message
from mini_deerflow.thread_state import ThreadState
from mini_deerflow.thread_store import (
    SQLiteThreadStore,
    ThreadAlreadyExistsError,
    ThreadDataCorruptedError,
    ThreadNotFoundError,
    ThreadStoreClosedError,
    ThreadStoreError,
)


class TestSQLiteThreadStore(unittest.TestCase):
    """SQLiteThreadStore 各项行为。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "store.db"
        self.store = SQLiteThreadStore(self.path)
        self.addCleanup(self.store.close)

    def test_initialization_creates_table(self):
        """初始化时应自动建表"""
        # 直接打开数据库检查表是否存在
        conn = sqlite3.connect(self.path)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='threads'"
            ).fetchone()
            self.assertIsNotNone(row)
        finally:
            conn.close()

    def test_create_and_get(self):
        """创建并读取会话"""
        thread = self.store.create("t1")
        self.assertEqual(thread.thread_id, "t1")
        restored = self.store.get("t1")
        self.assertEqual(restored.thread_id, "t1")
        self.assertIsInstance(restored, ThreadState)

    def test_save_messages_persist(self):
        """保存消息后能够恢复"""
        thread = self.store.create("t1")
        thread.append(Message(role="user", content="计算 1+1"))
        thread.append(Message(role="tool", content="2"))
        self.store.save(thread)
        restored = self.store.get("t1")
        self.assertEqual(restored.message_count, 2)
        self.assertEqual(restored.snapshot()[0].content, "计算 1+1")
        self.assertEqual(restored.snapshot()[1].content, "2")

    def test_chinese_messages_preserved(self):
        """中文消息能够正确保存"""
        thread = self.store.create("cn")
        thread.append(Message(role="user", content="你好世界"))
        self.store.save(thread)
        restored = self.store.get("cn")
        self.assertEqual(restored.last_message.content, "你好世界")

    def test_message_order_preserved(self):
        """多条消息顺序不变"""
        thread = self.store.create("t1")
        for c in ["first", "second", "third", "fourth"]:
            thread.append(Message(role="user", content=c))
        self.store.save(thread)
        restored = self.store.get("t1")
        self.assertEqual([m.content for m in restored.snapshot()],
                         ["first", "second", "third", "fourth"])

    def test_multiple_threads_isolated(self):
        """多个会话互相隔离"""
        a = self.store.create("a")
        b = self.store.create("b")
        a.append(Message(role="user", content="A的消息"))
        b.append(Message(role="user", content="B的消息"))
        self.store.save(a)
        self.store.save(b)
        self.assertEqual(self.store.get("a").last_message.content, "A的消息")
        self.assertEqual(self.store.get("b").last_message.content, "B的消息")

    def test_duplicate_create_fails(self):
        """重复创建应抛出 ThreadAlreadyExistsError"""
        self.store.create("t1")
        with self.assertRaises(ThreadAlreadyExistsError):
            self.store.create("t1")

    def test_get_unknown_fails(self):
        """获取不存在应抛出 ThreadNotFoundError"""
        with self.assertRaises(ThreadNotFoundError):
            self.store.get("missing")

    def test_delete_thread(self):
        """删除会话"""
        self.store.create("t1")
        self.store.delete("t1")
        self.assertFalse(self.store.exists("t1"))

    def test_delete_unknown_fails(self):
        """删除不存在应抛出 ThreadNotFoundError"""
        with self.assertRaises(ThreadNotFoundError):
            self.store.delete("missing")

    def test_exists(self):
        """exists 应正确返回 bool"""
        self.store.create("t1")
        self.assertTrue(self.store.exists("t1"))
        self.assertFalse(self.store.exists("missing"))

    def test_list_threads_sorted(self):
        """list_threads 应按 thread_id 排序"""
        for tid in ["zeta", "alpha", "mid"]:
            self.store.create(tid)
        ids = [t.thread_id for t in self.store.list_threads()]
        self.assertEqual(ids, ["alpha", "mid", "zeta"])

    def test_save_non_thread_state(self):
        """save 非 ThreadState 应抛 TypeError"""
        with self.assertRaises(TypeError):
            self.store.save("not a thread")  # type: ignore[arg-type]

    def test_save_unknown_fails(self):
        """save 未创建的 Thread 应抛 ThreadNotFoundError"""
        stranger = ThreadState(thread_id="not-in-store")
        with self.assertRaises(ThreadNotFoundError):
            self.store.save(stranger)


class TestSQLiteThreadStorePersistence(unittest.TestCase):
    """关闭 Store 后数据应持久化到磁盘。"""

    def test_data_persists_after_reopen(self):
        """关闭并重新创建 Store 后数据仍然存在"""
        tmp = tempfile.TemporaryDirectory()
        try:
            path = Path(tmp.name) / "persist.db"
            store1 = SQLiteThreadStore(path)
            try:
                thread = store1.create("important")
                thread.append(Message(role="user", content="不能丢失"))
                store1.save(thread)
            finally:
                store1.close()

            # 重新打开
            store2 = SQLiteThreadStore(path)
            try:
                restored = store2.get("important")
                self.assertEqual(restored.thread_id, "important")
                self.assertEqual(restored.last_message.content, "不能丢失")
            finally:
                store2.close()
        finally:
            tmp.cleanup()


class TestSQLiteThreadStoreCorruptJSON(unittest.TestCase):
    """损坏的 JSON 应被检测并清晰报错。"""

    def test_corrupt_json_in_get_raises(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            path = Path(tmp.name) / "corrupt.db"
            store = SQLiteThreadStore(path)
            try:
                store.create("ok")
                # 直接写入损坏的 JSON
                store._conn.execute(
                    "UPDATE threads SET state_json = ? WHERE thread_id = ?",
                    ("{not valid json}", "ok"),
                )
                store._conn.commit()
                with self.assertRaises(ValueError) as ctx:
                    store.get("ok")
                self.assertIn("损坏", str(ctx.exception))
            finally:
                store.close()
        finally:
            tmp.cleanup()

    def test_corrupt_json_in_list_raises(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            path = Path(tmp.name) / "corrupt.db"
            store = SQLiteThreadStore(path)
            try:
                store.create("ok")
                store._conn.execute(
                    "UPDATE threads SET state_json = ? WHERE thread_id = ?",
                    ("not json at all", "ok"),
                )
                store._conn.commit()
                with self.assertRaises(ValueError):
                    store.list_threads()
            finally:
                store.close()
        finally:
            tmp.cleanup()


class _CorruptRowTestCase(unittest.TestCase):
    """基类：向数据库注入损坏行的公共工具。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "store.db"
        self.store = SQLiteThreadStore(self.path)
        self.addCleanup(self.store.close)

    def inject(self, thread_id: str, raw: str) -> None:
        """直接向数据库写入任意 state_json。"""
        self.store._conn.execute(
            "INSERT OR REPLACE INTO threads (thread_id, state_json) VALUES (?, ?)",
            (thread_id, raw),
        )
        self.store._conn.commit()


class TestCorruptedDataHandling(_CorruptRowTestCase):
    """6D：各类损坏数据都报 ThreadDataCorruptedError，且带 thread_id。"""

    def assert_corrupted(self, thread_id: str) -> None:
        with self.assertRaises(ThreadDataCorruptedError) as ctx:
            self.store.get(thread_id)
        self.assertIn(thread_id, str(ctx.exception))

    def test_invalid_json(self):
        """state_json 不是合法 JSON"""
        self.inject("t1", "{broken json")
        self.assert_corrupted("t1")

    def test_top_level_not_dict(self):
        """JSON 顶层不是字典"""
        self.inject("t1", json.dumps([1, 2, 3]))
        self.assert_corrupted("t1")

    def test_missing_schema_version(self):
        """缺少 schema_version"""
        self.inject("t1", json.dumps({"thread_id": "t1", "messages": []}))
        self.assert_corrupted("t1")

    def test_unknown_schema_version(self):
        """不支持的 schema_version"""
        self.inject("t1", json.dumps({
            "schema_version": 999, "thread_id": "t1", "messages": [],
        }))
        self.assert_corrupted("t1")

    def test_missing_thread_id(self):
        """缺少 thread_id"""
        self.inject("t1", json.dumps({"schema_version": 1, "messages": []}))
        self.assert_corrupted("t1")

    def test_thread_id_mismatch(self):
        """数据内部 thread_id 与数据库主键不一致"""
        self.inject("math", json.dumps({
            "schema_version": 1, "thread_id": "other", "messages": [],
        }))
        with self.assertRaises(ThreadDataCorruptedError) as ctx:
            self.store.get("math")
        msg = str(ctx.exception)
        self.assertIn("math", msg)
        self.assertIn("other", msg)
        self.assertIn("不一致", msg)

    def test_messages_not_list(self):
        """messages 不是列表"""
        self.inject("t1", json.dumps({
            "schema_version": 1, "thread_id": "t1", "messages": "oops",
        }))
        self.assert_corrupted("t1")

    def test_invalid_message_in_messages(self):
        """messages 中存在非法 Message"""
        self.inject("t1", json.dumps({
            "schema_version": 1, "thread_id": "t1",
            "messages": [{"role": "alien", "content": "bad"}],
        }))
        self.assert_corrupted("t1")

    def test_list_threads_reports_corrupted_row(self):
        """list_threads 遇到损坏记录也应报错并指出 thread_id"""
        good = ThreadState(thread_id="a-good")
        self.inject("a-good", json.dumps(good.to_dict(), ensure_ascii=False))
        self.inject("b-broken", "{oops")
        with self.assertRaises(ThreadDataCorruptedError) as ctx:
            self.store.list_threads()
        self.assertIn("b-broken", str(ctx.exception))

    def test_corrupted_is_thread_store_error(self):
        """ThreadDataCorruptedError 属于 ThreadStoreError 异常族"""
        self.assertTrue(issubclass(ThreadDataCorruptedError, ThreadStoreError))
        self.assertTrue(issubclass(ThreadDataCorruptedError, ValueError))

    def test_no_low_level_exception_leaks(self):
        """底层 json/sqlite3 异常不应直接泄漏（__cause__ 保留即可，对外是领域异常）"""
        self.inject("t1", "{broken")
        with self.assertRaises(ThreadDataCorruptedError):
            self.store.get("t1")


class TestTransactionAtomicity(_CorruptRowTestCase):
    """6D：写入失败时 rollback，旧数据保持不变。"""

    def test_failed_save_keeps_old_data(self):
        """保存失败（serialize 阶段抛错）后，数据库中的旧内容不变"""
        thread = self.store.create("t1")
        thread.append(Message(role="user", content="原始旧消息"))
        self.store.save(thread)

        # 让序列化在写入前失败
        corrupted = ThreadState(thread_id="t1")
        corrupted.append(Message(role="user", content="新消息"))
        with patch.object(
            self.store, "_serialize", side_effect=ValueError("serialize boom")
        ):
            with self.assertRaises(ValueError):
                self.store.save(corrupted)

        # 旧数据仍在
        restored = self.store.get("t1")
        self.assertEqual(restored.last_message.content, "原始旧消息")

    def test_failed_delete_of_missing_keeps_db_intact(self):
        """删除不存在的线程抛错后，已有数据不受影响"""
        self.store.create("t1")
        with self.assertRaises(ThreadNotFoundError):
            self.store.delete("ghost")
        self.assertTrue(self.store.exists("t1"))

    def test_rowcount_failure_rolls_back(self):
        """UPDATE 影响 0 行（未知线程）在事务内抛出，数据库无半写状态"""
        stranger = ThreadState(thread_id="nobody")
        with self.assertRaises(ThreadNotFoundError):
            self.store.save(stranger)
        # 数据库里不应出现 nobody
        self.assertFalse(self.store.exists("nobody"))


class TestStoreLifecycle6D(_CorruptRowTestCase):
    """6D：close 幂等、关闭后清晰报错、上下文管理器。"""

    def test_close_is_idempotent(self):
        """重复调用 close 不崩溃"""
        self.store.close()
        self.store.close()  # 不应抛异常

    def test_operation_after_close_raises_clear_error(self):
        """关闭后操作抛出清晰的领域错误"""
        self.store.close()
        with self.assertRaises(ThreadStoreClosedError) as ctx:
            self.store.create("t1")
        self.assertIn("已关闭", str(ctx.exception))

    def test_all_operations_guarded_after_close(self):
        """所有公开操作在关闭后都有清晰错误"""
        self.store.close()
        thread = ThreadState(thread_id="t")
        for op in (
            lambda: self.store.get("x"),
            lambda: self.store.save(thread),
            lambda: self.store.delete("x"),
            lambda: self.store.exists("x"),
            lambda: self.store.list_threads(),
        ):
            with self.assertRaises(ThreadStoreClosedError):
                op()

    def test_context_manager_auto_closes(self):
        """with 离开后连接自动关闭"""
        path = Path(self.tmp.name) / "ctx.db"
        with SQLiteThreadStore(path) as store:
            store.create("t1")
            self.assertFalse(store._closed)
        self.assertTrue(store._closed)
        with self.assertRaises(ThreadStoreClosedError):
            store.get("t1")

    def test_context_manager_closes_on_exception(self):
        """with 块内抛异常也会关闭连接"""
        path = Path(self.tmp.name) / "ctx-exc.db"
        with self.assertRaises(RuntimeError):
            with SQLiteThreadStore(path) as store:
                raise RuntimeError("boom")
        self.assertTrue(store._closed)


class TestMultipleInstances(_CorruptRowTestCase):
    """6D：两个 Store 实例顺序访问同一数据库文件。"""

    def test_two_instances_sequential_access(self):
        path = self.path
        store1 = SQLiteThreadStore(path)
        try:
            t = store1.create("shared")
            t.append(Message(role="user", content="store1 写入"))
            store1.save(t)
        finally:
            store1.close()

        store2 = SQLiteThreadStore(path)
        try:
            got = store2.get("shared")
            self.assertEqual(got.last_message.content, "store1 写入")

            # store2 修改并保存
            got.append(Message(role="assistant", content="store2 追加"))
            store2.save(got)
        finally:
            store2.close()

        store3 = SQLiteThreadStore(path)
        self.addCleanup(store3.close)
        final = store3.get("shared")
        self.assertEqual(final.message_count, 2)
        self.assertEqual(final.last_message.content, "store2 追加")


if __name__ == "__main__":
    unittest.main()