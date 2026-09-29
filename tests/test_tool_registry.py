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


class TestToolRegistryRegisterMany(unittest.TestCase):
    """register_many 批量注册的原子性测试。"""

    def test_empty_list_registers_nothing(self):
        """空列表不修改 registry"""
        registry = ToolRegistry()
        registry.register(_make_tool("existing"))
        registry.register_many([])
        self.assertEqual(len(registry.list_tools()), 1)
        self.assertEqual(registry.get("existing").name, "existing")

    def test_single_tool_registered(self):
        """单元素列表等价于 register"""
        registry = ToolRegistry()
        registry.register_many([_make_tool("solo")])
        self.assertEqual(registry.get("solo").name, "solo")

    def test_multiple_tools_all_registered(self):
        """多工具全部注册成功"""
        registry = ToolRegistry()
        tools = [_make_tool(f"tool_{i}") for i in range(5)]
        registry.register_many(tools)
        names = {t.name for t in registry.list_tools()}
        self.assertEqual(names, {f"tool_{i}" for i in range(5)})

    def test_duplicate_in_batch_raises_before_any_change(self):
        """批次内重名：任何注册前就失败，registry 不变"""
        registry = ToolRegistry()
        registry.register(_make_tool("preexisting"))
        tools = [_make_tool("dup"), _make_tool("dup"), _make_tool("good")]
        with self.assertRaises(DuplicateToolError) as ctx:
            registry.register_many(tools)
        self.assertIn("dup", str(ctx.exception))
        # 没有任何工具被注册（原子性）
        self.assertEqual(len(registry.list_tools()), 1)

    def test_conflict_with_existing_raises_before_any_change(self):
        """与已有工具冲突：失败时 registry 完全不变"""
        registry = ToolRegistry()
        registry.register(_make_tool("conflict"))
        tools = [_make_tool("new1"), _make_tool("conflict"), _make_tool("new2")]
        with self.assertRaises(DuplicateToolError):
            registry.register_many(tools)
        # 已有和新工具均未注册
        self.assertEqual({t.name for t in registry.list_tools()}, {"conflict"})
        with self.assertRaises(ToolNotFoundError):
            registry.get("new1")
        with self.assertRaises(ToolNotFoundError):
            registry.get("new2")

    def test_non_tool_in_list_raises(self):
        """列表包含非 Tool 元素时失败"""
        registry = ToolRegistry()
        tools = [_make_tool("good"), "not a tool"]  # type: ignore
        with self.assertRaises(TypeError):
            registry.register_many(tools)
        self.assertEqual(len(registry.list_tools()), 0)

    def test_string_raises_type_error(self):
        """传入 str 时抛出 TypeError（不是被当作单个元素）"""
        registry = ToolRegistry()
        with self.assertRaises(TypeError):
            registry.register_many("sandbox_read_file")  # type: ignore
        self.assertEqual(len(registry.list_tools()), 0)


if __name__ == "__main__":
    unittest.main()
