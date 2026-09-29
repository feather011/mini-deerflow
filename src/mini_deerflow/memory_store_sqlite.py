"""SQLite 实现的 MemoryStore，支持容量限制和 FIFO 淘汰。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mini_deerflow.memory import Memory


class SQLiteMemoryStoreError(Exception):
    """SQLiteMemoryStore 错误的基类。"""


class MemoryStoreClosedError(SQLiteMemoryStoreError):
    """Store 关闭后继续操作时抛出。"""


class MemoryDataCorruptedError(SQLiteMemoryStoreError, ValueError):
    """数据库中的记忆数据损坏时抛出。"""


class SQLiteMemoryStore:
    """基于 SQLite 的 MemoryStore 实现，支持容量限制和 FIFO 淘汰。

    数据库表结构：
    - seq: 自增主键，用于维护 FIFO 插入顺序
    - memory_id: 记忆 ID，唯一约束
    - kind: 记忆类型
    - content: 记忆内容
    - source_thread_id: 来源会话 ID
    """

    def __init__(
        self,
        database_path: str | Path,
        max_memories: int | None = None,
    ) -> None:
        self._path = Path(database_path)
        self._closed = False
        self._max_memories = self._validate_max_memories(max_memories)
        self._conn = sqlite3.connect(self._path)
        self._init_db()

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

    def _init_db(self) -> None:
        """初始化数据库表。"""
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS memories ("
            "  seq INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  memory_id TEXT NOT NULL UNIQUE,"
            "  kind TEXT NOT NULL,"
            "  content TEXT NOT NULL,"
            "  source_thread_id TEXT NOT NULL"
            ")"
        )
        # 索引用于加速按 kind 和 source_thread_id 查询
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_source_thread "
            "ON memories(source_thread_id)"
        )
        self._conn.commit()

    def __enter__(self) -> "SQLiteMemoryStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        """关闭数据库连接；重复调用安全无副作用。"""
        if not self._closed:
            self._conn.close()
            self._closed = True

    def _ensure_open(self) -> None:
        """操作前检查连接状态。"""
        if self._closed:
            raise MemoryStoreClosedError(
                f"Store 已关闭，无法操作: {self._path}"
            )

    def _row_to_memory(self, row: tuple) -> "Memory":
        """从数据库行恢复 Memory 对象。"""
        from mini_deerflow.memory import ALLOWED_KINDS, Memory

        _, memory_id, kind, content, source_thread_id = row

        # 检查必要字段
        for name, value in [
            ("memory_id", memory_id),
            ("kind", kind),
            ("content", content),
            ("source_thread_id", source_thread_id),
        ]:
            if not isinstance(value, str) or not value.strip():
                raise MemoryDataCorruptedError(
                    f"记忆 {memory_id} 数据损坏: {name} 字段无效"
                )

        # 检查 kind
        if kind not in ALLOWED_KINDS:
            raise MemoryDataCorruptedError(
                f"记忆 {memory_id} 数据损坏: 未知 kind {kind!r}"
            )

        return Memory(
            memory_id=memory_id,
            kind=kind,
            content=content,
            source_thread_id=source_thread_id,
        )

    def add(self, memory: "Memory") -> "Memory | None":
        """添加一条记忆，超容量时 FIFO 淘汰最旧的记忆。

        Returns:
            被淘汰的记忆，没有淘汰时返回 None。
        """
        from mini_deerflow.memory import Memory, MemoryAlreadyExistsError

        self._ensure_open()

        if not isinstance(memory, Memory):
            raise TypeError(f"期望 Memory 类型，收到: {type(memory).__name__}")

        # 检查是否已存在
        existing = self._conn.execute(
            "SELECT seq, memory_id, kind, content, source_thread_id "
            "FROM memories WHERE memory_id = ?",
            (memory.memory_id,),
        ).fetchone()

        if existing is not None:
            raise MemoryAlreadyExistsError(memory.memory_id)

        evicted: Memory | None = None

        # 检查是否需要淘汰
        if self._max_memories is not None:
            count = self._conn.execute(
                "SELECT COUNT(*) FROM memories"
            ).fetchone()[0]

            if count >= self._max_memories:
                # 找出最旧的记录（seq 最小的）
                oldest = self._conn.execute(
                    "SELECT seq, memory_id, kind, content, source_thread_id "
                    "FROM memories ORDER BY seq ASC LIMIT 1"
                ).fetchone()

                if oldest is not None:
                    evicted = self._row_to_memory(oldest)

                    # 在同一事务中删除旧记录并插入新记录
                    with self._conn:
                        self._conn.execute(
                            "DELETE FROM memories WHERE seq = ?",
                            (oldest[0],)
                        )
                        self._conn.execute(
                            "INSERT INTO memories "
                            "(memory_id, kind, content, source_thread_id) "
                            "VALUES (?, ?, ?, ?)",
                            (
                                memory.memory_id,
                                memory.kind,
                                memory.content,
                                memory.source_thread_id,
                            ),
                        )
                    return evicted

        # 容量未满，直接插入
        with self._conn:
            self._conn.execute(
                "INSERT INTO memories "
                "(memory_id, kind, content, source_thread_id) "
                "VALUES (?, ?, ?, ?)",
                (
                    memory.memory_id,
                    memory.kind,
                    memory.content,
                    memory.source_thread_id,
                ),
            )

        return None

    def update(self, memory: "Memory") -> None:
        """更新已有记忆，memory_id 必须存在。"""
        from mini_deerflow.memory import Memory, MemoryNotFoundError

        self._ensure_open()

        if not isinstance(memory, Memory):
            raise TypeError(f"期望 Memory 类型，收到: {type(memory).__name__}")

        with self._conn:
            cursor = self._conn.execute(
                "UPDATE memories "
                "SET kind = ?, content = ?, source_thread_id = ? "
                "WHERE memory_id = ?",
                (
                    memory.kind,
                    memory.content,
                    memory.source_thread_id,
                    memory.memory_id,
                ),
            )
            if cursor.rowcount == 0:
                raise MemoryNotFoundError(memory.memory_id)

    def get(self, memory_id: str) -> "Memory":
        """根据 memory_id 获取记忆，不存在则抛出 MemoryNotFoundError。"""
        from mini_deerflow.memory import MemoryNotFoundError

        self._ensure_open()

        row = self._conn.execute(
            "SELECT seq, memory_id, kind, content, source_thread_id "
            "FROM memories WHERE memory_id = ?",
            (memory_id,),
        ).fetchone()

        if row is None:
            raise MemoryNotFoundError(memory_id)

        return self._row_to_memory(row)

    def delete(self, memory_id: str) -> None:
        """根据 memory_id 删除记忆，不存在则抛出 MemoryNotFoundError。"""
        from mini_deerflow.memory import MemoryNotFoundError

        self._ensure_open()

        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM memories WHERE memory_id = ?",
                (memory_id,),
            )
            if cursor.rowcount == 0:
                raise MemoryNotFoundError(memory_id)

    def list_memories(self) -> list["Memory"]:
        """返回所有记忆，按 memory_id 排序。"""
        self._ensure_open()

        rows = self._conn.execute(
            "SELECT seq, memory_id, kind, content, source_thread_id "
            "FROM memories ORDER BY memory_id"
        ).fetchall()

        return [self._row_to_memory(row) for row in rows]

    def list_by_kind(self, kind: str) -> list["Memory"]:
        """返回指定类型的记忆，按 memory_id 排序。"""
        self._ensure_open()

        rows = self._conn.execute(
            "SELECT seq, memory_id, kind, content, source_thread_id "
            "FROM memories WHERE kind = ? ORDER BY memory_id",
            (kind,),
        ).fetchall()

        return [self._row_to_memory(row) for row in rows]

    def list_by_source_thread(self, thread_id: str) -> list["Memory"]:
        """返回指定来源会话的记忆，按 memory_id 排序。"""
        self._ensure_open()

        rows = self._conn.execute(
            "SELECT seq, memory_id, kind, content, source_thread_id "
            "FROM memories WHERE source_thread_id = ? ORDER BY memory_id",
            (thread_id,),
        ).fetchall()

        return [self._row_to_memory(row) for row in rows]

    def exists(self, memory_id: str) -> bool:
        """检查记忆是否存在。"""
        self._ensure_open()

        row = self._conn.execute(
            "SELECT 1 FROM memories WHERE memory_id = ? LIMIT 1",
            (memory_id,),
        ).fetchone()

        return row is not None
