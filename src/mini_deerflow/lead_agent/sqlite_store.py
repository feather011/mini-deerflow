"""Phase 25B SQLite 持久化：LeadRun 快照 + 审计事件 + 预算账本。

## 与现有 SQLiteThreadStore 的关系

沿用同一套习惯：`sqlite3.connect(path, check_same_thread=False)` +
`RLock` + `CREATE TABLE IF NOT EXISTS`（初始化幂等）+ `with conn:` 明确事务边界。
但因本模块需要**级联删除**与**并发写**，比 `SQLiteThreadStore` 多三项配置：

```python
PRAGMA foreign_keys = ON      # 删 Run 时级联清事件/租约/预算
PRAGMA busy_timeout = 5000    # 写锁竞争时等待而非立刻报 database is locked
PRAGMA journal_mode = WAL    # 读写并发（内存库不支持，故容错降级）
```

WAL 失败时静默回退到默认日志模式：它只是性能优化，
失败不影响正确性，不该因此拒绝启动。

## 为什么 state_json 之外还要 thread_id 列

`list_by_thread()` 要在 SQL 层过滤。把 `LeadRunState.to_dict()` 整个塞进
JSON 再用 `json_extract` 过滤既依赖 SQLite 的 JSON1 扩展，又让索引失效。
单列冗余存储换来的是可用的索引与跨实现一致的分页语义——`LeadRunState` 本身
始终是权威来源，该列只作查询加速，读取时**不**反向回填。

## 事务与并发

`save()` 的「校验 revision → 写快照 → 递增 → 追加事件」全部在一个
`with self._conn:` 块内：块内抛异常即整体 ROLLBACK，绝不会出现
「状态推进了但审计没记」或反之的半截结果。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Iterator

from .budget_ledger import BudgetLease, LeadBudgetLedger
from .errors import (
    EventRunMismatchError,
    LeadRunAlreadyExistsError,
    LeadRunConflictError,
    LeadRunNotFoundError,
    LeadStoreClosedError,
    LeadStoreDataCorruptedError,
    LeadStoreWriteError,
)
from .events import LeadDomainEvent
from .models import LeadRunState
from .state_machine import TERMINAL_LEAD_STATUSES
from .store import (
    DEFAULT_EVENT_PAGE,
    MAX_EVENT_PAGE_LIMIT,
    MAX_LIST_LIMIT,
    checked_limit,
    checked_offset,
    checked_thread_id,
)

__all__ = ["SQLiteLeadRunStore"]

# 终态状态值：用于 list_recoverable() 在 SQL 层排除终态 Run。
# 取自状态机常量而非本地硬编码，避免两处漂移。
_TERMINAL_RUN_VALUES: tuple[str, ...] = tuple(
    sorted(status.value for status in TERMINAL_LEAD_STATUSES)
)

# 写锁等待上限（毫秒）。超过则由调用方决定是否重试。
BUSY_TIMEOUT_MS = 5000

_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS lead_runs (
        lead_run_id  TEXT PRIMARY KEY,
        revision     INTEGER NOT NULL,
        thread_id    TEXT NOT NULL,
        status       TEXT NOT NULL,
        state_json   TEXT NOT NULL,
        CHECK (length(trim(thread_id)) > 0)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_lead_runs_thread ON lead_runs(thread_id)",
    "CREATE INDEX IF NOT EXISTS idx_lead_runs_status ON lead_runs(status)",
    """
    CREATE TABLE IF NOT EXISTS lead_events (
        event_id     TEXT PRIMARY KEY,
        run_id       TEXT NOT NULL,
        sequence     INTEGER NOT NULL,
        event_type   TEXT NOT NULL,
        timestamp    REAL NOT NULL,
        payload_json TEXT NOT NULL,
        UNIQUE (run_id, sequence),
        FOREIGN KEY (run_id) REFERENCES lead_runs(lead_run_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_lead_events_run ON lead_events(run_id, sequence)",
    """
    CREATE TABLE IF NOT EXISTS lead_budget_ledgers (
        run_id       TEXT PRIMARY KEY,
        ledger_json  TEXT NOT NULL,
        FOREIGN KEY (run_id) REFERENCES lead_runs(lead_run_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS lead_budget_leases (
        lease_id   TEXT PRIMARY KEY,
        run_id     TEXT NOT NULL,
        work_id    TEXT NOT NULL,
        reserved   INTEGER NOT NULL,
        consumed   INTEGER NOT NULL,
        returned   INTEGER NOT NULL,
        opened_at  REAL NOT NULL,
        closed_at  REAL,
        -- 资源级预留/消耗（JSON 对象）。租约的额度是按
        -- model_calls / tool_calls / steps / subagent_tasks 分账的，
        -- 只存总额会让「这次跑到底花在哪类调用上」查不出来。
        reserved_by_resource  TEXT NOT NULL DEFAULT '{}',
        consumed_by_resource  TEXT NOT NULL DEFAULT '{}',
        FOREIGN KEY (run_id) REFERENCES lead_runs(lead_run_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_lead_leases_run ON lead_budget_leases(run_id)",
)

# `_SCHEMA` 已含这些列（新库直接建好）；这里登记是为了给**先于本改动创建的库**补列。
_LATE_COLUMNS = (
    ("lead_budget_leases", "reserved_by_resource", "TEXT NOT NULL DEFAULT '{}'"),
    ("lead_budget_leases", "consumed_by_resource", "TEXT NOT NULL DEFAULT '{}'"),
)


class SQLiteLeadRunStore:
    """SQLite 实现：与 `InMemoryLeadRunStore` 行为契约完全一致。"""

    def __init__(self, database_path: str | Path) -> None:
        self._path = Path(database_path)
        self._lock = threading.RLock()
        self._closed = False
        # isolation_level=None = autocommit，**事务边界由本类显式管理**。
        # 这是 BEGIN IMMEDIATE 的前提：Python 的 `with conn:` 只在默认
        # deferred 模式下开事务，而 deferred 的写锁是在**第一次写**时才取，
        # 于是「读 revision → 算 sequence → 写」之间存在窗口，
        # 并发写会撞上 WAL 的 SQLITE_BUSY_SNAPSHOT，抛
        # `OperationalError: database is locked` 直接泄漏给调用方。
        self._conn = sqlite3.connect(self._path, check_same_thread=False,
                                     isolation_level=None)
        self._apply_pragmas()
        self._init_schema()

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    def _apply_pragmas(self) -> None:
        with self._lock:
            # 级联删除的前提；SQLite 默认关闭
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            try:
                # WAL 只对文件库有效，内存库会报错；属性能优化，失败即回退
                self._conn.execute("PRAGMA journal_mode = WAL")
            except sqlite3.DatabaseError:
                pass

    def _init_schema(self) -> None:
        with self._lock:
            for statement in _SCHEMA:
                self._conn.execute(statement)
            # `CREATE TABLE IF NOT EXISTS` 不会给**已存在**的库补列。
            # 资源级租约分账列是后加的，老库必须显式升级，
            # 否则 save_ledger 会开始报「no such column」。
            for table, column, decl in _LATE_COLUMNS:
                existing = {
                    row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")
                }
                if column not in existing:
                    self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {decl}"
                    )

    def close(self) -> None:
        """关闭连接（幂等）。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def __enter__(self) -> "SQLiteLeadRunStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise LeadStoreClosedError()

    @contextmanager
    def _write_transaction(self, *, run_id: str) -> Iterator[sqlite3.Connection]:
        """显式写事务：`BEGIN IMMEDIATE` 在事务开始时就取写锁。

        默认的 deferred 事务直到**第一次写**才取写锁，于是
        「读 revision → 算 sequence → 写」之间存在窗口：并发写会撞上
        WAL 的 `SQLITE_BUSY_SNAPSHOT`，直接抛
        `OperationalError: database is locked` 给调用方。

        `IMMEDIATE` 把竞争提前到事务开始，使失败变成「拿不到写锁」，
        这一类收敛为 `LeadRunConflictError`；其余 sqlite 错误走
        `_as_conflict` 分类，不会被谎报成可重试的冲突。

        `transaction_started` 是必须的：`BEGIN IMMEDIATE` 自己失败时事务
        **根本没开**，这时再 `ROLLBACK` 只会盖掉真正的错误信息
        （"cannot rollback - no transaction is active"）。
        """
        self._ensure_open()
        transaction_started = False
        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            # 未开事务 → 不 ROLLBACK，按是否并发分别归类
            raise self._as_conflict(exc, run_id, expected=-1) from exc
        transaction_started = True
        try:
            yield self._conn
        except BaseException:
            if transaction_started:
                # 失败事务必须整体回滚：不能残留半写的事件行
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise
        else:
            try:
                self._conn.execute("COMMIT")
            except sqlite3.Error:
                # 提交失败（磁盘满 / I-O）同样要收尾，且不能谎报成冲突
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    #: 只有这些 sqlite 消息代表「没抢到锁」= 并发竞争 = 可重读重试
    _CONCURRENCY_MARKERS = (
        "database is locked",
        "database table is locked",
        "database schema is locked",
        "database is busy",
        "database connection is busy",
    )
    #: 并发 sequence 竞争撞到的唯一约束（只此一条算冲突）
    _SEQUENCE_UNIQUE = (
        "unique constraint failed: lead_events.run_id, lead_events.sequence"
    )

    @classmethod
    def _as_conflict(
        cls, exc: sqlite3.Error, run_id: str, expected: int
    ) -> Exception:
        """按「能否重读重试」给底层 sqlite 错误分类，返回该抛的领域异常。

        - 并发竞争（拿不到写锁、sequence 撞 UNIQUE）→ `LeadRunConflictError`
        - 其余（表结构错 / CHECK 错 / I-O / 别的约束）→ `LeadStoreWriteError`

        把后者谎报成冲突是有害的：调用方会对着一个**必然失败**的操作
        无限重试，而真正的故障（磁盘坏了、表结构不对）被重试彻底掩盖。
        """
        if isinstance(exc, LeadRunConflictError):
            return exc
        message = str(exc).lower()
        if isinstance(exc, sqlite3.OperationalError) and any(
            marker in message for marker in cls._CONCURRENCY_MARKERS
        ):
            return LeadRunConflictError(run_id, expected=expected, actual=None)
        if isinstance(exc, sqlite3.IntegrityError) and (
            cls._SEQUENCE_UNIQUE in message
        ):
            return LeadRunConflictError(run_id, expected=expected, actual=None)
        return LeadStoreWriteError(
            run_id, f"{type(exc).__name__}: {exc}"
        )

    # ------------------------------------------------------------------
    # 反序列化
    # ------------------------------------------------------------------

    def _decode_state(self, run_id: str, raw: str) -> LeadRunState:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise LeadStoreDataCorruptedError(
                run_id, f"state_json 不是合法 JSON ({exc})"
            ) from exc
        try:
            return LeadRunState.from_dict(data)
        except ValueError as exc:
            # 含未知 schema_version / 非法字段：明确失败，不静默当作空状态
            raise LeadStoreDataCorruptedError(run_id, str(exc)) from exc

    @staticmethod
    def _decode_event(row: tuple) -> LeadDomainEvent:
        event_id, run_id, sequence, event_type, timestamp, payload_json = row
        try:
            payload = json.loads(payload_json)
        except (json.JSONDecodeError, ValueError) as exc:
            raise LeadStoreDataCorruptedError(
                str(run_id), f"事件 payload 不是合法 JSON ({exc})"
            ) from exc
        return LeadDomainEvent.from_dict(
            {
                "event_id": event_id,
                "run_id": run_id,
                "sequence": sequence,
                "event_type": event_type,
                "timestamp": timestamp,
                "payload": payload,
            }
        )

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(self, state: LeadRunState) -> None:
        """创建 Run 与首条 `run.created` 事件（同一写事务）。"""
        if not isinstance(state, LeadRunState):
            raise TypeError(f"只接受 LeadRunState，收到 {type(state).__name__}")
        checked_thread_id(state)
        run_id = state.lead_run_id
        created = LeadDomainEvent.create(
            run_id=run_id, sequence=1, event_type="run.created"
        )
        with self._lock:
            try:
                with self._write_transaction(run_id=run_id) as conn:
                    conn.execute(
                        "INSERT INTO lead_runs (lead_run_id, revision, thread_id,"
                        " status, state_json) VALUES (?, ?, ?, ?, ?)",
                        (
                            run_id,
                            0,
                            state.thread_id,
                            state.status.value,
                            json.dumps(state.to_dict(), ensure_ascii=False),
                        ),
                    )
                    self._insert_event(created)
            except sqlite3.IntegrityError as exc:
                # `checked_thread_id` 已在事务外挡掉空值，因此走到这里的
                # 只可能是约束冲突；但**只有主键唯一**才等于「run 已存在」，
                # 其余（CHECK、别的 UNIQUE）是写入失败，重试也没用
                if "unique constraint failed: lead_runs.lead_run_id" in str(
                    exc
                ).lower():
                    raise LeadRunAlreadyExistsError(run_id) from exc
                raise self._as_conflict(exc, run_id, expected=-1) from exc
            except sqlite3.OperationalError as exc:
                raise self._as_conflict(exc, run_id, expected=-1) from exc

    def get(self, run_id: str) -> LeadRunState:
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT state_json FROM lead_runs WHERE lead_run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise LeadRunNotFoundError(run_id)
        return self._decode_state(run_id, row[0])

    def revision(self, run_id: str) -> int:
        """当前 revision（首次 save 前的基准值）。"""
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT revision FROM lead_runs WHERE lead_run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise LeadRunNotFoundError(run_id)
        return int(row[0])

    def _insert_event(self, event: LeadDomainEvent) -> None:
        self._conn.execute(
            "INSERT INTO lead_events (event_id, run_id, sequence, event_type,"
            " timestamp, payload_json) VALUES (?, ?, ?, ?, ?, ?)",
            (
                event.event_id,
                event.run_id,
                event.sequence,
                event.event_type,
                event.timestamp,
                json.dumps(event.payload, ensure_ascii=False),
            ),
        )

    def save(
        self,
        state: LeadRunState,
        *,
        expected_revision: int,
        event: LeadDomainEvent,
    ) -> int:
        """CAS 写入 + 追加事件，在**同一事务**内完成。

        事务顺序：
        1. 读当前 revision 并比对 `expected_revision`
        2. 在事务内计算新 sequence（`MAX(sequence)+1`，天然唯一）
        3. 更新快照与 revision
        4. 追加事件

        任何一步失败 → `with self._conn` 块回滚，状态与事件都不变。
        """
        if not isinstance(state, LeadRunState):
            raise TypeError(f"只接受 LeadRunState，收到 {type(state).__name__}")
        checked_thread_id(state)
        if not isinstance(event, LeadDomainEvent):
            raise TypeError(f"只接受 LeadDomainEvent，收到 {type(event).__name__}")
        if event.run_id != state.lead_run_id:
            # 领域异常（不是 ValueError）：写事务之前就拦下，
            # 快照不更新、revision 不增、事件不写
            raise EventRunMismatchError(event.run_id, state.lead_run_id)
        run_id = state.lead_run_id

        with self._lock:
            try:
                with self._write_transaction(run_id=run_id) as conn:
                    row = conn.execute(
                        "SELECT revision FROM lead_runs WHERE lead_run_id = ?",
                        (run_id,),
                    ).fetchone()
                    if row is None:
                        raise LeadRunNotFoundError(run_id)
                    current = int(row[0])
                    if current != expected_revision:
                        raise LeadRunConflictError(
                            run_id, expected=expected_revision, actual=current
                        )
                    next_sequence = int(
                        conn.execute(
                            # 序号从 1 起（与 InMemory 一致）：创建事件占 1，
                            # 首条 save 落 2；`after_sequence=0` 才能读到全部
                            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM lead_events"
                            " WHERE run_id = ?",
                            (run_id,),
                        ).fetchone()[0]
                    )
                    stamped = replace(event, sequence=next_sequence)
                    self._insert_event(stamped)
                    cursor = conn.execute(
                        "UPDATE lead_runs SET revision = ?, thread_id = ?, status = ?,"
                        " state_json = ? WHERE lead_run_id = ? AND revision = ?",
                        (
                            current + 1,
                            state.thread_id,
                            state.status.value,
                            json.dumps(state.to_dict(), ensure_ascii=False),
                            run_id,
                            current,
                        ),
                    )
                    if cursor.rowcount != 1:
                        # 读与写之间被并发改过：回滚并让调用方重读重试
                        raise LeadRunConflictError(
                            run_id, expected=expected_revision, actual=None
                        )
                    return current + 1
            except sqlite3.IntegrityError as exc:
                # UNIQUE(run_id, sequence) 撞了：同样收敛成 CAS 冲突，
                # 不让 sqlite 的约束错误泄漏给调用方
                raise self._as_conflict(exc, run_id, expected_revision) from exc
            except sqlite3.OperationalError as exc:
                raise self._as_conflict(exc, run_id, expected_revision) from exc

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def list_by_thread(
        self, thread_id: str, *, limit: int, offset: int
    ) -> list[LeadRunState]:
        checked_limit(limit, max_limit=MAX_LIST_LIMIT)
        checked_offset(offset)
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError(f"thread_id 必须是非空字符串，收到 {thread_id!r}")
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                "SELECT lead_run_id, state_json FROM lead_runs WHERE thread_id = ?"
                " ORDER BY lead_run_id LIMIT ? OFFSET ?",
                (thread_id, limit, offset),
            ).fetchall()
        return [self._decode_state(row[0], row[1]) for row in rows]

    def list_recoverable(self, *, limit: int) -> list[LeadRunState]:
        """只返回**非终态** Run（重启后待恢复的那些）。

        在 SQL 层按 `status` 冗余列过滤：终态集合是常量，`NOT IN` 即可，
        不必把整库读出来再解码。`status` 列只是查询加速，
        权威值始终在 `LeadRunState` 里。
        """
        checked_limit(limit, max_limit=MAX_LIST_LIMIT)
        terminal_clause = ", ".join(f"'{value}'" for value in _TERMINAL_RUN_VALUES)
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                "SELECT lead_run_id, state_json FROM lead_runs"
                f" WHERE status NOT IN ({terminal_clause})"
                " ORDER BY lead_run_id LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._decode_state(run_id, raw) for run_id, raw in rows]

    def list_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = DEFAULT_EVENT_PAGE,
    ) -> list[LeadDomainEvent]:
        checked_limit(limit, max_limit=MAX_EVENT_PAGE_LIMIT)
        if isinstance(after_sequence, bool) or not isinstance(after_sequence, int):
            raise ValueError(
                f"after_sequence 必须是整数，收到 {after_sequence!r}"
            )
        if after_sequence < 0:
            raise ValueError(f"after_sequence 不能为负，收到 {after_sequence}")
        with self._lock:
            self._ensure_open()
            if not self._conn.execute(
                "SELECT 1 FROM lead_runs WHERE lead_run_id = ?", (run_id,)
            ).fetchone():
                raise LeadRunNotFoundError(run_id)
            rows = self._conn.execute(
                "SELECT event_id, run_id, sequence, event_type, timestamp,"
                " payload_json FROM lead_events WHERE run_id = ? AND sequence > ?"
                " ORDER BY sequence LIMIT ?",
                (run_id, after_sequence, limit),
            ).fetchall()
        return [self._decode_event(row) for row in rows]

    def delete(self, run_id: str) -> None:
        """删除 Run；事件、租约、预算记录由外键 ON DELETE CASCADE 清理。

        不存在时静默返回（幂等）。
        """
        with self._lock:
            try:
                with self._write_transaction(run_id=run_id) as conn:
                    conn.execute(
                        "DELETE FROM lead_runs WHERE lead_run_id = ?", (run_id,)
                    )
            except sqlite3.OperationalError as exc:
                raise self._as_conflict(exc, run_id, expected=-1) from exc

    # ------------------------------------------------------------------
    # 预算账本持久化（重启后 usage / reserved / lease 完全恢复）
    # ------------------------------------------------------------------

    def save_ledger(self, run_id: str, ledger: LeadBudgetLedger) -> None:
        """把根账本与全部租约原子落库。

        快照在**取本库锁之前**就算好：账本的持久化钩子是持着账本锁调进来的，
        若在锁内再调 `ledger.to_state()`，就会形成「库锁 → 账本锁」的反向锁序。
        """
        if not isinstance(ledger, LeadBudgetLedger):
            raise TypeError(
                f"只接受 LeadBudgetLedger，收到 {type(ledger).__name__}"
            )
        self.save_ledger_state(run_id, ledger.to_state())

    def save_ledger_state(self, run_id: str, state: dict) -> None:
        """把一份**账本状态快照**原子落库（账本持久化钩子的入口）。

        参数是快照而不是账本对象——钩子是在账本锁内被调用的，
        绝不能反过来去读账本（那是反向锁序）；同时这份快照本身就是
        「本次扣减成功后应该存在库里的样子」，写它不需要再问内存要值。
        """
        if not isinstance(state, dict):
            raise TypeError(f"账本状态必须是字典，收到 {type(state).__name__}")
        leases = state.get("leases")
        if not isinstance(leases, list):
            raise ValueError("leases 必须是数组")
        # 序列化与行构造都放在锁外：纯计算，不碰连接
        payload = json.dumps(state, ensure_ascii=False)
        rows = [
            (
                lease["lease_id"],
                run_id,
                lease["work_id"],
                lease["reserved"],
                lease["consumed"],
                lease["returned"],
                lease["opened_at"],
                lease["closed_at"],
                json.dumps(lease["reserved_by_resource"], ensure_ascii=False),
                json.dumps(lease["consumed_by_resource"], ensure_ascii=False),
            )
            for lease in leases
        ]
        with self._lock:
            try:
                with self._write_transaction(run_id=run_id) as conn:
                    conn.execute(
                        "INSERT INTO lead_budget_ledgers (run_id, ledger_json)"
                        " VALUES (?, ?) ON CONFLICT(run_id) DO UPDATE SET"
                        " ledger_json = excluded.ledger_json",
                        (run_id, payload),
                    )
                    # 租约整表重写：账本是这一维度的唯一权威来源，
                    # 局部 upsert 容易在「租约被删」时留下残行
                    conn.execute(
                        "DELETE FROM lead_budget_leases WHERE run_id = ?", (run_id,)
                    )
                    conn.executemany(
                        "INSERT INTO lead_budget_leases (lease_id, run_id, work_id,"
                        " reserved, consumed, returned, opened_at, closed_at,"
                        " reserved_by_resource, consumed_by_resource)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        rows,
                    )
            except sqlite3.OperationalError as exc:
                raise self._as_conflict(exc, run_id, expected=-1) from exc
            except sqlite3.IntegrityError as exc:
                raise self._as_conflict(exc, run_id, expected=-1) from exc

    def get_ledger(self, run_id: str) -> LeadBudgetLedger | None:
        """读回根账本；无记录返回 None。"""
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT ledger_json FROM lead_budget_ledgers WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row[0])
        except (json.JSONDecodeError, ValueError) as exc:
            raise LeadStoreDataCorruptedError(
                run_id, f"ledger_json 不是合法 JSON ({exc})"
            ) from exc
        return LeadBudgetLedger.from_state(data)

    def load_leases(self, run_id: str) -> tuple[BudgetLease, ...]:
        """从租约表读回全部租约（含已关闭），供恢复校验与审计。"""
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                "SELECT lease_id, run_id, work_id, reserved, consumed, returned,"
                " opened_at, closed_at, reserved_by_resource, consumed_by_resource"
                " FROM lead_budget_leases"
                " WHERE run_id = ? ORDER BY lease_id",
                (run_id,),
            ).fetchall()
        leases = []
        for row in rows:
            lease_id = str(row[0])
            try:
                leases.append(
                    BudgetLease.from_dict(
                        {
                            "lease_id": lease_id,
                            "run_id": row[1],
                            "work_id": row[2],
                            "reserved": row[3],
                            "consumed": row[4],
                            "returned": row[5],
                            "opened_at": row[6],
                            "closed_at": row[7],
                            "reserved_by_resource": self._resource_map(
                                run_id, lease_id, row[8]
                            ),
                            "consumed_by_resource": self._resource_map(
                                run_id, lease_id, row[9]
                            ),
                        }
                    )
                )
            except ValueError as exc:
                # 分账列内容非法（JSON 坏 / 资源名未登记 / 非整数）
                raise LeadStoreDataCorruptedError(
                    run_id, f"租约 {lease_id} 分账数据非法 ({exc})"
                ) from exc
        return tuple(leases)

    @staticmethod
    def _resource_map(run_id: str, lease_id: str, raw: object) -> dict[str, int]:
        """解析租约的资源分账 JSON；损坏即抛数据损坏错误，不静默降级成空表。"""
        try:
            data = json.loads(raw if isinstance(raw, str) else "")
        except (json.JSONDecodeError, ValueError) as exc:
            raise LeadStoreDataCorruptedError(
                run_id, f"租约 {lease_id} 分账列不是合法 JSON ({exc})"
            ) from exc
        if not isinstance(data, dict):
            raise LeadStoreDataCorruptedError(
                run_id, f"租约 {lease_id} 分账列必须是对象，实为 {type(data).__name__}"
            )
        try:
            return {str(key): int(value) for key, value in data.items()}
        except (TypeError, ValueError) as exc:
            raise LeadStoreDataCorruptedError(
                run_id, f"租约 {lease_id} 分账数值必须是整数 ({exc})"
            ) from exc

    def count_events(self, run_id: str) -> int:
        """事件总数（原子性测试用：验证失败时状态与事件都未推进）。"""
        with self._lock:
            self._ensure_open()
            return int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM lead_events WHERE run_id = ?", (run_id,)
                ).fetchone()[0]
            )
