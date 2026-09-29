"""长期记忆 CLI 集成测试（14G 阶段）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mini_deerflow.cli import (
    CLIState,
    format_memories,
    handle_command,
)
from mini_deerflow.memory import Memory
from mini_deerflow.memory_store_sqlite import SQLiteMemoryStore
from mini_deerflow.thread_store import SQLiteThreadStore


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


def _captured_prints(mock_print) -> str:
    """把所有 print 调用拼成一个字符串便于断言。"""
    return "\n".join(str(c) for c in mock_print.call_args_list)


class TestFormatMemories(unittest.TestCase):
    """format_memories 纯函数测试。"""

    def test_empty(self):
        """空列表输出暂无提示。"""
        result = format_memories([])
        self.assertEqual(result, "暂无长期记忆")

    def test_single_memory(self):
        """单条 Memory 格式化。"""
        memories = [_memory("m1", kind="preference", content="用户喜欢 uv")]
        result = format_memories(memories)
        self.assertIn("[m1]", result)
        self.assertIn("preference", result)
        self.assertIn("用户喜欢 uv", result)
        self.assertIn("来源：", result)

    def test_multiple_memories(self):
        """多条 Memory 格式化。"""
        memories = [
            _memory("m1", kind="instruction", content="指令内容"),
            _memory("m2", kind="fact", content="事实内容"),
        ]
        result = format_memories(memories)
        self.assertIn("[m1]", result)
        self.assertIn("[m2]", result)
        self.assertIn("instruction", result)
        self.assertIn("fact", result)

    def test_chinese_content(self):
        """中文内容。"""
        memories = [_memory("m1", content="中文测试内容")]
        result = format_memories(memories)
        self.assertIn("中文测试内容", result)

    def test_does_not_modify_memory(self):
        """不修改原始 Memory。"""
        memories = [_memory("m1", content="原始")]
        format_memories(memories)
        self.assertEqual(memories[0].content, "原始")


class TestMemoriesCommand(unittest.TestCase):
    """/memories 命令测试。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "store.db")
        self.store = SQLiteMemoryStore(self.db_path)
        self.addCleanup(self.store.close)

    def test_empty_store(self):
        """空 Store 输出暂无提示。"""
        with patch("builtins.print") as mock_print:
            handle_command("/memories", CLIState("default"), None, None, self.store)
        result = _captured_prints(mock_print)
        self.assertIn("暂无长期记忆", result)

    def test_single_memory(self):
        """单条 Memory 显示。"""
        self.store.add(_memory("m1", kind="preference", content="用户喜欢 uv"))
        with patch("builtins.print") as mock_print:
            handle_command("/memories", CLIState("default"), None, None, self.store)
        result = _captured_prints(mock_print)
        self.assertIn("[m1]", result)
        self.assertIn("preference", result)
        self.assertIn("用户喜欢 uv", result)

    def test_multiple_memories(self):
        """多条 Memory 显示。"""
        self.store.add(_memory("m1", kind="fact", content="事实"))
        self.store.add(_memory("m2", kind="preference", content="偏好"))
        with patch("builtins.print") as mock_print:
            handle_command("/memories", CLIState("default"), None, None, self.store)
        result = _captured_prints(mock_print)
        self.assertIn("[m1]", result)
        self.assertIn("[m2]", result)

    def test_memory_store_none(self):
        """未配置 MemoryStore 时给出提示。"""
        with patch("builtins.print") as mock_print:
            handle_command("/memories", CLIState("default"), None, None, None)
        result = _captured_prints(mock_print)
        self.assertIn("长期记忆未启用", result)


