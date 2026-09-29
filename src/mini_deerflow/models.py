"""模型接口与模拟实现。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

_VALID_ROLES: frozenset[str] = frozenset({"system", "user", "assistant", "tool"})


@dataclass(frozen=True)
class Message:
    """对话消息。"""
    role: str  # "user" | "assistant" | "tool"
    content: str

    def to_dict(self) -> dict[str, object]:
        """消息转字典：{"role": ..., "content": ...}"""
        return {"role": self.role, "content": self.content}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Message":
        """从字典恢复 Message；非法数据抛 ValueError。"""
        if not isinstance(data, dict):
            raise ValueError(f"data 必须是字典，收到: {type(data).__name__}")

        if "role" not in data:
            raise ValueError("Message 缺少字段: role")
        role = data["role"]
        if not isinstance(role, str):
            raise ValueError(f"role 必须是字符串，收到: {type(role).__name__}")
        if role not in _VALID_ROLES:
            raise ValueError(
                f"未知 role: {role!r}（允许: system / user / assistant / tool）"
            )

        if "content" not in data:
            raise ValueError("Message 缺少字段: content")
        content = data["content"]
        if not isinstance(content, str):
            raise ValueError(
                f"content 必须是字符串，收到: {type(content).__name__}"
            )

        return cls(role=role, content=content)


@dataclass(frozen=True)
class ToolCall:
    """模型决定调用工具。"""
    name: str
    arguments: dict[str, object]


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
            return ToolCall(name="calculator", arguments={"expression": expression})

        if latest.role == "tool":
            return FinalAnswer(content=f"计算结果: {latest.content}")

        return FinalAnswer(content="当前只支持计算功能，请输入以'计算'开头的表达式。")
