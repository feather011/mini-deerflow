"""模型接口与模拟实现。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Message:
    """对话消息。"""
    role: str  # "user" | "tool"
    content: str


@dataclass(frozen=True)
class ToolCall:
    """模型决定调用工具。"""
    name: str
    args: str


@dataclass(frozen=True)
class FinalAnswer:
    """模型决定给出最终回答。"""
    content: str


@runtime_checkable
class Model(Protocol):
    """模型协议：任何实现 decide 方法的类都满足此协议。"""
    def decide(self, messages: list[Message]) -> ToolCall | FinalAnswer: ...


class FakeModel:
    """确定性模拟模型，用于测试 Agent 循环。"""

    def decide(self, messages: list[Message]) -> ToolCall | FinalAnswer:
        if not messages:
            return FinalAnswer(content="请输入问题。")

        latest = messages[-1]

        if latest.role == "user" and latest.content.startswith("计算 "):
            expression = latest.content[len("计算 "):]
            return ToolCall(name="calculator", args=expression)

        if latest.role == "tool":
            return FinalAnswer(content=f"计算结果: {latest.content}")

        return FinalAnswer(content="当前只支持计算功能，请输入以'计算'开头的表达式。")
