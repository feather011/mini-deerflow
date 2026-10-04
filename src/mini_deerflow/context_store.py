"""Phase 24 ContextStore：独立持久化 ContextState。

为什么单独一张表、不塞回 ThreadState JSON：

- ThreadState 是**事实源**，每次 Agent 成功轮次都会整体重写；
  把投影塞进去会让「压缩水位」跟着消息一起被覆盖，CAS 也无从谈起。
- 摘要与 Task Notes 的写入频率、并发方（工具调用 vs 成功轮次）与消息不同，
  分表后两边可以各自做乐观并发控制。
- 分表给未来迁移留了独立的 `schema_version` 与 ALTER 空间。

CAS 语义：`save` 必须带 `expected_revision`，匹配才写入，否则抛
`ContextConflictError`。绝不「最后写入者赢」——那会静默吃掉并发 Run 写的
Task Note。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from mini_deerflow.context_errors import (
    ContextConflictError,
    ContextDataCorruptedError,
    ContextNotFoundError,
    ContextSchemaVersionError,
)
from mini_deerflow.context_state import (
    CONTEXT_SCHEMA_VERSION,
    ContextState,
)

# 迁移注册表：target_version -> callable(state_dict) -> state_dict
# 第一版只有 1，因此是空表；保留结构是为了让 v2 出现时有明确落点，
# 而不是在读取处散落 if version == 2 的分支。
MIGRATIONS: dict[int, Callable[[dict], dict]] = {}


class ContextStore(Protocol):
    """ContextState 持久化协议。"""

    def get(self, thread_id: str) -> ContextState:
        """读取；不存在抛 ContextNotFoundError。"""
        ...

    def save(
        self,
        thread_id: str,
        state: ContextState,
        *,
        expected_revision: int,
    ) -> ContextState:
        """CAS 写入；revision 不匹配抛 ContextConflictError。

        返回**写入后**的状态（revision 已递增）。
        """
        ...

    def delete(self, thread_id: str) -> None:
        """删除；线程删除时必须同步调用。不存在时静默返回（幂等）。"""
        ...

    def exists(self, thread_id: str) -> bool:
        """是否存在该 ContextState。"""
        ...

    def create(self, thread_id: str) -> ContextState:
        """初始化一份空 ContextState（新线程 / Subagent 启动时调用）。

        已存在时抛 `ContextConflictError`，调用方据此改走读取路径。
        """
        ...


def cas_update(
    store: ContextStore,
    thread_id: str,
    mutator: Callable[[ContextState], ContextState],
    *,
    max_retries: int = 3,
) -> ContextState:
    """读-改-写的 CAS 循环：冲突时最多自动重试 `max_retries` 次。

    总尝试次数 = 1（首次）+ max_retries。全部冲突后把最后一次的
    `ContextConflictError` 原样抛出，不吞异常、不返回过期数据。
    """
    if max_retries < 0:
        raise ValueError("max_retries 不能为负")
    last_error: ContextConflictError | None = None
    for attempt in range(max_retries + 1):
        current = store.get(thread_id)
        candidate = mutator(current)
        if not isinstance(candidate, ContextState):
            raise TypeError(
                f"mutator 必须返回 ContextState，收到 {type(candidate).__name__}"
            )
        try:
            return store.save(
                thread_id,
                candidate,
                expected_revision=current.revision,
            )
        except ContextConflictError as exc:
            last_error = exc
            if attempt == max_retries:
                break
    assert last_error is not None
    raise last_error


# ---------------------------------------------------------------------------
# 内存实现
# ---------------------------------------------------------------------------


class InMemoryContextStore:
    """内存实现。测试与未配置持久化时的默认选择。"""

    def __init__(self) -> None:
        self._states: dict[str, ContextState] = {}
        self._lock = threading.RLock()
        self._closed = False

    def close(self) -> None:
        """标记为已关闭并丢弃全部状态；幂等。

        与 SQLite 实现同一语义：Runtime shutdown 后再访问必须明确失败，
        而不是继续返回内存里的旧数据——否则「已关闭」只是句口号。
        """
        with self._lock:
            self._closed = True
            self._states.clear()

    def _ensure_open(self) -> None:
        if self._closed:
            raise ContextNotFoundError("ContextStore 已关闭")

    def get(self, thread_id: str) -> ContextState:
        with self._lock:
            self._ensure_open()
            state = self._states.get(thread_id)
        if state is None:
            raise ContextNotFoundError(thread_id)
        return state

    def save(
        self,
        thread_id: str,
        state: ContextState,
        *,
        expected_revision: int,
    ) -> ContextState:
        if not isinstance(state, ContextState):
            raise TypeError(f"只接受 ContextState，收到 {type(state).__name__}")
        # state.revision 必须与调用方声称的基准版本一致。
        # 少了这道校验，「mutator 没有基于传入的 state 构造」这种错误会
        # 静默写入过期内容，把并发方刚写的 Task Note 冲掉——而且 CAS
        # 看起来还是成功的。
        if state.revision != expected_revision:
            raise ContextConflictError(
                thread_id, expected=expected_revision, actual=state.revision
            )
        with self._lock:
            self._ensure_open()
            current = self._states.get(thread_id)
            if current is None:
                raise ContextNotFoundError(thread_id)
            if current.revision != expected_revision:
                raise ContextConflictError(
                    thread_id,
                    expected=expected_revision,
                    actual=current.revision,
                )
            written = replace(state, revision=expected_revision + 1)
            self._states[thread_id] = written
            return written

    def delete(self, thread_id: str) -> None:
        with self._lock:
            self._ensure_open()
            self._states.pop(thread_id, None)

    def exists(self, thread_id: str) -> bool:
        with self._lock:
            self._ensure_open()
            return thread_id in self._states

    def create(self, thread_id: str) -> ContextState:
        """显式初始化（新线程 / Subagent 启动时调用）。重复创建抛冲突。"""
        with self._lock:
            self._ensure_open()
            if thread_id in self._states:
                raise ContextConflictError(thread_id, expected=0, actual=None)
            state = ContextState()
            self._states[thread_id] = state
            return state


# ---------------------------------------------------------------------------
# SQLite 实现
# ---------------------------------------------------------------------------


class SQLiteContextStore:
    """SQLite 实现：独立表 + 单连接 + RLock。

    线程安全理由与 `SQLiteThreadStore` 一致：FastAPI 的同步路由跑在线程池，
    同一个连接会被多线程复用。
    """

    def __init__(self, database_path: str | Path) -> None:
        self._path = Path(database_path)
        self._closed = False
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS context_states ("
                "  thread_id TEXT PRIMARY KEY,"
                "  revision INTEGER NOT NULL,"
                "  schema_version INTEGER NOT NULL,"
                "  payload TEXT NOT NULL,"
                "  updated_at TEXT NOT NULL"
                ")"
            )
            self._conn.commit()

    def __enter__(self) -> "SQLiteContextStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise ContextNotFoundError(f"ContextStore 已关闭: {self._path}")

    @staticmethod
    def _serialize(state: ContextState) -> str:
        return json.dumps(state.to_dict(), ensure_ascii=False)

    @staticmethod
    def _deserialize(raw: str, thread_id: str) -> ContextState:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ContextDataCorruptedError(
                thread_id, f"JSON 非法 ({exc})"
            ) from exc
        if not isinstance(data, dict):
            raise ContextDataCorruptedError(
                thread_id, f"顶层应为字典，实际 {type(data).__name__}"
            )
        version = data.get("schema_version", CONTEXT_SCHEMA_VERSION)
        if isinstance(version, bool) or not isinstance(version, int):
            raise ContextDataCorruptedError(thread_id, "schema_version 非整数")
        if version == CONTEXT_SCHEMA_VERSION:
            try:
                return ContextState.from_dict(data)
            except ValueError as exc:
                raise ContextDataCorruptedError(thread_id, str(exc)) from exc
        # 迁移路径：有迁移表就逐级升到当前版本
        migrated = data
        while version < CONTEXT_SCHEMA_VERSION:
            step = MIGRATIONS.get(version)
            if step is None:
                raise ContextSchemaVersionError(
                    thread_id, found=version, supported=CONTEXT_SCHEMA_VERSION
                )
            migrated = step(migrated)
            version = migrated.get("schema_version", version)
            if not isinstance(version, int):
                raise ContextDataCorruptedError(thread_id, "迁移后 schema_version 非整数")
        if version > CONTEXT_SCHEMA_VERSION:
            raise ContextSchemaVersionError(
                thread_id, found=version, supported=CONTEXT_SCHEMA_VERSION
            )
        try:
            return ContextState.from_dict(migrated)
        except ValueError as exc:
            raise ContextDataCorruptedError(thread_id, str(exc)) from exc

    def create(self, thread_id: str) -> ContextState:
        """显式初始化一行；已存在时抛 ContextConflictError。"""
        state = ContextState()
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._ensure_open()
            try:
                with self._conn:
                    self._conn.execute(
                        "INSERT INTO context_states "
                        "(thread_id, revision, schema_version, payload, updated_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            thread_id,
                            state.revision,
                            state.schema_version,
                            self._serialize(state),
                            now,
                        ),
                    )
            except sqlite3.IntegrityError:
                raise ContextConflictError(thread_id, expected=0, actual=None) from None
        return state

    def get(self, thread_id: str) -> ContextState:
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT payload FROM context_states WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        if row is None:
            raise ContextNotFoundError(thread_id)
        return self._deserialize(row[0], thread_id)

    def save(
        self,
        thread_id: str,
        state: ContextState,
        *,
        expected_revision: int,
    ) -> ContextState:
        if not isinstance(state, ContextState):
            raise TypeError(f"只接受 ContextState，收到 {type(state).__name__}")
        # 与 InMemory 实现同一道校验：state 必须基于调用方声称的基准版本。
        if state.revision != expected_revision:
            raise ContextConflictError(
                thread_id, expected=expected_revision, actual=state.revision
            )
        written = replace(state, revision=expected_revision + 1)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._ensure_open()
            with self._conn:
                # 先读当前 revision，区分「不存在」与「版本不匹配」
                row = self._conn.execute(
                    "SELECT revision FROM context_states WHERE thread_id = ?",
                    (thread_id,),
                ).fetchone()
                if row is None:
                    raise ContextNotFoundError(thread_id)
                if row[0] != expected_revision:
                    raise ContextConflictError(
                        thread_id, expected=expected_revision, actual=row[0]
                    )
                cursor = self._conn.execute(
                    "UPDATE context_states SET revision = ?, schema_version = ?, "
                    "payload = ?, updated_at = ? "
                    "WHERE thread_id = ? AND revision = ?",
                    (
                        written.revision,
                        written.schema_version,
                        self._serialize(written),
                        now,
                        thread_id,
                        expected_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    # 走到这里说明读和写之间被并发改了（同库多进程场景）
                    raise ContextConflictError(
                        thread_id, expected=expected_revision, actual=None
                    )
        return written

    def delete(self, thread_id: str) -> None:
        with self._lock:
            self._ensure_open()
            with self._conn:
                self._conn.execute(
                    "DELETE FROM context_states WHERE thread_id = ?", (thread_id,)
                )

    def exists(self, thread_id: str) -> bool:
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT 1 FROM context_states WHERE thread_id = ?", (thread_id,)
            ).fetchone()
        return row is not None


__all__ = [
    "MIGRATIONS",
    "ContextStore",
    "InMemoryContextStore",
    "SQLiteContextStore",
    "cas_update",
]
