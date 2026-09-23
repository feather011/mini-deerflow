"""ToolRegistry 单元测试。"""

import unittest

from mini_deerflow.tools import (
    DuplicateToolError,
    Tool,
    ToolExecutionError,
    ToolNotFoundError,
    ToolRegistry,
    ToolRegistryError,
)


def _echo_handler(arguments: dict[str, object]) -> str:
    """返回 message 参数的字符串形式。"""
    return str(arguments.get("message", ""))


def _make_tool(name: str = "echo") -> Tool:
    return Tool(
        name=name,
        description="回显 message",
        parameters={"type": "object"},
        handler=_echo_handler,
    )


class TestToolRegistry(unittest.TestCase):
    """测试工具注册表的各项行为。"""

    def test_register_and_get(self):
        """注册后应能按名称取回同一个工具"""
        registry = ToolRegistry()
        tool = _make_tool()
        registry.register(tool)
        self.assertIs(registry.get("echo"), tool)

    def test_execute(self):
        """execute 应调用 handler 并返回结果"""
        registry = ToolRegistry()
        registry.register(_make_tool())
        self.assertEqual(registry.execute("echo", {"message": "hi"}), "hi")

    def test_duplicate_register(self):
        """重名注册应抛出 DuplicateToolError"""
        registry = ToolRegistry()
        registry.register(_make_tool())
        with self.assertRaises(DuplicateToolError):
            registry.register(_make_tool())

    def test_unknown_tool(self):
        """查找未知工具应抛出 ToolNotFoundError"""
        registry = ToolRegistry()
        with self.assertRaises(ToolNotFoundError):
            registry.get("missing")
        with self.assertRaises(ToolNotFoundError):
            registry.execute("missing", {})

    def test_list_tools_sorted_by_name(self):
        """list_tools 应按名称排序"""
        registry = ToolRegistry()
        registry.register(_make_tool(name="zeta"))
        registry.register(_make_tool(name="alpha"))
        registry.register(_make_tool(name="mid"))
        names = [tool.name for tool in registry.list_tools()]
        self.assertEqual(names, ["alpha", "mid", "zeta"])

    def test_handler_error_wrapped(self):
        """handler 抛出异常时应包装为 ToolExecutionError"""

        def failing_handler(arguments: dict[str, object]) -> str:
            raise RuntimeError("boom")

        registry = ToolRegistry()
        registry.register(
            Tool(
                name="fail",
                description="总是失败",
                parameters={},
                handler=failing_handler,
            )
        )
        with self.assertRaises(ToolExecutionError):
            registry.execute("fail", {})

    def test_original_exception_in_cause(self):
        """原始异常应保存在 __cause__ 中"""

        def failing_handler(arguments: dict[str, object]) -> str:
            raise RuntimeError("boom")

        registry = ToolRegistry()
        registry.register(
            Tool(
                name="fail",
                description="总是失败",
                parameters={},
                handler=failing_handler,
            )
        )
        with self.assertRaises(ToolExecutionError) as ctx:
            registry.execute("fail", {})
        self.assertIsInstance(ctx.exception.__cause__, RuntimeError)
        self.assertEqual(str(ctx.exception.__cause__), "boom")

    def test_errors_inherit_tool_registry_error(self):
        """自定义异常应统一继承 ToolRegistryError"""
        for exc_type in (DuplicateToolError, ToolNotFoundError, ToolExecutionError):
            self.assertTrue(issubclass(exc_type, ToolRegistryError))


if __name__ == "__main__":
    unittest.main()
