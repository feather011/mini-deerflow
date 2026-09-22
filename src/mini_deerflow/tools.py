"""安全工具集：基于 ast 的数学表达式计算器。"""

from __future__ import annotations

import ast
import operator as op

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


TOOLS: dict[str, callable] = {
    "calculator": calculator,
}
