"""长期记忆精确去重（14E1 阶段）。"""

from __future__ import annotations

from typing import Protocol, Sequence

from mini_deerflow.memory import Memory


class MemoryDeduplicator(Protocol):
    """记忆去重接口。"""

    def filter_new(
        self,
        candidates: Sequence[Memory],
        existing: Sequence[Memory],
    ) -> list[Memory]:
        """从候选记忆中过滤出真正需要新增的记忆。

        Args:
            candidates: 待检查的候选记忆
            existing: Store 中已有的记忆

        Returns:
            真正需要新增的记忆列表（不包含与已有记忆重复的内容）
        """
        ...


def normalize_memory_content(content: str) -> str:
    """标准化记忆内容用于去重比较。

    规则：
    1. 去掉首尾空白
    2. 把连续空格、Tab、换行压缩成一个普通空格
    3. 使用 casefold 处理英文字母大小写

    注意：
    - 不修改标点
    - 不进行中文分词
    - 不进行语义理解

    Examples:
        >>> normalize_memory_content("  用户喜欢使用  UV  ")
        '用户喜欢使用 uv'
        >>> normalize_memory_content("用户\\n喜欢\\tpoetry")
        '用户喜欢 poetry'
    """
    return " ".join(content.split()).casefold()


def memory_dedup_key(memory: Memory) -> tuple[str, str]:
    """计算记忆的去重键。

    去重键只包含 kind 和标准化后的 content，
    不包含 memory_id 和 source_thread_id。

    Args:
        memory: 待计算的 Memory 对象

    Returns:
        (kind, normalized_content) 元组
    """
    return (
        memory.kind,
        normalize_memory_content(memory.content),
    )


class ExactMemoryDeduplicator:
    """基于内容精确匹配的去重器。"""

    def filter_new(
        self,
        candidates: Sequence[Memory],
        existing: Sequence[Memory],
    ) -> list[Memory]:
        """从候选记忆中过滤出真正需要新增的记忆。

        执行过程：
        1. 根据 existing 建立已有去重键集合
        2. 按 candidates 原始顺序遍历
        3. 如果候选 key 已存在，跳过
        4. 如果不存在，加入结果并添加到已见集合
        5. 返回新的列表

        Args:
            candidates: 待检查的候选记忆
            existing: Store 中已有的记忆

        Returns:
            真正需要新增的记忆列表

        Raises:
            TypeError: 参数中存在非 Memory 对象
        """
        # 验证参数类型
        for i, mem in enumerate(candidates):
            if not isinstance(mem, Memory):
                raise TypeError(
                    f"candidates[{i}] 必须是 Memory 类型，收到: {type(mem).__name__}"
                )

        for i, mem in enumerate(existing):
            if not isinstance(mem, Memory):
                raise TypeError(
                    f"existing[{i}] 必须是 Memory 类型，收到: {type(mem).__name__}"
                )

        # 建立已有去重键集合
        seen_keys: set[tuple[str, str]] = {
            memory_dedup_key(memory)
            for memory in existing
        }

        result: list[Memory] = []

        for memory in candidates:
            key = memory_dedup_key(memory)

            if key in seen_keys:
                continue

            seen_keys.add(key)
            result.append(memory)

        return result
