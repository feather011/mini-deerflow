"""长期记忆数据模型（14A 阶段）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

ALLOWED_KINDS: frozenset[str] = frozenset({"fact", "preference", "instruction"})


class MemoryAlreadyExistsError(Exception):
    """尝试添加已存在的记忆时抛出。"""

    def __init__(self, memory_id: str) -> None:
        self.memory_id = memory_id
        super().__init__(f"记忆已存在: {memory_id}")


class MemoryNotFoundError(Exception):
    """访问或删除不存在的记忆时抛出。"""

    def __init__(self, memory_id: str) -> None:
        self.memory_id = memory_id
        super().__init__(f"记忆不存在: {memory_id}")


class MemoryContentConflictError(Exception):
    """更新记忆时发现新内容与已有记忆重复。"""

    def __init__(self, memory_id: str) -> None:
        self.memory_id = memory_id
        super().__init__(
            f"更新记忆 {memory_id} 失败：内容与已有记忆重复"
        )


@dataclass(frozen=True)
class Memory:
    """一条长期记忆：从某次会话中提取、可跨会话复用的信息。"""

    memory_id: str
    kind: str
    content: str
    source_thread_id: str

    def to_dict(self) -> dict[str, object]:
        """记忆转字典。"""
        return {
            "memory_id": self.memory_id,
            "kind": self.kind,
            "content": self.content,
            "source_thread_id": self.source_thread_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Memory":
        """从字典恢复 Memory；非法数据抛 ValueError，不修改输入字典。"""
        if not isinstance(data, dict):
            raise ValueError(f"data 必须是字典，收到: {type(data).__name__}")

        required = ("memory_id", "kind", "content", "source_thread_id")
        for field_name in required:
            if field_name not in data:
                raise ValueError(f"Memory 缺少字段: {field_name}")
            value = data[field_name]
            if not isinstance(value, str):
                raise ValueError(
                    f"{field_name} 必须是字符串，收到: {type(value).__name__}"
                )
            if not value.strip():
                raise ValueError(f"{field_name} 不能为空字符串")

        kind = data["kind"]
        if kind not in ALLOWED_KINDS:
            raise ValueError(
                f"未知 kind: {kind!r}（允许: fact / preference / instruction）"
            )

        return cls(
            memory_id=data["memory_id"],
            kind=kind,
            content=data["content"],
            source_thread_id=data["source_thread_id"],
        )


class MemoryStore(Protocol):
    """记忆存储抽象接口。"""

    def add(self, memory: Memory) -> Memory | None:
        """添加一条记忆，memory_id 必须唯一。返回被淘汰的记忆，无淘汰时返回 None。"""
        ...

    def update(self, memory: Memory) -> None:
        """更新已有记忆，memory_id 必须存在。"""
        ...

    def get(self, memory_id: str) -> Memory:
        """根据 memory_id 获取记忆，不存在则抛出 MemoryNotFoundError。"""
        ...

    def delete(self, memory_id: str) -> None:
        """根据 memory_id 删除记忆，不存在则抛出 MemoryNotFoundError。"""
        ...

    def list_memories(self) -> list[Memory]:
        """返回所有记忆，按 memory_id 排序。"""
        ...

    def list_by_kind(self, kind: str) -> list[Memory]:
        """返回指定类型的记忆，按 memory_id 排序。"""
        ...

    def list_by_source_thread(self, thread_id: str) -> list[Memory]:
        """返回指定来源会话的记忆，按 memory_id 排序。"""
        ...

    def exists(self, memory_id: str) -> bool:
        """检查记忆是否存在。"""
        ...


class InMemoryMemoryStore:
    """基于内存的 MemoryStore 实现，支持容量限制和 FIFO 淘汰。"""

    __slots__ = ("_memories", "_insertion_order", "_max_memories")

    def __init__(self, max_memories: int | None = None) -> None:
        self._memories: dict[str, Memory] = {}
        self._insertion_order: list[str] = []
        self._max_memories = self._validate_max_memories(max_memories)

    def _validate_max_memories(self, value: int | None) -> int | None:
        """验证 max_memories 参数。"""
        if value is None:
            return None
        if isinstance(value, bool):
            raise TypeError("max_memories 不能是 bool 类型")
        if not isinstance(value, int):
            raise TypeError(
                f"max_memories 必须是 int 或 None，收到: {type(value).__name__}"
            )
        if value <= 0:
            raise ValueError(f"max_memories 必须大于 0，收到: {value}")
        return value

    def add(self, memory: Memory) -> Memory | None:
        """添加一条记忆，超容量时 FIFO 淘汰最旧的记忆。

        Returns:
            被淘汰的记忆，没有淘汰时返回 None。
        """
        self._validate_memory(memory)

        if memory.memory_id in self._memories:
            raise MemoryAlreadyExistsError(memory.memory_id)

        evicted: Memory | None = None

        if (
            self._max_memories is not None
            and len(self._memories) >= self._max_memories
        ):
            oldest_id = self._insertion_order.pop(0)
            evicted = self._memories.pop(oldest_id)

        self._memories[memory.memory_id] = memory
        self._insertion_order.append(memory.memory_id)

        return evicted

    def _validate_memory(self, memory: object) -> None:
        """验证参数是 Memory 类型。"""
        if not isinstance(memory, Memory):
            raise TypeError(f"期望 Memory 类型，收到: {type(memory).__name__}")

    def update(self, memory: Memory) -> None:
        self._validate_memory(memory)
        if memory.memory_id not in self._memories:
            raise MemoryNotFoundError(memory.memory_id)
        # 不改变插入顺序
        self._memories[memory.memory_id] = memory

    def get(self, memory_id: str) -> Memory:
        if memory_id not in self._memories:
            raise MemoryNotFoundError(memory_id)
        return self._memories[memory_id]

    def delete(self, memory_id: str) -> None:
        if memory_id not in self._memories:
            raise MemoryNotFoundError(memory_id)
        del self._memories[memory_id]
        self._insertion_order.remove(memory_id)

    def list_memories(self) -> list[Memory]:
        return sorted(self._memories.values(), key=lambda m: m.memory_id)

    def list_by_kind(self, kind: str) -> list[Memory]:
        return sorted(
            (m for m in self._memories.values() if m.kind == kind),
            key=lambda m: m.memory_id,
        )

    def list_by_source_thread(self, thread_id: str) -> list[Memory]:
        return sorted(
            (m for m in self._memories.values() if m.source_thread_id == thread_id),
            key=lambda m: m.memory_id,
        )

    def exists(self, memory_id: str) -> bool:
        return memory_id in self._memories
