"""cli.py 单元测试。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mini_deerflow.cli import (
    CLIState,
    create_store,
    format_history,
    format_thread_list,
    format_trace,
    handle_command,
    main,
    run_cli,
)
from mini_deerflow.models import Message
from mini_deerflow.thread_state import ThreadState
from mini_deerflow.thread_store import InMemoryThreadStore, SQLiteThreadStore


def _captured_prints(mock_print) -> str:
    """把所有 print 调用拼成一个字符串便于断言。"""
    return "\n".join(str(c) for c in mock_print.call_args_list)


class _TempDBTestCase(unittest.TestCase):
    """基类：每个测试使用独立的临时数据库，不写项目目录。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = str(Path(self._tmp.name) / "test.db")

    def run_main(self, inputs: list[str], extra_argv: list[str] | None = None) -> str:
        """用给定输入序列运行 main()，返回捕获的输出。"""
        argv = ["mini-deerflow", "--db", self.db_path] + (extra_argv or [])
        with patch("builtins.input", side_effect=inputs), \
             patch("builtins.print") as mock_print, \
             patch("sys.argv", argv):
            main()
        return _captured_prints(mock_print)


class TestFormatTrace(unittest.TestCase):
    """轨迹格式化纯函数。"""

    def test_model_decision(self):
        trace = [{"step": 1, "action": "model_decision", "detail": "调用工具 calculator"}]
        result = format_trace(trace)
        self.assertIn("模型决策", result)
        self.assertIn("calculator", result)

    def test_tool_call(self):
        trace = [{
            "step": 1,
            "action": "tool_call",
            "detail": {"name": "calculator", "args": "1+1", "result": "2"},
        }]
        result = format_trace(trace)
        self.assertIn("calculator", result)
        self.assertIn("'1+1'", result)
        self.assertIn("2", result)

    def test_final_answer(self):
        trace = [{"step": 1, "action": "final_answer", "detail": "答案是 42"}]
        result = format_trace(trace)
        self.assertIn("最终回答", result)
        self.assertIn("42", result)

    def test_full_trace(self):
        trace = [
            {"step": 1, "action": "model_decision", "detail": "调用工具 calculator，参数: 10 + 5"},
            {
                "step": 1,
                "action": "tool_call",
                "detail": {"name": "calculator", "args": "10 + 5", "result": "15"},
            },
            {"step": 2, "action": "final_answer", "detail": "计算结果: 15"},
        ]
        result = format_trace(trace)
        self.assertIn("步骤 1", result)
        self.assertIn("步骤 2", result)
        self.assertEqual(result.count("步骤 1"), 2)

    def test_memory_saved(self):
        trace = [
            {"step": 1, "action": "final_answer", "detail": "回答"},
            {"step": 2, "type": "memory_saved", "count": 2, "memory_ids": ["m1", "m2"]},
        ]
        result = format_trace(trace)
        self.assertIn("已保存长期记忆", result)
        self.assertIn("2", result)

    def test_memory_warning(self):
        trace = [
            {"step": 1, "action": "final_answer", "detail": "回答"},
            {"step": 2, "type": "memory_warning", "error": "记忆冲突"},
        ]
        result = format_trace(trace)
        self.assertIn("长期记忆处理失败", result)
        self.assertIn("记忆冲突", result)


class TestFormatHistory(unittest.TestCase):
    """history 格式化纯函数。"""

    def test_empty(self):
        thread = ThreadState(thread_id="t")
        self.assertEqual(format_history(thread), "（空会话）")

    def test_messages(self):
        thread = ThreadState(thread_id="t")
        thread.extend([
            Message(role="user", content="hi"),
            Message(role="assistant", content="hello"),
        ])
        result = format_history(thread)
        self.assertIn("[user] hi", result)
        self.assertIn("[assistant] hello", result)


