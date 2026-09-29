"""会话状态数据模型（5A 阶段）。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from mini_deerflow.models import Message


SCHEMA_VERSION = 1


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

    def to_dict(self) -> dict[str, object]:
        """ThreadState 转字典；通过 snapshot() 读取消息，不暴露内部列表。"""
        return {
            "schema_version": SCHEMA_VERSION,
            "thread_id": self.thread_id,
            "messages": [m.to_dict() for m in self.snapshot()],
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "ThreadState":
        """从字典恢复 ThreadState；非法数据抛 ValueError，输入不被修改。"""
        if not isinstance(data, dict):
            raise ValueError(f"data 必须是字典，收到: {type(data).__name__}")

        if "schema_version" not in data:
            raise ValueError("Thread 缺少字段: schema_version")
        version = data["schema_version"]
        # bool 是 int 子类，必须显式排除
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValueError(
                f"schema_version 必须是整数，收到: {type(version).__name__}"
            )
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"未知 schema_version: {version}（当前: {SCHEMA_VERSION}）"
            )

        if "thread_id" not in data:
            raise ValueError("Thread 缺少字段: thread_id")
        thread_id = data["thread_id"]
        if not isinstance(thread_id, str):
            raise ValueError(
                f"thread_id 必须是非空字符串，收到: {type(thread_id).__name__}"
            )
        if not thread_id:
            raise ValueError("thread_id 不能为空字符串")

        if "messages" not in data:
            raise ValueError("Thread 缺少字段: messages")
        raw_messages = data["messages"]
        if not isinstance(raw_messages, list):
            raise ValueError(
                f"messages 必须是列表，收到: {type(raw_messages).__name__}"
            )

        # 原子恢复：先全部恢复为 Message，验证通过再写入 Thread
        messages = [Message.from_dict(m) for m in raw_messages]
        thread = cls(thread_id=thread_id)
        thread.extend(messages)
        return thread
