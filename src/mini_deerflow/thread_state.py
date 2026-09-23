"""会话状态数据模型（5A 阶段）。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from mini_deerflow.models import Message


@dataclass
class ThreadState:
    """一个会话的线程状态：持有消息列表及其元信息。"""

    thread_id: str
    messages: list[Message] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.thread_id, str):
            raise ValueError("thread_id 必须是字符串")
        if not self.thread_id.strip():
            raise ValueError("thread_id 不能为空（去除首尾空格后）")

    def append(self, message: Message) -> None:
        """追加一条消息；非 Message 类型抛出 TypeError。"""
        if not isinstance(message, Message):
            raise TypeError(f"只接受 Message，收到: {type(message).__name__}")
        self.messages.append(message)

    def extend(self, messages: Iterable[Message]) -> None:
        """批量追加消息：先完整验证，再一次性写入（全部成功或全部失败）。"""
        items = list(messages)
        for item in items:
            if not isinstance(item, Message):
                raise TypeError(f"只接受 Message，收到: {type(item).__name__}")
        self.messages.extend(items)

    @property
    def message_count(self) -> int:
        """当前消息数量。"""
        return len(self.messages)

    @property
    def last_message(self) -> Message | None:
        """最后一条消息；空线程返回 None。"""
        return self.messages[-1] if self.messages else None

    def snapshot(self) -> tuple[Message, ...]:
        """返回不可变快照，外部修改不影响内部状态。"""
        return tuple(self.messages)