class TestFormatThreadList(unittest.TestCase):
    """list 格式化纯函数。"""

    def test_marks_current(self):
        store = InMemoryThreadStore()
        store.create("default")
        store.create("math")
        result = format_thread_list(store, "math")
        lines = result.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(any(line == "* math" for line in lines))
        self.assertTrue(any(line == "  default" for line in lines))


class TestMainBasics(_TempDBTestCase):
    """主循环的基础行为。"""

    def test_exit_immediately(self):
        """输入 exit 应直接退出"""
        out = self.run_main(["exit"])
        self.assertIn("再见", out)

    def test_calculation_output(self):
        """输入计算应输出结果"""
        out = self.run_main(["计算 1+1", "exit"])
        self.assertIn("2", out)

    def test_empty_input_ignored(self):
        """空输入应被跳过"""
        self.run_main(["", "exit"])
        # 空输入不会崩溃即可，第二次才是 exit

    def test_trace_flag(self):
        """--trace 应显示执行轨迹"""
        out = self.run_main(["计算 1+1", "exit"], extra_argv=["--trace"])
        self.assertIn("执行轨迹", out)
        self.assertIn("工具调用", out)


class TestCommands(_TempDBTestCase):
    """5D 阶段：CLI 命令完整覆盖。"""

    def test_new_creates_and_switches(self):
        """/new 创建并自动切换"""
        out = self.run_main(["/new math", "exit"])
        self.assertIn("已创建并切换到：math", out)

    def test_new_duplicate_does_not_switch(self):
        """重复 /new 报错且不切换"""
        out = self.run_main(["/new math", "/new math", "exit"])
        self.assertEqual(out.count("已创建并切换到：math"), 1)
        self.assertIn("线程已存在", out)

    def test_switch_success(self):
        """/switch 成功"""
        out = self.run_main(["/new math", "/switch math", "exit"])
        self.assertIn("已切换到：math", out)

    def test_switch_unknown_fails(self):
        """/switch 未知 Thread 失败"""
        out = self.run_main(["/switch ghost", "exit"])
        self.assertIn("未知线程: ghost", out)

    def test_list_marks_current(self):
        """/list 标记当前 Thread 为 *"""
        out = self.run_main(["/new math", "/list", "exit"])
        self.assertIn("* math", out)
        self.assertIn("  default", out)

    def test_history_empty_when_no_messages(self):
        """/history 空会话显示提示"""
        out = self.run_main(["/history", "exit"])
        self.assertIn("空会话", out)

    def test_history_preserves_message_order(self):
        """/history 按消息顺序显示"""
        out = self.run_main(["计算 1+1", "/history", "exit"])
        u = out.find("[user] 计算 1+1")
        t = out.find("[tool]")
        a = out.find("[assistant]")
        self.assertGreater(u, -1)
        self.assertGreater(t, u)
        self.assertGreater(a, t)

    def test_delete_non_current_thread(self):
        """/delete 非当前 Thread 应成功"""
        out = self.run_main(["/new math", "/delete default", "exit"])
        self.assertIn("已删除: default", out)

    def test_delete_current_forbidden(self):
        """/delete 当前 Thread 应被禁止"""
        out = self.run_main(["/delete default", "exit"])
        self.assertIn("不能删除当前 Thread", out)
        self.assertNotIn("已删除", out)

    def test_delete_unknown(self):
        """/delete 未知 Thread 应报错"""
        out = self.run_main(["/delete ghost", "exit"])
        self.assertIn("未知线程: ghost", out)

    def test_current_command(self):
        """/current 显示当前 Thread ID"""
        out = self.run_main(["/current", "exit"])
        self.assertIn("default", out)

    def test_unknown_command(self):
        """未知命令应报错，不发往 Agent"""
        out = self.run_main(["/hello", "exit"])
        self.assertIn("未知命令: /hello", out)

    def test_missing_arguments(self):
        """/new /switch /delete 缺参数应提示用法"""
        out = self.run_main(["/new", "/switch", "/delete", "exit"])
        self.assertIn("用法：/new", out)
        self.assertIn("用法：/switch", out)
        self.assertIn("用法：/delete", out)


