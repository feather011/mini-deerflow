"""cli.py 单元测试。"""

import unittest
from unittest.mock import patch

from mini_deerflow.cli import format_trace, main


class TestFormatTrace(unittest.TestCase):
    """测试轨迹格式化纯函数。"""

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


class TestMain(unittest.TestCase):
    """测试 main() 的非交互行为。"""

    @patch("builtins.input", side_effect=["exit"])
    @patch("builtins.print")
    @patch("sys.argv", ["mini-deerflow"])
    def test_exit_immediately(self, mock_print, mock_input):
        """输入 exit 应直接退出"""
        main()
        printed = [str(c) for c in mock_print.call_args_list]
        self.assertTrue(any("再见" in p for p in printed))

    @patch("builtins.input", side_effect=["计算 1+1", "exit"])
    @patch("builtins.print")
    @patch("sys.argv", ["mini-deerflow"])
    def test_calculation_output(self, mock_print, mock_input):
        """输入计算应输出结果"""
        main()
        printed = [str(c) for c in mock_print.call_args_list]
        self.assertTrue(any("2" in p for p in printed))

    @patch("builtins.input", side_effect=["", "exit"])
    @patch("builtins.print")
    @patch("sys.argv", ["mini-deerflow"])
    def test_empty_input_ignored(self, mock_print, mock_input):
        """空输入应被跳过"""
        main()
        self.assertEqual(mock_input.call_count, 2)


if __name__ == "__main__":
    unittest.main()
