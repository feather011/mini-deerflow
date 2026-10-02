"""线程仓库接口与内存实现、SQLite 实现。"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Protocol, runtime_checkable

from mini_deerflow.thread_state import ThreadState


# --- 异常体系 ---


class ThreadStoreError(Exception):
    """线程仓库错误的基类。"""


class ThreadAlreadyExistsError(ThreadStoreError):
    """创建已存在的线程时抛出。"""


class ThreadNotFoundError(ThreadStoreError):
    """查找/保存/删除未知线程时抛出。"""


class ThreadStoreClosedError(ThreadStoreError):
    """关闭后继续操作 Store 时抛出。"""


class ThreadDataCorruptedError(ThreadStoreError, ValueError):
    """数据库中的线程数据无法解析时抛出；错误信息包含对应 thread_id。"""


# --- 接口 ---


@runtime_checkable
class ThreadStore(Protocol):
    """线程仓库：管理 ThreadState 的创建、读取、更新、删除与列表。"""

    def create(self, thread_id: str) -> ThreadState: ...
    def get(self, thread_id: str) -> ThreadState: ...
    def save(self, thread: ThreadState) -> None: ...
    def delete(self, thread_id: str) -> None: ...
    def exists(self, thread_id: str) -> bool: ...
    def list_threads(self) -> tuple[ThreadState, ...]: ...


# --- 内存实现 ---


class InMemoryThreadStore:
    """线程仓库的内存实现，私有字典持有所有 ThreadState。"""

    def __init__(self) -> None:
        self._threads: dict[str, ThreadState] = {}

    def create(self, thread_id: str) -> ThreadState:
        """创建并保存新 Thread；重复 ID 抛出 ThreadAlreadyExistsError。"""
        if thread_id in self._threads:
            raise ThreadAlreadyExistsError(f"线程已存在: {thread_id}")
        thread = ThreadState(thread_id=thread_id)
        self._threads[thread_id] = thread
        return thread

    def get(self, thread_id: str) -> ThreadState:
        """获取已有 Thread；不存在时抛出 ThreadNotFoundError。"""
        if thread_id not in self._threads:
            raise ThreadNotFoundError(f"未知线程: {thread_id}")
        return self._threads[thread_id]

    def save(self, thread: ThreadState) -> None:
        """更新已有 Thread；非 ThreadState 抛 TypeError，未知 ID 抛 ThreadNotFoundError。"""
        if not isinstance(thread, ThreadState):
            raise TypeError(f"只接受 ThreadState，收到: {type(thread).__name__}")
        if thread.thread_id not in self._threads:
            raise ThreadNotFoundError(f"未知线程: {thread.thread_id}")
        self._threads[thread.thread_id] = thread

    def delete(self, thread_id: str) -> None:
        """删除已有 Thread；不存在时抛出 ThreadNotFoundError。"""
        if thread_id not in self._threads:
            raise ThreadNotFoundError(f"未知线程: {thread_id}")
        del self._threads[thread_id]

    def exists(self, thread_id: str) -> bool:
        """检查 Thread 是否存在；从不抛出 ThreadNotFoundError。"""
        return thread_id in self._threads

    def list_threads(self) -> tuple[ThreadState, ...]:
        """按 thread_id 排序返回所有 Thread 的不可变快照。"""
        return tuple(sorted(self._threads.values(), key=lambda t: t.thread_id))


# --- SQLite 实现 ---


class SQLiteThreadStore:
    """基于 SQLite 的 ThreadStore 实现：线程状态以 JSON 形式持久化到数据库。

    线程安全（23）：FastAPI 的同步路由跑在线程池里，**同一个连接会被多个
    线程使用**，而 sqlite3 默认 `check_same_thread=True` 会直接抛
    `SQLite objects created in a thread can only be used in that same thread`。
    因此这里同时做两件事：

    - `check_same_thread=False`：允许跨线程使用（否则 API 侧根本跑不起来）
    - `self._lock`：跨线程共用一个连接必须串行，否则并发写会
      `database is locked` 或写坏状态

    CLI 是单线程的，加锁对它没有额外开销（RLock 未争用时就是一次 CAS）。
    """

    def __init__(self, database_path: str | Path) -> None:
        self._path = Path(database_path)
        self._closed = False
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS threads ("
                "  thread_id TEXT PRIMARY KEY,"
                "  state_json TEXT NOT NULL"
                ")"
            )
            self._conn.commit()

    def __enter__(self) -> "SQLiteThreadStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        """关闭数据库连接；重复调用安全无副作用。"""
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def _ensure_open(self) -> None:
        """操作前检查连接；已关闭时抛出清晰错误。"""
        if self._closed:
            raise ThreadStoreClosedError(
                f"Store 已关闭，无法操作: {self._path}"
            )

    def _serialize(self, thread: ThreadState) -> str:
        return json.dumps(thread.to_dict(), ensure_ascii=False)

    def _deserialize(self, raw: str, thread_id: str) -> ThreadState:
        """解析 state_json；任何非法内容都包装为 ThreadDataCorruptedError。

        检查项：JSON 合法性、顶层类型、schema_version、thread_id 一致性、
        messages 列表及每条 Message 的合法性。
        """
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ThreadDataCorruptedError(
                f"线程 {thread_id} 的数据损坏: JSON 非法 ({exc})"
            ) from exc

        if not isinstance(data, dict):
            raise ThreadDataCorruptedError(
                f"线程 {thread_id} 的数据损坏: JSON 顶层应为字典，"
                f"实际为 {type(data).__name__}"
            )

        try:
            restored = ThreadState.from_dict(data)
        except ValueError as exc:
            raise ThreadDataCorruptedError(
                f"线程 {thread_id} 的数据损坏: {exc}"
            ) from exc

        # 数据内部 thread_id 必须与数据库主键一致，拒绝静默返回错误会话
        if restored.thread_id != thread_id:
            raise ThreadDataCorruptedError(
                f"线程 {thread_id} 的数据损坏: 内部 thread_id="
                f"{restored.thread_id!r} 与数据库主键不一致"
            )

        return restored

    def create(self, thread_id: str) -> ThreadState:
        """创建新 Thread；重复 ID 抛出 ThreadAlreadyExistsError。"""
        thread = ThreadState(thread_id=thread_id)
        with self._lock:
            self._ensure_open()
            try:
                with self._conn:
                    self._conn.execute(
                        "INSERT INTO threads (thread_id, state_json) VALUES (?, ?)",
                        (thread_id, self._serialize(thread)),
                    )
            except sqlite3.IntegrityError:
                raise ThreadAlreadyExistsError(f"线程已存在: {thread_id}") from None
        return thread

    def get(self, thread_id: str) -> ThreadState:
        """获取已有 Thread；不存在时抛出 ThreadNotFoundError。"""
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT state_json FROM threads WHERE thread_id = ?", (thread_id,)
            ).fetchone()
        if row is None:
            raise ThreadNotFoundError(f"未知线程: {thread_id}")
        return self._deserialize(row[0], thread_id)

    def save(self, thread: ThreadState) -> None:
        """更新已有 Thread；非 ThreadState 抛 TypeError，未知 ID 抛 ThreadNotFoundError。

        整个写入在事务中执行：成功自动 commit，失败自动 rollback，
        不会出现只写入一半的数据。
        """
        if not isinstance(thread, ThreadState):
            raise TypeError(f"只接受 ThreadState，收到: {type(thread).__name__}")
        with self._lock:
            self._ensure_open()
            with self._conn:
                cursor = self._conn.execute(
                    "UPDATE threads SET state_json = ? WHERE thread_id = ?",
                    (self._serialize(thread), thread.thread_id),
                )
                if cursor.rowcount == 0:
                    raise ThreadNotFoundError(f"未知线程: {thread.thread_id}")

    def delete(self, thread_id: str) -> None:
        """删除已有 Thread；不存在时抛出 ThreadNotFoundError。"""
        with self._lock:
            self._ensure_open()
            with self._conn:
                cursor = self._conn.execute(
                    "DELETE FROM threads WHERE thread_id = ?", (thread_id,)
                )
                if cursor.rowcount == 0:
                    raise ThreadNotFoundError(f"未知线程: {thread_id}")

    def exists(self, thread_id: str) -> bool:
        """检查 Thread 是否存在；从不抛出 ThreadNotFoundError。"""
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT 1 FROM threads WHERE thread_id = ? LIMIT 1", (thread_id,)
            ).fetchone()
        return row is not None

    def list_threads(self) -> tuple[ThreadState, ...]:
        """按 thread_id 排序返回所有 Thread；任一记录损坏则抛 ThreadDataCorruptedError。"""
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                "SELECT thread_id, state_json FROM threads ORDER BY thread_id"
            ).fetchall()
        return tuple(self._deserialize(raw, tid) for tid, raw in rows)