class TestForgetCommand(unittest.TestCase):
    """/forget 命令测试。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "store.db")
        self.store = SQLiteMemoryStore(self.db_path)
        self.addCleanup(self.store.close)

    def test_successful_delete(self):
        """成功删除。"""
        self.store.add(_memory("m1"))
        with patch("builtins.print") as mock_print:
            handle_command("/forget m1", CLIState("default"), None, None, self.store)
        result = _captured_prints(mock_print)
        self.assertIn("已删除长期记忆: m1", result)
        self.assertFalse(self.store.exists("m1"))

    def test_missing_argument(self):
        """缺少参数。"""
        with patch("builtins.print") as mock_print:
            handle_command("/forget", CLIState("default"), None, None, self.store)
        result = _captured_prints(mock_print)
        self.assertIn("用法：/forget <memory_id>", result)

    def test_unknown_id(self):
        """未知 ID。"""
        with patch("builtins.print") as mock_print:
            handle_command("/forget nonexistent", CLIState("default"), None, None, self.store)
        result = _captured_prints(mock_print)
        self.assertIn("未找到长期记忆: nonexistent", result)
        self.assertIn("nonexistent", result)

    def test_only_deletes_target(self):
        """只删除目标 Memory。"""
        self.store.add(_memory("m1", content="内容1"))
        self.store.add(_memory("m2", content="内容2"))
        handle_command("/forget m1", CLIState("default"), None, None, self.store)
        self.assertFalse(self.store.exists("m1"))
        self.assertTrue(self.store.exists("m2"))

    def test_delete_persists_across_restart(self):
        """删除跨重启保持。"""
        self.store.add(_memory("m1"))
        self.store.close()

        store2 = SQLiteMemoryStore(self.db_path)
        handle_command("/forget m1", CLIState("default"), None, None, store2)
        store2.close()

        store3 = SQLiteMemoryStore(self.db_path)
        self.assertFalse(store3.exists("m1"))
        store3.close()

    def test_memory_store_none(self):
        """未配置 MemoryStore 时给出提示。"""
        with patch("builtins.print") as mock_print:
            handle_command("/forget m1", CLIState("default"), None, None, None)
        result = _captured_prints(mock_print)
        self.assertIn("长期记忆未启用", result)


class TestCLIWithMemoryIntegration(unittest.TestCase):
    """CLI 与 MemoryStore 集成测试。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "store.db")

    def test_memories_command_with_real_store(self):
        """使用真实 Store 测试 /memories。"""
        from mini_deerflow.cli import create_memory_store, create_store

        thread_store = create_store(self.db_path)
        memory_store = create_memory_store(self.db_path)
        memory_store.add(_memory("m1", kind="preference", content="用户偏好"))

        try:
            with patch("builtins.print") as mock_print:
                handle_command(
                    "/memories",
                    CLIState("default"),
                    thread_store,
                    None,
                    memory_store,
                )
            result = _captured_prints(mock_print)
            self.assertIn("[m1]", result)
            self.assertIn("preference", result)
            self.assertIn("用户偏好", result)
        finally:
            memory_store.close()
            thread_store.close()

    def test_forget_command_with_real_store(self):
        """使用真实 Store 测试 /forget。"""
        from mini_deerflow.cli import create_memory_store, create_store

        thread_store = create_store(self.db_path)
        memory_store = create_memory_store(self.db_path)
        memory_store.add(_memory("m1"))

        try:
            with patch("builtins.print") as mock_print:
                handle_command(
                    "/forget m1",
                    CLIState("default"),
                    thread_store,
                    None,
                    memory_store,
                )
            result = _captured_prints(mock_print)
            self.assertIn("已删除长期记忆: m1", result)
            self.assertFalse(memory_store.exists("m1"))
        finally:
            memory_store.close()
            thread_store.close()


class TestEndToEndMemoryFlow(unittest.TestCase):
    """端到端记忆流程测试。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "store.db")

    def test_memory_survives_restart(self):
        """记忆在程序重启后仍然存在。"""
        from mini_deerflow.cli import create_memory_store, create_store

        # 第一次运行
        thread_store1 = create_store(self.db_path)
        memory_store1 = create_memory_store(self.db_path)

        memory_store1.add(_memory("m1", kind="fact", content="测试事实"))

        # 验证第一次写入
        self.assertTrue(memory_store1.exists("m1"))

        memory_store1.close()
        thread_store1.close()

        # 重启后
        thread_store2 = create_store(self.db_path)
        memory_store2 = create_memory_store(self.db_path)

        # 验证数据恢复
        self.assertTrue(memory_store2.exists("m1"))
        m = memory_store2.get("m1")
        self.assertEqual(m.content, "测试事实")
        self.assertEqual(m.kind, "fact")

        memory_store2.close()
        thread_store2.close()

    def test_multiple_threads_share_memory_store(self):
        """多个 Thread 共享同一个 MemoryStore。"""
        from mini_deerflow.cli import create_memory_store, create_store

        thread_store = create_store(self.db_path)
        memory_store = create_memory_store(self.db_path)

        # 在 thread-1 中添加记忆
        memory_store.add(_memory("m1", kind="preference", content="用户偏好", source_thread_id="thread-1"))

        # thread-2 应该能看到同一份记忆
        memories = memory_store.list_memories()
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].content, "用户偏好")

        memory_store.close()
        thread_store.close()


class TestStoreLifecycle(unittest.TestCase):
    """Store 生命周期测试。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "store.db")

    def test_main_closes_stores(self):
        """main 退出时关闭 Store。"""
        from mini_deerflow.cli import create_memory_store, create_store, main

        thread_store = create_store(self.db_path)
        memory_store = create_memory_store(self.db_path)
        memory_store.add(_memory("m1"))

        with patch("builtins.input", side_effect=["exit"]), \
             patch("builtins.print"):
            main(
                argv=["--db", self.db_path],
                thread_store=thread_store,
                memory_store=memory_store,
            )

        # Store 应该被关闭
        with self.assertRaises(Exception):
            thread_store._conn.execute("SELECT 1")
        with self.assertRaises(Exception):
            memory_store._conn.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
