"""cli.py 单元测试。"""

import unittest
from unittest.mock import patch

from mini_deerflow.cli import (
    CLIState,
    format_history,
    format_thread_list,
    format_trace,
    handle_command,
    main,
)
from mini_deerflow.models import Message
from mini_deerflow.thread_state import ThreadState
from mini_deerflow.thread_store import InMemoryThreadStore


def _captured_prints(mock_print) -> str:
    """把所有 print 调用拼成一个字符串便于断言。"""
    return "\n".join(str(c) for c in mock_print.call_args_list)


def _make_state_and_store():
    """构造一组干净的测试用 state + 已含 default 的 store + Agent。"""
    from mini_deerflow.agent import Agent
    from mini_deerflow.models import FakeModel
    from mini_deerflow.tools import create_default_registry

    store = InMemoryThreadStore()
    store.create("default")
    state = CLIState(current_thread_id="default")
    agent = Agent(model=FakeModel(), registry=create_default_registry(), store=store)
    return state, store, agent


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
        # math 应带 *
        self.assertTrue(any(line == "* math" for line in lines))
        self.assertTrue(any(line == "  default" for line in lines))


class TestMainBasics(unittest.TestCase):
    """主循环的基础行为（兼容原有测试）。"""

    @patch("builtins.input", side_effect=["exit"])
    @patch("builtins.print")
    @patch("sys.argv", ["mini-deerflow"])
    def test_exit_immediately(self, mock_print, mock_input):
        """输入 exit 应直接退出"""
        main()
        self.assertIn("再见", _captured_prints(mock_print))

    @patch("builtins.input", side_effect=["计算 1+1", "exit"])
    @patch("builtins.print")
    @patch("sys.argv", ["mini-deerflow"])
    def test_calculation_output(self, mock_print, mock_input):
        """输入计算应输出结果"""
        main()
        self.assertIn("2", _captured_prints(mock_print))

    @patch("builtins.input", side_effect=["", "exit"])
    @patch("builtins.print")
    @patch("sys.argv", ["mini-deerflow"])
    def test_empty_input_ignored(self, mock_print, mock_input):
        """空输入应被跳过"""
        main()
        self.assertEqual(mock_input.call_count, 2)


