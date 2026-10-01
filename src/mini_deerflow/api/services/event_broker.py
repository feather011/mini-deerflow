"""RunEventBroker（19D 阶段）。

线程安全的事件分发器：
- 每个 run_id 关联一个 Subscription（含 queue、lock、terminal_published）；
- subscribe 必须在 Agent 任务启动之前完成；
- unsubscribe 幂等；不重新创建队列；
- 终态事件由 publish_terminal 发布；保证终态进入队列（满则丢弃旧事件）；
- 普通 publish 用 put_nowait；满则丢弃；
- Consumer 收到终态事件后立即 return；不等待额外哨兵。
"""
from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from typing import Iterator

from mini_deerflow.api.schemas.streaming import RunEvent, RunEventType


_LOGGER = logging.getLogger(__name__)


_DEFAULT_QUEUE_MAXSIZE = 1024


# ---------------------------------------------------------------- subscription


@dataclass
class Subscription:
    """per-run_id 订阅状态。线程安全。"""
    queue: queue.Queue[RunEvent | object]
    lock: threading.Lock
    terminal_published: bool = False


# ---------------------------------------------------------------- broker


class RunEventBroker:
    """per-run_id 的事件队列管理器。"""

    def __init__(self, queue_maxsize: int = _DEFAULT_QUEUE_MAXSIZE) -> None:
        self._subs: dict[str, Subscription] = {}
        self._maxsize = queue_maxsize
        self._lock = threading.Lock()
        self._closed = False
        self._sequences: dict[str, int] = {}
        self._seq_lock = threading.Lock()

    # ---------------------------------------------------------------- sequence

    def next_sequence(self, run_id: str) -> int:
        with self._seq_lock:
            self._sequences[run_id] = self._sequences.get(run_id, -1) + 1
            return self._sequences[run_id]

    # ---------------------------------------------------------------- subscribe / unsubscribe

    def subscribe(self, run_id: str) -> queue.Queue[RunEvent | object]:
        """为 run_id 创建订阅。必须在 publish 之前调用。"""
        with self._lock:
            if self._closed:
                raise RuntimeError("Broker 已关闭")
            existing = self._subs.get(run_id)
            if existing is not None:
                return existing.queue
            q: queue.Queue[RunEvent | object] = queue.Queue(maxsize=self._maxsize)
            self._subs[run_id] = Subscription(queue=q, lock=threading.Lock())
            return q

    def unsubscribe(self, run_id: str) -> None:
        """释放订阅。幂等；不等待 worker。"""
        with self._lock:
            self._subs.pop(run_id, None)

    # ---------------------------------------------------------------- publish_terminal（核心保证：终态不丢失）

    def publish_terminal(self, run_id: str, event: RunEvent) -> bool:
        """发布终态事件。

        语义保证（即使 queue_size=1）：
        - 队列满时丢弃最旧的中间事件，腾出空间入队终态；
        - 使用 get_nowait + put_nowait 循环，永不永久阻塞；
        - 已终态则立即返回 False；
        - 队列不存在（unsubscribed）则立即返回 False；
        - terminal_published 在成功入队后才设置（在锁内）；
        - 每个 run 只接受一个终态。

        put_nowait 和 get_nowait 都不阻塞，可安全地在同一锁中完成整个状态转换。

        Returns:
            True  — 终态成功进入队列；
            False — 队列不存在或已发过终态。
        """
        sub = self._get_sub(run_id)
        if sub is None:
            return False

        with sub.lock:
            if sub.terminal_published:
                return False

            while True:
                try:
                    sub.queue.put_nowait(event)
                    sub.terminal_published = True
                    return True
                except queue.Full:
                    try:
                        sub.queue.get_nowait()
                    except queue.Empty:
                        # Consumer 恰好取走了事件；重新尝试即可
                        continue

    # ---------------------------------------------------------------- publish（普通事件）

    def publish(self, run_id: str, event: RunEvent) -> bool:
        """发布普通（非终态）事件。满则丢弃。

        terminal_published 检查与 put_nowait 在同一临界区。
        """
        sub = self._get_sub(run_id)
        if sub is None:
            return False
        with sub.lock:
            if sub.terminal_published:
                return False
            try:
                sub.queue.put_nowait(event)
                return True
            except queue.Full:
                return False

    def publish_overflow(self, run_id: str) -> bool:
        """溢出时发送一条 overflow 终态事件。"""
        overflow = RunEvent(
            run_id=run_id,
            thread_id="",
            event_type=RunEventType.RUN_FAILED,
            sequence=0,
            error="事件队列溢出；流被强制终止",
        )
        return self.publish_terminal(run_id, overflow)

    # ---------------------------------------------------------------- internal helpers

    def _get_sub(self, run_id: str) -> Subscription | None:
        """获取订阅（无锁，调用方决定是否需要锁）。"""
        with self._lock:
            if self._closed:
                return None
            return self._subs.get(run_id)

    # ---------------------------------------------------------------- shutdown

    def close(self) -> None:
        """关闭 broker：清空所有订阅；不再接受新订阅。幂等。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            subs = list(self._subs.values())
            self._subs.clear()

        for sub in subs:
            try:
                sub.queue.put_nowait(_SHUTDOWN_SENTINEL)
            except queue.Full:
                pass

    def has_subscribers(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._subs

    def clear(self) -> None:
        """清空所有订阅。测试用。"""
        with self._lock:
            self._subs.clear()
            self._closed = False

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"active_runs": len(self._subs)}


# 关闭哨兵：仅在 broker.close() 时使用
_SHUTDOWN_SENTINEL = object()


def _is_terminal_event(event_type: RunEventType) -> bool:
    return event_type in {
        RunEventType.RUN_COMPLETED,
        RunEventType.RUN_FAILED,
        RunEventType.RUN_TIMED_OUT,
        RunEventType.RUN_CANCELLED,
    }


# ---------------------------------------------------------------- iterator


def iter_run_events(
    q: queue.Queue[RunEvent | object],
    *,
    poll_timeout: float = 0.5,
) -> Iterator[RunEvent]:
    """从队列拉取事件；收到终态 RunEvent 或关闭哨兵则退出。"""
    while True:
        try:
            item = q.get(timeout=poll_timeout)
        except queue.Empty:
            continue
        if item is _SHUTDOWN_SENTINEL:
            return
        if isinstance(item, RunEvent):
            yield item
            if _is_terminal_event(item.event_type):
                return
        else:
            _LOGGER.warning("未知事件类型: %r", item)
            continue
