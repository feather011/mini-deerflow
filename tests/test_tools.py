"""tools.py 单元测试。"""

import unittest

from mini_deerflow.tools import calculator


class TestCalculator(unittest.TestCase):
    """测试安全计算器的各种场景。"""

    def test_bracket_and_precedence(self):
        """(12 + 8) * 3 = 60"""
        self.assertEqual(calculator("(12 + 8) * 3"), "60")

    def test_unary_negative(self):
        """-5 + 2 = -3"""
        self.assertEqual(calculator("-5 + 2"), "-3")

    def test_division(self):
        """10 / 4 = 2.5"""
        self.assertEqual(calculator("10 / 4"), "2.5")

    def test_rejects_function_call(self):
        """__import__() 应被拒绝"""
        with self.assertRaises(ValueError):
            calculator('__import__("os").getcwd()')

    def test_rejects_variable(self):
        """变量名应被拒绝"""
        with self.assertRaises(ValueError):
            calculator("x + 1")

    def test_rejects_attribute_access(self):
        """属性访问应被拒绝"""
        with self.assertRaises(ValueError):
            calculator("(1).__class__")


if __name__ == "__main__":
    unittest.main()
