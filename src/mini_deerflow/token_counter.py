"""Phase 24 Token 计数：可替换协议 + 保守启发式实现。

重要限制（务必如实看待）：

`HeuristicTokenCounter` 是**离线估算**，不等价于任何 Provider 的真实分词器。
它的设计目标是「宁可高估」——高估只会让压缩提前触发，低估会让请求直接被
Provider 以 context length 拒绝。因此：

- ASCII 按 3 字符 ≈ 1 token（Provider 常见约 4，取 3 = 高估约 33%）
- 非 ASCII（CJK 等）按 1 字 ≈ 1 token
- 每条消息额外计 role/分隔符开销

真实误差由 `ContextPolicy.safety_margin_tokens` 兜底，**不要**把这个数
当成「已精确计数」写进任何断言或文档。
"""

from __future__ import annotations

import json
import math
from typing import Protocol, runtime_checkable

from mini_deerflow.models import Message, ToolSpec

# 每条消息的结构性开销：role 标记、字段分隔、provider 封装
MESSAGE_OVERHEAD_TOKENS = 4


@runtime_checkable
class TokenCounter(Protocol):
    """Token 计数协议。可替换为 Provider 精确计数实现。"""

    def count_text(self, text: str) -> int:
        """统计一段文本的 Token 数。"""
        ...

    def count_message(self, message: Message) -> int:
        """统计单条消息（含结构性开销）。"""
        ...

    def count_messages(self, messages: list[Message] | tuple[Message, ...]) -> int:
        """统计消息序列。"""
        ...

    def count_tool_specs(self, specs: list[ToolSpec] | tuple[ToolSpec, ...]) -> int:
        """统计工具 Schema 占用的 Token。"""
        ...


class HeuristicTokenCounter:
    """保守启发式计数器（离线、确定性、无依赖）。"""

    __slots__ = ()

    def count_text(self, text: str) -> int:
        if not text:
            return 0
        ascii_chars = 0
        other_chars = 0
        for char in text:
            if ord(char) < 128:
                ascii_chars += 1
            else:
                other_chars += 1
        return math.ceil(ascii_chars / 3) + other_chars

    def count_message(self, message: Message) -> int:
        total = self.count_text(message.content) + MESSAGE_OVERHEAD_TOKENS
        if message.tool_name:
            total += self.count_text(message.tool_name)
        if message.tool_call_id:
            total += 1
        if message.tool_arguments:
            # 参数可能嵌套结构，序列化后计数比逐 key 猜更稳
            try:
                total += self.count_text(
                    json.dumps(message.tool_arguments, ensure_ascii=False, default=str)
                )
            except (TypeError, ValueError):
                total += MESSAGE_OVERHEAD_TOKENS
        return total

    def count_messages(
        self, messages: list[Message] | tuple[Message, ...]
    ) -> int:
        return sum(self.count_message(message) for message in messages)

    def count_tool_specs(
        self, specs: list[ToolSpec] | tuple[ToolSpec, ...]
    ) -> int:
        total = 0
        for spec in specs:
            total += self.count_text(spec.name)
            total += self.count_text(spec.description)
            try:
                total += self.count_text(
                    json.dumps(spec.input_schema, ensure_ascii=False, default=str)
                )
            except (TypeError, ValueError):
                total += MESSAGE_OVERHEAD_TOKENS
            # Provider 侧每个工具的结构性包装
            total += MESSAGE_OVERHEAD_TOKENS
        return total


__all__ = [
    "MESSAGE_OVERHEAD_TOKENS",
    "HeuristicTokenCounter",
    "TokenCounter",
]
