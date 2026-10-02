"""模型接口与模拟实现。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

_VALID_ROLES: frozenset[str] = frozenset({"system", "user", "assistant", "tool"})


@dataclass(frozen=True)
class Message:
    """对话消息。

    `tool_call_id` / `tool_name` / `tool_arguments` 是 21B 为工具闭环增加的
    **可选**字段，用于把 provider 的 `tool_use` / `tool_result` 关联关系带进
    同一轮的模型上下文：

    - assistant + `tool_call_id` + `tool_name` + `tool_arguments`
      → 还原成 provider 的 `tool_use` 块
    - tool + `tool_call_id`
      → 还原成 provider 的 `tool_result` 块（须紧跟其 `tool_use`）

    三者默认 `None`，因此**老数据无需迁移**：`to_dict` 只在有值时才写这些键，
    序列化结果与 21B 之前逐字节一致；`SCHEMA_VERSION` 保持 1（纯新增字段）。
    """

    role: str  # "user" | "assistant" | "tool"
    content: str
    # --- 以下为 21B 新增，全部可选 ---
    tool_call_id: str | None = None
    """provider 侧的 tool_use id；用于与 tool_result 配对。"""
    tool_name: str | None = None
    """对应 tool_use.name；仅 assistant 工具调用消息会设置。"""
    tool_arguments: dict[str, object] | None = None
    """对应 tool_use.input；结构化保留原始参数，供下一轮还原。"""

    def to_dict(self) -> dict[str, object]:
        """消息转字典。

        21B 新增字段**只在有值时写出**，保证不含这些字段的普通消息序列化结果
        与 21B 之前完全一致（持久化格式不变）。
        """
        data: dict[str, object] = {"role": self.role, "content": self.content}
        if self.tool_call_id is not None:
            data["tool_call_id"] = self.tool_call_id
        if self.tool_name is not None:
            data["tool_name"] = self.tool_name
        if self.tool_arguments is not None:
            data["tool_arguments"] = self.tool_arguments
        return data

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Message":
        """从字典恢复 Message；非法数据抛 ValueError。

        21B 新增字段缺省即视为未设置，因此 21B 之前写入的数据仍可正常恢复。
        """
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

        tool_call_id = _optional_str(data, "tool_call_id")
        tool_name = _optional_str(data, "tool_name")
        tool_arguments = _optional_args(data, "tool_arguments")

        return cls(
            role=role,
            content=content,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_arguments=tool_arguments,
        )


def _optional_str(data: dict[str, object], key: str) -> str | None:
    """读取可选字符串字段；存在但类型不对时抛 ValueError。"""
    if key not in data:
        return None
    value = data[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} 必须是字符串或 null，收到: {type(value).__name__}")
    return value


def _optional_args(data: dict[str, object], key: str) -> dict[str, object] | None:
    """读取可选的参数字典；工具参数必须是 JSON object。"""
    if key not in data:
        return None
    value = data[key]
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(
            f"{key} 必须是对象，收到: {type(value).__name__}"
        )
    return dict(value)


@dataclass(frozen=True)
class ToolSpec:
    """面向模型的工具描述（provider-neutral）。

    只暴露 `name` / `description` / `input_schema` 三件事——与 Anthropic、
    OpenAI 等供应商的 tools 字段同构，但**不含 handler、不含任何供应商类型**，
    因此 `ToolRegistry` 与 `Agent` 无需知道最终发给谁。
    """

    name: str
    description: str
    input_schema: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        """转成 JSON-Schema 形状的字典（供应商适配层各自再包装）。"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass(frozen=True)
class ToolCall:
    """模型决定调用工具。"""
    name: str
    arguments: dict[str, object]
    # 21B：provider 侧 tool_use id。保留它才能在下一轮请求里
    # 用匹配的 tool_result 与之配对，否则供应商会拒绝该轮请求。
    id: str | None = None


@dataclass(frozen=True)
class FinalAnswer:
    """模型决定给出最终回答。"""
    content: str


@runtime_checkable
class Model(Protocol):
    """模型协议：任何实现 decide 方法的类都满足此协议。

    `tools` 由 Agent 每轮传入「当前允许调用的工具描述」，模型适配层再翻译成
    供应商自己的 tools 字段。用**可选参数**而非新增方法，是为了让所有已有实现
    （FakeModel、测试替身、包装器）只需补一个默认值即可继续工作。

    不传 `ToolRegistry` 整体：注册表里带 handler（可执行函数），
    把它交给模型适配层等于把可执行对象泄漏到传输层；只给纯数据的 ToolSpec。
    """

    def decide(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
    ) -> ToolCall | FinalAnswer: ...


class FakeModel:
    """确定性模拟模型，用于测试 Agent 循环。

    刻意**忽略 `tools`**：它按内置规则决定是否调用 calculator，
    不需要、也不应该知道注册表里实际有哪些工具（否则测试会随工具集漂移）。
    """

    def decide(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
    ) -> ToolCall | FinalAnswer:
        if not messages:
            return FinalAnswer(content="请输入问题。")

        latest = messages[-1]

        if latest.role == "user" and latest.content.startswith("计算 "):
            expression = latest.content[len("计算 "):]
            return ToolCall(name="calculator", arguments={"expression": expression})

        if latest.role == "tool":
            return FinalAnswer(content=f"计算结果: {latest.content}")

        return FinalAnswer(content="当前只支持计算功能，请输入以'计算'开头的表达式。")
