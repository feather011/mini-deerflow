"""安全工具集：基于 ast 的数学表达式计算器，以及工具注册基础设施。"""

from __future__ import annotations

import ast
import operator as op
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from mini_deerflow.models import ToolSpec

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


class ToolNotAllowedError(ToolRegistryError):
    """工具不在允许清单内时抛出（Subagent `allowed_tools` 运行时强制）。

    与 `ToolNotFoundError` 的区别很重要：
    - `ToolNotFoundError` = 注册表里**没有**这个工具（名字压根不存在）
    - `ToolNotAllowedError` = 注册表里**有**，但当前子任务没有权限调用

    两者分开，日志与前端才能区分「模型叫错了名字」和「越权调用」。
    """


ToolHandler = Callable[[dict[str, object]], str]
"""普通工具签名：只拿到模型给的参数。

绝大多数工具（calculator、sandbox_*、MCP 转发）都属于这类——
它们不关心自己跑在哪个会话里。
"""

ContextAwareToolHandler = Callable[..., str]
"""需要执行上下文的工具签名：`(arguments, context) -> str`。

仅 `task_note` 这类**按线程隔离**的工具使用。thread_id 从 `ExecutionContext`
取，**不接受模型传入**——否则模型写一句 `{"thread_id": "别人的线程"}` 就能
跨会话读写笔记。
"""

# 允许清单的两种取值语义（贯穿暴露限制与执行限制两层）：
#   None → 不设限制，沿用调用方传入的注册表（既有默认策略）
#   frozenset() → 空白名单，**任何工具都不允许**
ToolAllowList = frozenset[str] | None


def build_allow_list(allowed_tools: tuple[str, ...] | None) -> ToolAllowList:
    """把 `SubagentContextPolicy.allowed_tools` 归一成内部判定期望的形态。

    `None` 与 `[]` 必须**刻意区分**：前者是"不设限制"，后者是"全部禁止"。
    用 `None` / `frozenset()` 承载，避免把"空列表"误判成"不过滤"。
    """
    if allowed_tools is None:
        return None
    return frozenset(allowed_tools)


def filter_tool_specs(
    specs: Sequence[ToolSpec],
    allowed: ToolAllowList,
) -> list[ToolSpec]:
    """第一道防线（**暴露限制**）：只把允许的工具描述交给模型。

    模型看不到的工具，正常情况下就不会被选中。但这只是降低概率——
    模型可能凭上下文里出现过的名字"凭记忆"伪造调用，
    所以还需要第二道防线 `ensure_tool_allowed()`。
    """
    if allowed is None:
        return list(specs)
    return [spec for spec in specs if spec.name in allowed]


def ensure_tool_allowed(name: str, allowed: ToolAllowList) -> None:
    """第二道防线（**执行限制**）：执行前再校验一次工具名。

    与第一道防线构成纵深防御：
    - 暴露限制挡的是"模型本不该知道有这个工具"
    - 执行限制挡的是"模型编造了一个未授权的工具名"

    任一层单独存在都不够——只做暴露限制，伪造名字即可绕过；
    只做执行限制，模型仍会浪费轮次去猜不存在的工具。
    """
    if allowed is None:
        return
    if name not in allowed:
        raise ToolNotAllowedError(f"工具不在允许清单内: {name}")


@dataclass(frozen=True)
class Tool:
    """不可变的工具描述：元数据 + 执行处理器。"""

    name: str
    description: str
    parameters: dict[str, object]
    handler: ToolHandler | ContextAwareToolHandler
    needs_context: bool = False
    """为 True 时 `execute` 会把 ExecutionContext 一并交给 handler。

    默认 False：存量工具（calculator / sandbox / MCP）的签名完全不变，
    只有明确按线程隔离的工具才打开这条通道。
    """

    def execute(
        self,
        arguments: dict[str, object],
        context: object | None = None,
    ) -> str:
        """调用 handler 执行工具。

        Args:
            arguments: 模型给出的参数。
            context: 执行上下文；仅 `needs_context=True` 时传递。
                为 None 时不会悄悄改语义——context 工具拿不到 thread_id
                应当明确失败，而不是落到某个默认线程上。
        """
        if self.needs_context:
            return self.handler(arguments, context)  # type: ignore[call-arg]
        return self.handler(arguments)

    def to_spec(self) -> ToolSpec:
        """生成面向模型的纯数据描述。

        这是「可执行 Tool」与「给模型看的 ToolSpec」的唯一转换点：
        handler 不会离开注册表，供应商适配层拿到的只有 name/description/input_schema。
        """
        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=dict(self.parameters),
        )


class ToolRegistry:
    """工具注册表：管理 Tool 的注册、查找与执行。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册工具；重名时抛出 DuplicateToolError。"""
        if tool.name in self._tools:
            raise DuplicateToolError(f"工具已存在: {tool.name}")
        self._tools[tool.name] = tool

    def register_many(self, tools: Sequence[Tool]) -> None:
        """批量原子注册工具。

        全部验证通过后才提交：批次内无重复、与已有工具无冲突。
        任何失败时 registry 保持不变。
        """
        if isinstance(tools, str):
            raise TypeError("tools 不能是 str")
        # 预先检查：批次内重复
        seen: dict[str, Tool] = {}
        for tool in tools:
            if not isinstance(tool, Tool):
                raise TypeError(
                    f"tools 元素必须是 Tool，收到: {type(tool).__name__}"
                )
            if tool.name in seen:
                raise DuplicateToolError(
                    f"批次内工具名重复: {tool.name}"
                )
            seen[tool.name] = tool
        # 预先检查：与已有冲突
        for tool in tools:
            if tool.name in self._tools:
                raise DuplicateToolError(f"工具已存在: {tool.name}")
        # 全部通过后原子提交
        for tool in tools:
            self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        """按名称获取工具；未知时抛出 ToolNotFoundError。"""
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFoundError(f"未知工具: {name}") from None

    def execute(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        context: object | None = None,
    ) -> str:
        """查找并执行工具；handler 出错时包装为 ToolExecutionError。

        `context` 只会传给 `needs_context=True` 的工具；普通工具即使收到
        也会被忽略，因此存量调用点不传也能照常工作。
        """
        tool = self.get(name)
        try:
            return tool.execute(arguments, context)
        except Exception as exc:
            raise ToolExecutionError(f"工具执行失败: {name}: {exc}") from exc

    def list_tools(self) -> list[Tool]:
        """按名称排序返回所有工具。"""
        return [self._tools[name] for name in sorted(self._tools)]

    def tool_specs(self) -> list[ToolSpec]:
        """按名称排序返回工具描述（provider-neutral，供 Agent 传给 Model）。

        与 `list_tools()` 的区别：这里不暴露 handler，
        调用方无法从中执行任何东西。
        """
        return [tool.to_spec() for tool in self.list_tools()]


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
