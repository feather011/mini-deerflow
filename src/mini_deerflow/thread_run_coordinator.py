"""线程运行协调器（19C 阶段 + 19D 阶段循环依赖分离）。

从 runtime.py 抽离，让 api.services 不必反向依赖 runtime 整个模块。
"""
from __future__ import annotations

from threading import Lock


class ThreadRunConflictError(Exception):
    """同一 thread_id 已被占用时抛出。"""

    def __init__(self, thread_id: str, running_run_id: str) -> None:
        super().__init__(
            f"线程 {thread_id} 正在运行（run_id={running_run_id}）"
        )
        self.thread_id = thread_id
        self.running_run_id = running_run_id


class ThreadRunCoordinator:
    """进程内的"每线程单 run"协调器。

    - 同 thread_id 同时只能有一个 run；
    - 不同 thread_id 互不阻塞；
    - try/finally 或上下文管理器释放占用；
    - 锁表不得无限增长：release() 移除条目；shutdown() 清空。

    多 Uvicorn worker / 多进程部署需要数据库锁或分布式锁；
    当前实现仅支持单进程。
    """

    def __init__(self) -> None:
        self._running: dict[str, str] = {}
        self._lock = Lock()

    def try_acquire(self, thread_id: str, run_id: str) -> None:
        """占用 thread_id；已被占用时抛 ThreadRunConflictError。

        Args:
            thread_id: 目标线程。
            run_id: 本次 run 的 ID，用于诊断冲突时报告。
        """
        with self._lock:
            existing = self._running.get(thread_id)
            if existing is not None and existing != run_id:
                raise ThreadRunConflictError(thread_id, existing)
            self._running[thread_id] = run_id

    def release(self, thread_id: str, run_id: str | None = None) -> None:
        """释放 thread_id 占用。

        Args:
            thread_id: 目标线程。
            run_id: 若提供，仅当当前记录匹配时才移除（防误删）。
        """
        with self._lock:
            if run_id is None:
                self._running.pop(thread_id, None)
                return
            current = self._running.get(thread_id)
            if current == run_id:
                del self._running[thread_id]

    def is_running(self, thread_id: str) -> bool:
        """判断 thread_id 是否正在被占用。"""
        with self._lock:
            return thread_id in self._running

    def running_count(self) -> int:
        """当前占用数量（测试 / 诊断用）。"""
        with self._lock:
            return len(self._running)

    def get_running(self, thread_id: str) -> str | None:
        """TOOL-05：返回当前占用 thread_id 的 run_id；无占用返回 None。

        加锁读，与 try_acquire/release 互斥，避免读到中间状态。
        """
        with self._lock:
            return self._running.get(thread_id)

    def shutdown(self) -> None:
        """清空占用表；幂等，重复调用安全。"""
        with self._lock:
            self._running.clear()
