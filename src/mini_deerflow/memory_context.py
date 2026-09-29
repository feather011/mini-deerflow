"""长期记忆上下文构建（14D2 阶段）。"""

from __future__ import annotations

from typing import Protocol

from mini_deerflow.memory import Memory, MemoryStore


class MemoryContextProvider(Protocol):
    """可以提供长期记忆上下文的对象。"""

    def build(self) -> str:
        """构建并返回长期记忆上下文文本。"""
        ...


# 类型展示顺序和中文标题
_SECTION_ORDER: tuple[tuple[str, str], ...] = (
    ("instruction", "长期指令"),
    ("preference", "用户偏好"),
    ("fact", "相关事实"),
)


class MemoryContextBuilder:
    """将 MemoryStore 中的结构化记忆整理成文本上下文。"""

    __slots__ = ("_store",)

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    def build(self) -> str:
        """构建长期记忆上下文文本。

        执行流程：
        1. 调用 store.list_memories() 获取所有记忆
        2. 按 kind 分组
        3. 分类内部按 memory_id 排序
        4. 生成稳定的上下文文本

        Returns:
            长期记忆上下文文本，空时返回空字符串

        Raises:
            Store 抛出的任何异常原样传播
        """
        # 只调用一次 store.list_memories()
        memories = list(self._store.list_memories())

        if not memories:
            return ""

        sections: list[str] = []

        for kind, title in _SECTION_ORDER:
            # 筛选当前分类的记忆
            matching = [
                memory
                for memory in memories
                if memory.kind == kind
            ]

            if not matching:
                continue

            # 按 memory_id 排序
            matching.sort(key=lambda m: m.memory_id)

            # 构建当前分类的文本
            lines = [title]
            lines.extend(
                f"- {memory.content}"
                for memory in matching
            )
            sections.append("\n".join(lines))

        if not sections:
            return ""

        # 组装最终输出
        return "长期记忆\n\n" + "\n\n".join(sections)
