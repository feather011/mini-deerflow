"""Phase 25B LeadRunStore：状态快照与审计事件的原子持久化。

## 快照 + 事件，而不是完整 Event Sourcing

- **当前快照**（`LeadRunState`）用于**恢复**——重启后直接读出来继续用
- **追加事件**（`LeadDomainEvent`）用于**审计**——回答「这个 Run 经历过什么」

两者在**同一次 `save()` 内提交**：校验 revision → 写快照 → 递增 revision
→ 追加事件。任何一步失败整体回滚，绝不出现「状态推进了但审计没记」或
「事件记了但状态没动」的半截结果。

完整 Event Sourcing（事件作为唯一事实源、靠重放重建状态）不在本阶段范围：
它会让每次读取都变慢，而这里快照就是权威，事件只是旁证。

## 事件 sequence 由 Store 分配

调用方构造事件时不指定 sequence——`save()` 在事务内取
`len(events[run_id])` 作为新序号，因此**每个 run 单调递增且不可重复**，
并发写者之间也不会撞号。

本模块实现 `InMemoryLeadRunStore`；`SQLiteLeadRunStore` 在
`sqlite_store.py`，两者共用同一份契约测试。
"""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Protocol, runtime_checkable

from .errors import (
    EventRunMismatchError,
    LeadRunAlreadyExistsError,
    LeadRunConflictError,
    LeadRunNotFoundError,
    LeadStoreClosedError,
)
from .events import LeadDomainEvent
from .models import LeadRunState
from .state_machine import is_terminal

__all__ = [
    "DEFAULT_EVENT_PAGE",
    "MAX_EVENT_PAGE_LIMIT",
    "MAX_LIST_LIMIT",
    "InMemoryLeadRunStore",
    "LeadRunStore",
    "checked_limit",
    "checked_offset",
    "checked_thread_id",
]

# 列表上限：禁止无界读取。一个 Run 的状态快照体量不小，
# 无界 list_by_thread 会让一次误调用把整库读进内存。
MAX_LIST_LIMIT = 200
DEFAULT_EVENT_PAGE = 100
MAX_EVENT_PAGE_LIMIT = 500


def checked_limit(limit: int, *, max_limit: int, name: str = "limit") -> int:
    """校验分页 limit：必须是 [1, max_limit] 内的整数。"""
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError(f"{name} 必须是整数，收到 {limit!r}")
    if limit < 1 or limit > max_limit:
        raise ValueError(f"{name} 必须在 1..{max_limit}，收到 {limit}")
    return limit


def checked_offset(offset: int) -> int:
    """校验分页 offset：非负整数。"""
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise ValueError(f"offset 必须是整数，收到 {offset!r}")
    if offset < 0:
        raise ValueError(f"offset 不能为负，收到 {offset}")
    return offset


def checked_thread_id(state: LeadRunState) -> str:
    """Store 入口的防御性校验：`thread_id` 必须非空。

    `LeadRunState.__post_init__` 已经拦过空值，这里是**第二道**——
    领域模型可能被反序列化、复制或未来放宽构造器，Store 不接受空线程。
    """
    thread_id = state.thread_id
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError(
            f"LeadRunState.thread_id 必须是非空字符串（run_id={state.lead_run_id!r}）"
        )
    return thread_id


@runtime_checkable
class LeadRunStore(Protocol):
    """LeadRun 持久化协议。InMemory 与 SQLite 必须行为一致。"""

    def create(self, state: LeadRunState) -> None:
        """创建 Run；run_id 重复抛 LeadRunAlreadyExistsError。"""
        ...

    def get(self, run_id: str) -> LeadRunState:
        """读取快照；不存在抛 LeadRunNotFoundError。"""
        ...

    def save(
        self,
        state: LeadRunState,
        *,
        expected_revision: int,
        event: LeadDomainEvent,
    ) -> int:
        """CAS 写入并在同一事务内追加事件，返回递增后的 revision。"""
        ...

    def list_by_thread(
        self, thread_id: str, *, limit: int, offset: int
    ) -> list[LeadRunState]:
        """按 thread_id 分页列出 Run。"""
        ...

    def list_recoverable(self, *, limit: int) -> list[LeadRunState]:
        """列出**非终态** Run（重启后待恢复的那些）。"""
        ...

    def list_events(
        self, run_id: str, *, after_sequence: int = 0, limit: int = DEFAULT_EVENT_PAGE
    ) -> list[LeadDomainEvent]:
        """按 sequence 游标分页读取审计事件。"""
        ...

    def delete(self, run_id: str) -> None:
        """删除 Run，级联清理其事件（及租约/预算记录）。"""
        ...


