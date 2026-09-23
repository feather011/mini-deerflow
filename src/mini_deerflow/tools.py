"""安全工具集：基于 ast 的数学表达式计算器，以及工具注册基础设施。"""

from __future__ import annotations

import ast
import operator as op
from collections.abc import Callable
from dataclasses import dataclass

# ast 节点 → 安全运算符映射
_BINOPS: dict[type[ast.BinOp], op] = {
    ast.Add: op.add,
    ast.Sub: op.sub,
    ast.Mult: op.mul,
    ast.Div: op.truediv,
}

_UNARYOPS: dict[type[ast.UnaryOp], op] = {
    ast.USub: op.neg,
    ast.UAdd: op.pos,
}


def _eval_node(node: ast.AST) -> float | int:
    """递归求值 AST 节点，仅允许数字字面量和四则运算。"""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)

    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value

    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        return _BINOPS[type(node.op)](left, right)

    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARYOPS:
        return _UNARYOPS[type(node.op)](_eval_node(node.operand))

    raise ValueError(f"不支持的表达式节点: {ast.dump(node)}")


def calculator(expression: str) -> str:
    """安全计算数学表达式，返回字符串结果。"""
    tree = ast.parse(expression, mode="eval")
    result = _eval_node(tree)
    return str(float(result)) if isinstance(result, float) else str(result)


def calculator_handler(arguments: dict[str, object]) -> str:
    """calculator 的 Tool 适配器：校验参数后委托给 calculator 纯函数。"""
    if "expression" not in arguments:
        raise ValueError("缺少参数: expression")
    expression = arguments["expression"]
    if not isinstance(expression, str):
        raise ValueError("expression 必须是字符串")
    if not expression.strip():
        raise ValueError("expression 必须是非空字符串")
    return calculator(expression)


# --- 工具管理基础设施 ---


class ToolRegistryError(Exception):
    """工具注册表相关错误的基类。"""


class DuplicateToolError(ToolRegistryError):
    """注册已存在的工具名时抛出。"""


class ToolNotFoundError(ToolRegistryError):
    """查找未知工具时抛出。"""


class ToolExecutionError(ToolRegistryError):
    """handler 执行出错时抛出，原异常保存在 __cause__。"""


ToolHandler = Callable[[dict[str, object]], str]


@dataclass(frozen=True)
class Tool:
    """不可变的工具描述：元数据 + 执行处理器。"""

    name: str
    description: str
    parameters: dict[str, object]
    handler: ToolHandler

    def execute(self, arguments: dict[str, object]) -> str:
        """调用 handler 执行工具。"""
        return self.handler(arguments)


class ToolRegistry:
    """工具注册表：管理 Tool 的注册、查找与执行。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册工具；重名时抛出 DuplicateToolError。"""
        if tool.name in self._tools:
            raise DuplicateToolError(f"工具已存在: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        """按名称获取工具；未知时抛出 ToolNotFoundError。"""
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFoundError(f"未知工具: {name}") from None

    def execute(self, name: str, arguments: dict[str, object]) -> str:
        """查找并执行工具；handler 出错时包装为 ToolExecutionError。"""
        tool = self.get(name)
        try:
            return tool.execute(arguments)
        except Exception as exc:
            raise ToolExecutionError(f"工具执行失败: {name}: {exc}") from exc

    def list_tools(self) -> list[Tool]:
        """按名称排序返回所有工具。"""
        return [self._tools[name] for name in sorted(self._tools)]


CALCULATOR_TOOL = Tool(
    name="calculator",
    description="计算四则运算表达式（加减乘除、括号、正负号），适用于只包含数字和 +-*/ 的数学计算。",
    parameters={
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "需要计算的四则运算表达式",
            }
        },
        "required": ["expression"],
        "additionalProperties": False,
    },
    handler=calculator_handler,
)


def create_default_registry() -> ToolRegistry:
    """创建并返回注册了默认工具的新 Registry（每次都是新实例）。"""
    registry = ToolRegistry()
    registry.register(CALCULATOR_TOOL)
    return registry
