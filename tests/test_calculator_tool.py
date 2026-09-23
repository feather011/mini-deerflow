"""calculator_handler、CALCULATOR_TOOL 与 create_default_registry 单元测试。"""

import unittest

from mini_deerflow.tools import (
    CALCULATOR_TOOL,
    ToolExecutionError,
    calculator,
    calculator_handler,
    create_default_registry,
)


class TestCalculatorHandler(unittest.TestCase):
    """测试 calculator_handler 的参数校验与委托。"""

    def test_delegates_to_calculator(self):
        """校验通过后应委托给 calculator 纯函数"""
        self.assertEqual(
            calculator_handler({"expression": "(12 + 8) * 3"}),
            calculator("(12 + 8) * 3"),
        )

    def test_missing_expression(self):
        """缺少 expression 应抛出 ValueError"""
        with self.assertRaises(ValueError) as ctx:
            calculator_handler({})
        self.assertIn("expression", str(ctx.exception))

    def test_expression_not_string(self):
        """expression 不是字符串应抛出 ValueError"""
        with self.assertRaises(ValueError) as ctx:
            calculator_handler({"expression": 42})
        self.assertIn("字符串", str(ctx.exception))

    def test_expression_empty_string(self):
        """expression 是空字符串应抛出 ValueError"""
        with self.assertRaises(ValueError) as ctx:
            calculator_handler({"expression": ""})
        self.assertIn("非空", str(ctx.exception))

    def test_error_via_registry_wrapped(self):
        """经 Registry 执行时应表现为 ToolExecutionError，__cause__ 是原 ValueError"""
        registry = create_default_registry()
        with self.assertRaises(ToolExecutionError) as ctx:
            registry.execute("calculator", {})
        self.assertIsInstance(ctx.exception.__cause__, ValueError)


class TestCalculatorTool(unittest.TestCase):
    """测试 CALCULATOR_TOOL 定义与默认 Registry。"""

    def test_schema_correct(self):
        """参数 Schema 应符合约定"""
        schema = CALCULATOR_TOOL.parameters
        self.assertEqual(schema["type"], "object")
        self.assertEqual(
            schema["properties"]["expression"]["type"],
            "string",
        )
        self.assertEqual(schema["required"], ["expression"])
        self.assertIs(schema["additionalProperties"], False)

    def test_tool_identity(self):
        """name/description/handler 应正确"""
        self.assertEqual(CALCULATOR_TOOL.name, "calculator")
        self.assertTrue(CALCULATOR_TOOL.description)
        self.assertIs(CALCULATOR_TOOL.handler, calculator_handler)

    def test_default_registry_contains_calculator(self):
        """默认 Registry 应包含 calculator"""
        registry = create_default_registry()
        self.assertIn("calculator", [t.name for t in registry.list_tools()])

    def test_default_registry_execute(self):
        """默认 Registry 应能执行 calculator"""
        registry = create_default_registry()
        self.assertEqual(
            registry.execute("calculator", {"expression": "(12 + 8) * 3"}),
            "60",
        )

    def test_two_registries_are_different_instances(self):
        """两次调用应返回不同实例"""
        self.assertIsNot(create_default_registry(), create_default_registry())


if __name__ == "__main__":
    unittest.main()