class InMemoryLeadRunStore:
    """内存实现：与 SQLite 共享同一份契约测试。

    线程安全：三个映射的读写全部在同一把 RLock 内，
    保证 `save()` 的「校验 → 快照 → 递增 → 追加事件」不可分割。
    """

    def __init__(self) -> None:
        self._runs: dict[str, LeadRunState] = {}
        self._revisions: dict[str, int] = {}
        self._events: dict[str, list[LeadDomainEvent]] = {}
        self._lock = threading.RLock()
        self._closed = False

    def close(self) -> None:
        """标记关闭（幂等），语义与 SQLite 实现一致。"""
        with self._lock:
            self._closed = True
            self._runs.clear()
            self._revisions.clear()
            self._events.clear()

    def _ensure_open(self) -> None:
        if self._closed:
            raise LeadStoreClosedError()

    # --- CRUD ---

    def create(self, state: LeadRunState) -> None:
        if not isinstance(state, LeadRunState):
            raise TypeError(f"只接受 LeadRunState，收到 {type(state).__name__}")
        checked_thread_id(state)
        run_id = state.lead_run_id
        with self._lock:
            self._ensure_open()
            if run_id in self._runs:
                raise LeadRunAlreadyExistsError(run_id)
            # 初始事件：创建也进审计链。
            # sequence 从 **1** 开始——`after_sequence` 默认 0 表示「尚未读过」，
            # 若创建事件占 0 位，会被 `> after_sequence` 永久过滤掉，
            # 分页的第一屏就丢了 run.created。
            self._runs[run_id] = state
            self._revisions[run_id] = 0
            self._events[run_id] = [
                LeadDomainEvent.create(
                    run_id=run_id, sequence=1, event_type="run.created"
                )
            ]

    def get(self, run_id: str) -> LeadRunState:
        with self._lock:
            self._ensure_open()
            state = self._runs.get(run_id)
        if state is None:
            raise LeadRunNotFoundError(run_id)
        return state

    def revision(self, run_id: str) -> int:
        """当前 revision（测试与调用方首次 save 前需要基准值）。"""
        with self._lock:
            self._ensure_open()
            if run_id not in self._revisions:
                raise LeadRunNotFoundError(run_id)
            return self._revisions[run_id]

    def save(
        self,
        state: LeadRunState,
        *,
        expected_revision: int,
        event: LeadDomainEvent,
    ) -> int:
        """原子提交：校验 revision → 写快照 → 递增 → 追加事件。

        事件 payload 的校验发生在**写入之前**——不合格的事件会让整个
        save 失败，而不是先推进状态再发现审计记不下。

        Raises:
            LeadRunNotFoundError: run_id 不存在。
            LeadRunConflictError: revision 不匹配（并发写者必须重读重写）。
            EventRunMismatchError: 事件不属于自己这份状态。
            ValueError: 事件 payload 不合法。
        """
        if not isinstance(state, LeadRunState):
            raise TypeError(f"只接受 LeadRunState，收到 {type(state).__name__}")
        checked_thread_id(state)
        if not isinstance(event, LeadDomainEvent):
            raise TypeError(f"只接受 LeadDomainEvent，收到 {type(event).__name__}")
        if event.run_id != state.lead_run_id:
            # 领域异常（不是 ValueError）：下面四步一步都还没走，
            # 快照不更新、revision 不增、事件不写
            raise EventRunMismatchError(event.run_id, state.lead_run_id)
        # 先构造带正确 sequence 的事件：payload/类型校验在此触发
        with self._lock:
            self._ensure_open()
            run_id = state.lead_run_id
            if run_id not in self._runs:
                raise LeadRunNotFoundError(run_id)
            current = self._revisions[run_id]
            if current != expected_revision:
                raise LeadRunConflictError(
                    run_id, expected=expected_revision, actual=current
                )
            # 序号 = 现有条数 + 1：创建事件占 1，首条 save 落 2
            sequence = len(self._events[run_id]) + 1
            stamped = replace(event, sequence=sequence)

            # 四步在同一把锁内完成 —— 等价于一次事务
            self._runs[run_id] = state
            self._revisions[run_id] = current + 1
            self._events[run_id].append(stamped)
            return current + 1

    # --- 查询 ---

    def list_by_thread(
        self, thread_id: str, *, limit: int, offset: int
    ) -> list[LeadRunState]:
        checked_limit(limit, max_limit=MAX_LIST_LIMIT)
        checked_offset(offset)
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError(f"thread_id 必须是非空字符串，收到 {thread_id!r}")
        with self._lock:
            self._ensure_open()
            # 按 lead_run_id 排序保证分页顺序稳定（dict 插入序不够明确）
            matched = [
                state
                for state in sorted(self._runs.values(), key=lambda s: s.lead_run_id)
                if state.thread_id == thread_id
            ]
        return matched[offset : offset + limit]

    def list_recoverable(self, *, limit: int) -> list[LeadRunState]:
        checked_limit(limit, max_limit=MAX_LIST_LIMIT)
        with self._lock:
            self._ensure_open()
            # 只返回非终态：终态 Run 没有「恢复」的必要
            return [
                state
                for state in self._runs.values()
                if not is_terminal(state.status)
            ][:limit]

    def list_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = DEFAULT_EVENT_PAGE,
    ) -> list[LeadDomainEvent]:
        checked_limit(limit, max_limit=MAX_EVENT_PAGE_LIMIT)
        if isinstance(after_sequence, bool) or not isinstance(after_sequence, int):
            raise ValueError(f"after_sequence 必须是整数，收到 {after_sequence!r}")
        if after_sequence < 0:
            raise ValueError(f"after_sequence 不能为负，收到 {after_sequence}")
        with self._lock:
            self._ensure_open()
            if run_id not in self._runs:
                raise LeadRunNotFoundError(run_id)
            events = self._events[run_id]
            after = [
                event for event in events if event.sequence > after_sequence
            ]
        return after[:limit]

    def delete(self, run_id: str) -> None:
        """删除 Run 及其全部事件（级联清理）。不存在时静默返回（幂等）。"""
        with self._lock:
            self._ensure_open()
            self._runs.pop(run_id, None)
            self._revisions.pop(run_id, None)
            self._events.pop(run_id, None)