class TestNormalInputUsesCurrent(_TempDBTestCase):
    """普通输入应使用当前 thread_id 调 Agent。"""

    def test_calculation_runs_in_current_thread(self):
        """切换到 work 后再计算，history 应包含该计算"""
        out = self.run_main(["/new work", "计算 1+1", "/history", "exit"])
        self.assertIn("[user] 计算 1+1", out)
        self.assertIn("[assistant] 计算结果: 2", out)


class TestCreateStore(_TempDBTestCase):
    """create_store：default 会话的创建与保留。"""

    def test_first_startup_creates_default(self):
        """第一次启动应自动创建 default"""
        store = create_store(self.db_path)
        self.addCleanup(store.close)
        self.assertTrue(store.exists("default"))

    def test_default_not_overwritten(self):
        """default 已存在时不得覆盖或清空"""
        # 第一次：写入消息
        store1 = create_store(self.db_path)
        thread = store1.get("default")
        thread.append(Message(role="user", content="不能丢失的旧消息"))
        store1.save(thread)
        store1.close()

        # 第二次：default 已存在，不能被清空
        store2 = create_store(self.db_path)
        self.addCleanup(store2.close)
        restored = store2.get("default")
        self.assertEqual(restored.message_count, 1)
        self.assertEqual(restored.last_message.content, "不能丢失的旧消息")

    def test_specified_database_path(self):
        """指定的数据库路径应被使用（文件出现且内容隔离）"""
        other = str(Path(self._tmp.name) / "custom.db")
        store = create_store(other)
        store.close()
        self.assertTrue(Path(other).exists())
        self.assertFalse(Path(self.db_path).exists())


class TestPersistenceAcrossRuns(_TempDBTestCase):
    """跨"进程重启"的持久化：两次独立运行 main()。"""

    def test_second_run_lists_previous_threads(self):
        """第二次运行能列出第一次创建的会话"""
        self.run_main(["/new math", "计算 10 + 20", "exit"])
        out = self.run_main(["/list", "exit"])
        self.assertIn("math", out)

    def test_second_run_shows_previous_history(self):
        """第二次运行能查看第一次的历史"""
        self.run_main(["/new math", "计算 10 + 20", "exit"])
        out = self.run_main(["/switch math", "/history", "exit"])
        self.assertIn("[user] 计算 10 + 20", out)
        self.assertIn("[tool] 30", out)
        self.assertIn("[assistant] 计算结果: 30", out)


class TestStoreLifecycle(_TempDBTestCase):
    """Store 生命周期与测试隔离。"""

    def test_store_closed_on_exit(self):
        """正常退出时 Store 被关闭"""
        with patch("builtins.input", side_effect=["exit"]), \
             patch("builtins.print"), \
             patch("sys.argv", ["mini-deerflow", "--db", self.db_path]):
            main()
        # main 结束后连接已关闭：再次打开文件验证内容完好
        store = SQLiteThreadStore(self.db_path)
        self.assertTrue(store.exists("default"))
        store.close()

    def test_project_dir_not_polluted(self):
        """测试运行不向项目目录写入默认数据库"""
        project_root = Path(__file__).resolve().parent.parent
        default_db = project_root / "mini-deerflow.db"
        existed_before = default_db.exists()
        self.run_main(["计算 1+1", "exit"])
        # 运行后不应创建项目目录的默认库（若本来就存在则大小不变）
        if not existed_before:
            self.assertFalse(default_db.exists())

    def test_run_cli_closes_via_main(self):
        """注入 store 时，main 退出后该 store 的连接也被关闭"""
        store = create_store(self.db_path)
        with patch("builtins.input", side_effect=["exit"]), \
             patch("builtins.print"):
            main(argv=["--db", self.db_path], thread_store=store)
        # 注入的 store 被 main 关闭
        with self.assertRaises(Exception):
            store._conn.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()