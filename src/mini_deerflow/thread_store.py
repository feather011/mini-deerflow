"""线程仓库接口与内存实现（5B 阶段）。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from mini_deerflow.thread_state import ThreadState


# --- 异常体系 ---


class ThreadStoreError(Exception):
    """线程仓库错误的基类。"""


class ThreadAlreadyExistsError(ThreadStoreError):
    """创建已存在的线程时抛出。"""


class ThreadNotFoundError(ThreadStoreError):
    """查找/保存/删除未知线程时抛出。"""


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