class TestCommands(unittest.TestCase):
    """5D 阶段：CLI 命令完整覆盖。"""

    @patch("builtins.input", side_effect=["/new math", "exit"])
    @patch("builtins.print")
    @patch("sys.argv", ["mini-deerflow"])
    def test_new_creates_and_switches(self, mock_print, mock_input):
        """/new 创建并自动切换"""
        main()
        self.assertIn("已创建并切换到：math", _captured_prints(mock_print))

    @patch("builtins.input", side_effect=["/new math", "/new math", "exit"])
    @patch("builtins.print")
    @patch("sys.argv", ["mini-deerflow"])
    def test_new_duplicate_does_not_switch(self, mock_print, mock_input):
        """重复 /new 报错且不切换"""
        main()
        out = _captured_prints(mock_print)
        # 两次 /new 都打印了一次，且第二次打印错误
        self.assertEqual(out.count("已创建并切换到：math"), 1)
        self.assertIn("线程已存在", out)

    @patch("builtins.input", side_effect=["/new math", "/switch math", "exit"])
    @patch("builtins.print")
    @patch("sys.argv", ["mini-deerflow"])
    def test_switch_success(self, mock_print, mock_input):
        """/switch 成功"""
        main()
        self.assertIn("已切换到：math", _captured_prints(mock_print))

    @patch("builtins.input", side_effect=["/switch ghost", "exit"])
    @patch("builtins.print")
    @patch("sys.argv", ["mini-deerflow"])
    def test_switch_unknown_fails(self, mock_print, mock_input):
        """/switch 未知 Thread 失败"""
        main()
        self.assertIn("未知线程: ghost", _captured_prints(mock_print))

    @patch("builtins.input", side_effect=["/new math", "/list", "exit"])
    @patch("builtins.print")
    @patch("sys.argv", ["mini-deerflow"])
    def test_list_marks_current(self, mock_print, mock_input):
        """/list 标记当前 Thread 为 *"""
        main()
        out = _captured_prints(mock_print)
        self.assertIn("* math", out)
        self.assertIn("  default", out)

    @patch("builtins.input", side_effect=["/history", "exit"])
    @patch("builtins.print")
    @patch("sys.argv", ["mini-deerflow"])
    def test_history_empty_when_no_messages(self, mock_print, mock_input):
        """/history 空会话显示提示"""
        main()
        self.assertIn("空会话", _captured_prints(mock_print))

    @patch("builtins.input", side_effect=["计算 1+1", "/history", "exit"])
    @patch("builtins.print")
    @patch("sys.argv", ["mini-deerflow"])
    def test_history_preserves_message_order(self, mock_print, mock_input):
        """/history 按消息顺序显示"""
        main()
        out = _captured_prints(mock_print)
        u = out.find("[user] 计算 1+1")
        t = out.find("[tool]")
        a = out.find("[assistant]")
        self.assertGreater(u, -1)
        self.assertGreater(t, u)
        self.assertGreater(a, t)

    @patch("builtins.input", side_effect=["/new math", "/delete default", "exit"])
    @patch("builtins.print")
    @patch("sys.argv", ["mini-deerflow"])
    def test_delete_non_current_thread(self, mock_print, mock_input):
        """/delete 非当前 Thread 应成功（删 default，current 是 math）"""
        main()
        self.assertIn("已删除: default", _captured_prints(mock_print))

    @patch("builtins.input", side_effect=["/delete default", "exit"])
    @patch("builtins.print")
    @patch("sys.argv", ["mini-deerflow"])
    def test_delete_current_forbidden(self, mock_print, mock_input):
        """/delete 当前 Thread 应被禁止"""
        main()
        out = _captured_prints(mock_print)
        self.assertIn("不能删除当前 Thread", out)
        self.assertNotIn("已删除", out)

    @patch("builtins.input", side_effect=["/delete ghost", "exit"])
    @patch("builtins.print")
    @patch("sys.argv", ["mini-deerflow"])
    def test_delete_unknown(self, mock_print, mock_input):
        """/delete 未知 Thread 应报错"""
        main()
        self.assertIn("未知线程: ghost", _captured_prints(mock_print))

    @patch("builtins.input", side_effect=["/current", "exit"])
    @patch("builtins.print")
    @patch("sys.argv", ["mini-deerflow"])
    def test_current_command(self, mock_print, mock_input):
        """/current 显示当前 Thread ID"""
        main()
        self.assertIn("default", _captured_prints(mock_print))

    @patch("builtins.input", side_effect=["/hello", "exit"])
    @patch("builtins.print")
    @patch("sys.argv", ["mini-deerflow"])
    def test_unknown_command(self, mock_print, mock_input):
        """未知命令应报错，不发往 Agent"""
        main()
        out = _captured_prints(mock_print)
        self.assertIn("未知命令: /hello", out)

    @patch("builtins.input", side_effect=["/new", "/switch", "/delete", "exit"])
    @patch("builtins.print")
    @patch("sys.argv", ["mini-deerflow"])
    def test_missing_arguments(self, mock_print, mock_input):
        """/new /switch /delete 缺参数应提示用法"""
        main()
        out = _captured_prints(mock_print)
        self.assertIn("用法：/new", out)
        self.assertIn("用法：/switch", out)
        self.assertIn("用法：/delete", out)


class TestNormalInputUsesCurrent(unittest.TestCase):
    """普通输入应使用当前 thread_id 调 Agent。"""

    @patch("builtins.input", side_effect=["/new work", "计算 1+1", "/history", "exit"])
    @patch("builtins.print")
    @patch("sys.argv", ["mini-deerflow"])
    def test_calculation_runs_in_current_thread(self, mock_print, mock_input):
        """切换到 work 后再计算，history 应包含该计算"""
        main()
        out = _captured_prints(mock_print)
        # work 的 history 应包含 [user] 计算 1+1
        self.assertIn("[user] 计算 1+1", out)
        self.assertIn("[assistant] 计算结果: 2", out)


if __name__ == "__main__":
    unittest.main()