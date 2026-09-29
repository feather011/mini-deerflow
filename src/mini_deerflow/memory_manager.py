"""记忆协调服务（14D1 阶段）。"""

from __future__ import annotations

from typing import Protocol, Sequence

from mini_deerflow.memory import (
    Memory,
    MemoryAlreadyExistsError,
    MemoryContentConflictError,
    MemoryStore,
)
from mini_deerflow.memory_dedup import ExactMemoryDeduplicator, MemoryDeduplicator
from mini_deerflow.memory_extractor import MemoryExtractor
from mini_deerflow.models import Message


class MemoryProcessor(Protocol):
    """从消息中提取并保存记忆的接口。"""

    def extract_and_save(
        self,
        source_thread_id: str,
        messages: Sequence[Message],
    ) -> list[Memory]:
        """提取记忆并保存到 store。"""
        ...


class MemoryManager:
    """连接 MemoryExtractor 和 MemoryStore 的协调服务，支持记忆去重。"""

    __slots__ = ("_extractor", "_store", "_deduplicator")

    def __init__(
        self,
        extractor: MemoryExtractor,
        store: MemoryStore,
        deduplicator: MemoryDeduplicator | None = None,
    ) -> None:
        self._extractor = extractor
        self._store = store
        self._deduplicator = (
            deduplicator
            if deduplicator is not None
            else ExactMemoryDeduplicator()
        )

    def extract_and_save(
        self,
        source_thread_id: str,
        messages: Sequence[Message],
    ) -> list[Memory]:
        """提取记忆并保存到 store（支持内容去重）。

        执行流程：
        1. 调用 extractor.extract 提取候选记忆
        2. 如果没有候选，直接返回 []
        3. 调用 store.list_memories() 获取已有记忆
        4. 调用 deduplicator.filter_new 去重
        5. 如果全部重复，返回 []
        6. 对剩余 Memory 执行 ID 冲突检查
        7. 保存剩余 Memory
        8. 返回真正保存的 Memory 列表

        Args:
            source_thread_id: 来源会话 ID
            messages: 待分析的消息序列

        Returns:
            本次成功保存的记忆列表（去重后）

        Raises:
            MemoryAlreadyExistsError: 发现 ID 冲突时
            提取或存储过程中的其他异常也会原样传播
        """
        # 1. 调用 extractor.extract 提取候选记忆
        candidates = list(
            self._extractor.extract(
                source_thread_id=source_thread_id,
                messages=messages,
            )
        )

        # 2. 如果没有候选，直接返回
        if not candidates:
            return []

        # 3. 调用 store.list_memories() 获取已有记忆
        existing = list(self._store.list_memories())

        # 4. 调用 deduplicator.filter_new 去重
        new_memories = self._deduplicator.filter_new(
            candidates=candidates,
            existing=existing,
        )

        # 5. 如果全部重复，返回 []
        if not new_memories:
            return []

        # 6. 对剩余 Memory 执行 ID 冲突检查
        self._check_batch_conflicts(new_memories)

        # 7. 保存剩余 Memory，记录最终保留的 ID
        retained_ids = {memory.memory_id for memory in new_memories}
        for memory in new_memories:
            evicted = self._store.add(memory)
            # 如果被淘汰的是本批次新增的 Memory，从保留集合移除
            if evicted is not None and evicted.memory_id in retained_ids:
                retained_ids.remove(evicted.memory_id)

        # 8. 按原始候选顺序返回最终保留的 Memory
        return [memory for memory in new_memories if memory.memory_id in retained_ids]

    def _check_batch_conflicts(self, memories: list[Memory]) -> None:
        """检查批次内和批次与 store 间的 ID 冲突。

        Args:
            memories: 待保存的记忆列表

        Raises:
            MemoryAlreadyExistsError: 发现任何 ID 冲突时
        """
        seen_ids: set[str] = set()

        for memory in memories:
            # 检查本次提取结果中是否存在重复 ID
            if memory.memory_id in seen_ids:
                raise MemoryAlreadyExistsError(memory.memory_id)
            seen_ids.add(memory.memory_id)

            # 检查 MemoryStore 中是否已经存在相同 ID
            if self._store.exists(memory.memory_id):
                raise MemoryAlreadyExistsError(memory.memory_id)

    def update_memory(self, memory: Memory) -> Memory:
        """更新已有记忆。

        执行流程：
        1. 验证参数是 Memory
        2. store.get(memory.memory_id) 读取旧 Memory
        3. 如果新旧 Memory 完全相同，直接返回旧 Memory
        4. store.list_memories() 读取全部记忆
        5. 排除当前 memory_id 对应的旧 Memory
        6. 使用 deduplicator 检查新 Memory 是否与其他记忆重复
        7. 重复时抛出 MemoryContentConflictError
        8. 不重复时调用 store.update(memory)
        9. 返回更新后的 Memory

        Args:
            memory: 待更新的 Memory 对象

        Returns:
            更新后的 Memory 对象

        Raises:
            TypeError: 参数不是 Memory 类型
            MemoryNotFoundError: memory_id 不存在
            MemoryContentConflictError: 新内容与已有记忆重复
        """
        # 1. 验证参数是 Memory
        if not isinstance(memory, Memory):
            raise TypeError(f"memory 必须是 Memory 类型，收到: {type(memory).__name__}")

        # 2. 读取旧 Memory
        existing = self._store.get(memory.memory_id)

        # 3. 如果新旧 Memory 完全相同，直接返回
        if existing == memory:
            return existing

        # 4-5. 读取全部记忆，排除当前 memory_id
        other_memories = [
            item
            for item in self._store.list_memories()
            if item.memory_id != memory.memory_id
        ]

        # 6. 使用 deduplicator 检查是否重复
        filtered = self._deduplicator.filter_new(
            candidates=[memory],
            existing=other_memories,
        )

        # 7. 重复时抛出异常
        if not filtered:
            raise MemoryContentConflictError(memory.memory_id)

        # 8. 不重复时调用 store.update
        self._store.update(memory)

        # 9. 返回更新后的 Memory
        return memory
