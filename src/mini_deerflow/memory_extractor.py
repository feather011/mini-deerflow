"""记忆提取器（14C 阶段）。"""

from __future__ import annotations

from typing import Callable, Protocol, Sequence

from mini_deerflow.memory import Memory
from mini_deerflow.models import Message


class MemoryExtractor(Protocol):
    """从消息序列中提取 Memory 的接口。"""

    def extract(
        self,
        source_thread_id: str,
        messages: Sequence[Message],
    ) -> list[Memory]:
        """从消息中提取记忆，返回 Memory 列表。"""
        ...


# 支持的前缀映射：前缀 -> kind
_PREFIX_MAP: dict[str, str] = {
    "记住事实：": "fact",
    "记住事实:": "fact",
    "记住偏好：": "preference",
    "记住偏好:": "preference",
    "记住指令：": "instruction",
    "记住指令:": "instruction",
}

# 按长度降序排列，用于优先匹配最长前缀
_PREFIXES_BY_LENGTH = sorted(_PREFIX_MAP.keys(), key=len, reverse=True)


class RuleBasedMemoryExtractor:
    """基于规则的记忆提取器。"""

    __slots__ = ("_id_factory",)

    def __init__(self, id_factory: Callable[[], str]) -> None:
        self._id_factory = id_factory

    def extract(
        self,
        source_thread_id: str,
        messages: Sequence[Message],
    ) -> list[Memory]:
        if not isinstance(source_thread_id, str) or not source_thread_id.strip():
            raise ValueError(
                f"source_thread_id 必须是非空字符串，收到: {source_thread_id!r}"
            )

        # 验证 messages 是可遍历的 Message 序列
        try:
            message_list = list(messages)
        except TypeError as e:
            raise TypeError(f"messages 必须是可遍历的序列，收到: {type(messages).__name__}") from e

        for i, msg in enumerate(message_list):
            if not isinstance(msg, Message):
                raise TypeError(
                    f"messages[{i}] 必须是 Message 类型，收到: {type(msg).__name__}"
                )

        memories: list[Memory] = []
        for msg in message_list:
            # 只处理 user 消息
            if msg.role != "user":
                continue

            # 先对完整 content 执行 strip
            stripped_content = msg.content.strip()
            if not stripped_content:
                continue

            # 查找匹配的前缀（必须在 strip 后的内容开头）
            matched_kind: str | None = None
            for prefix in _PREFIXES_BY_LENGTH:
                if stripped_content.startswith(prefix):
                    matched_kind = _PREFIX_MAP[prefix]
                    # 去掉前缀后再次 strip 作为实际内容
                    content_after_prefix = stripped_content[len(prefix) :].strip()
                    if content_after_prefix:
                        memory_id = self._id_factory()
                        if not isinstance(memory_id, str) or not memory_id.strip():
                            raise ValueError(
                                f"id_factory 必须返回非空字符串，收到: {memory_id!r}"
                            )
                        memories.append(
                            Memory(
                                memory_id=memory_id,
                                kind=matched_kind,
                                content=content_after_prefix,
                                source_thread_id=source_thread_id,
                            )
                        )
                    break

        return memories
