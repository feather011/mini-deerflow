"""Phase 25B / LeadRunStore 契约测试。

**InMemory 与 SQLite 跑同一份用例**——两者行为必须完全一致，否则
「本地跑得通、换 SQLite 就坏」会成为最贵的 bug。

关键期望值**独立写死**在测试里（revision 从 0 起、事件 sequence 从 0 起、
终态集合是这 5 个……），不去 import 生产常量做对照，避免改实现时测试
跟着一起「通过」。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from mini_deerflow.lead_agent import (
    DEFAULT_EVENT_PAGE,
    MAX_LIST_LIMIT,
    EventRunMismatchError,
    EvidenceReceipt,
    GoalContract,
    InMemoryLeadRunStore,
    LeadDomainEvent,
    LeadRunAlreadyExistsError,
    LeadRunConflictError,
    LeadRunNotFoundError,
    LeadRunState,
    LeadRunStatus,
    LeadStoreClosedError,
    LeadStoreDataCorruptedError,
    LeadStoreWriteError,
    SQLiteLeadRunStore,
    VerifiedFact,
    WorkItem,
    WorkItemProposal,
)

# --- 独立写死的期望值（不 import 生产常量） ---
EXPECTED_TERMINAL_RUN_STATUSES = {"succeeded", "partial", "failed", "cancelled", "timed_out"}
EXPECTED_CREATED_EVENT_TYPE = "run.created"
T0 = 1_700_000_000.0


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path):
    """同一份契约跑两种实现。"""
    if request.param == "memory":
        handle = InMemoryLeadRunStore()
    else:
        handle = SQLiteLeadRunStore(tmp_path / "lead.db")
    yield handle
    handle.close()


def _goal(objective: str = "验证 LeadRun 持久化") -> GoalContract:
    return GoalContract.create(
        goal_id="goal-1",
        objective=objective,
        acceptance_criteria=("状态可恢复", "事件可审计"),
        now=T0,
    )


def _run(
    run_id: str = "run-1", *, thread_id: str = "thread-a"
) -> LeadRunState:
    """一个新创建的 Run（CREATED）。"""
    return LeadRunState.start(
        lead_run_id=run_id, goal=_goal(), thread_id=thread_id, now=T0
    )


def _state(
    run_id: str,
    status: LeadRunStatus,
    *,
    thread_id: str = "thread-a",
    **extra,
) -> LeadRunState:
    """构造指定状态的 Run（用于终态、可恢复态等场景）。"""
    return LeadRunState(
        lead_run_id=run_id,
        goal=_goal(),
        status=status,
        created_at=T0,
        thread_id=thread_id,
        updated_at=T0,
        **extra,
    )


def _event(
    run_id: str = "run-1",
    event_type: str = "run.state_changed",
    payload: dict | None = None,
):
    """构造事件。`payload` 必须是**显式字典参数**——若写成 `**payload`，
    调用方的 `payload={...}` 会被二次包装成 `{"payload": {...}}`，
    落库后读 `stored.payload["counts"]` 直接 KeyError。"""
    return LeadDomainEvent.create(
        run_id=run_id, sequence=0, event_type=event_type, payload=payload or {}
    )


# ---------------------------------------------------------------------------
# create / get
# ---------------------------------------------------------------------------


def test_create_then_get_roundtrips_state(store) -> None:
    state = _run()
    store.create(state)
    restored = store.get("run-1")
    assert restored.lead_run_id == "run-1"
    assert restored.status is LeadRunStatus.CREATED
    assert restored.thread_id == "thread-a"
    assert restored.to_dict() == state.to_dict()


def test_create_duplicate_run_id_rejected(store) -> None:
    store.create(_run())
    with pytest.raises(LeadRunAlreadyExistsError):
        store.create(_run())
    # 重复创建不能覆盖原有数据
    assert store.get("run-1").status is LeadRunStatus.CREATED


def test_get_unknown_run_raises_not_found(store) -> None:
    with pytest.raises(LeadRunNotFoundError):
        store.get("ghost")


def test_create_records_initial_event(store) -> None:
    """创建动作本身也进审计链。"""
    store.create(_run())
    events = store.list_events("run-1")
    assert len(events) == 1
    assert events[0].event_type == EXPECTED_CREATED_EVENT_TYPE
    assert events[0].sequence == 1


def test_rich_state_survives_roundtrip(store) -> None:
    """带 items / facts / decisions 的复杂状态往返不丢字段。"""
    from mini_deerflow.lead_agent import PlanningDecision

    fact = VerifiedFact(
        fact_id="f-1",
        statement="已验证事实",
        work_id="w-1",
        verification_id="v-1",
        receipts=(EvidenceReceipt(receipt_id="rc-1", source="test", observed_at=T0),),
        established_at=T0,
    )
    item = WorkItem.from_proposal(
        WorkItemProposal(
            work_id="w-1",
            objective="做一件事",
            created_at=T0,
        )
    )
    state = _state(
        "run-1",
        LeadRunStatus.VERIFYING,
        thread_id="thread-a",
        batch_index=2,
        decisions=(
            PlanningDecision(
                decision_id="d-1",
                batch_index=1,
                proposals=(
                    WorkItemProposal(work_id="w-1", objective="做一件事", created_at=T0),
                ),
                decided_at=T0,
            ),
        ),
        items=(item,),
        verified_facts=(fact,),
    )
    store.create(state)
    restored = store.get("run-1")
    assert restored == state
    assert restored.batch_index == 2
    assert len(restored.decisions) == 1
    assert len(restored.items) == 1
    assert len(restored.verified_facts) == 1


# ---------------------------------------------------------------------------
# CAS
# ---------------------------------------------------------------------------


def test_save_with_correct_revision_succeeds_and_increments(store) -> None:
    store.create(_run())
    assert store.revision("run-1") == 0
    state = _run()
    state.transition_to(LeadRunStatus.PLANNING, now=T0 + 1)

    new_revision = store.save(state, expected_revision=0, event=_event())
    assert new_revision == 1
    assert store.revision("run-1") == 1
    assert store.get("run-1").status is LeadRunStatus.PLANNING


def test_save_with_stale_revision_conflicts_and_keeps_state(store) -> None:
    store.create(_run())
    state = _run()
    state.transition_to(LeadRunStatus.PLANNING, now=T0 + 1)
    store.save(state, expected_revision=0, event=_event())

    # 第二个写者仍拿着 revision=0（状态路径必须合法：CREATED 只能去 PLANNING）
    other = _run()
    other.transition_to(LeadRunStatus.PLANNING, now=T0 + 2)
    with pytest.raises(LeadRunConflictError) as excinfo:
        store.save(other, expected_revision=0, event=_event())
    assert excinfo.value.expected == 0
    assert excinfo.value.actual == 1
    # 陈旧写入不得改变已提交状态
    assert store.get("run-1").status is LeadRunStatus.PLANNING
    assert store.revision("run-1") == 1


def test_save_unknown_run_raises_not_found(store) -> None:
    with pytest.raises(LeadRunNotFoundError):
        store.save(_run(), expected_revision=0, event=_event())


def test_concurrent_cas_exactly_one_wins(store) -> None:
    """两线程同时 CAS 同一 revision，只能有一个成功。"""
    store.create(_run())
    barrier = threading.Barrier(2)
    results: list[int] = []
    errors: list[BaseException] = []

    def writer(index: int) -> None:
        state = _run()
        state.transition_to(LeadRunStatus.PLANNING, now=T0 + index)
        try:
            barrier.wait(timeout=5)
            results.append(
                store.save(
                    state,
                    expected_revision=0,
                    event=_event(payload={"writer": index}),
                )
            )
        except BaseException as exc:  # noqa: BLE001 - 收集断言
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    # 恰好一个成功、一个冲突——冲突是**预期结果**，不是错误
    assert len(results) == 1, f"应当恰好一个成功，实际 {results}"
    assert results == [1]
    assert len(errors) == 1, f"应当恰好一个冲突，实际 {errors}"
    assert isinstance(errors[0], LeadRunConflictError)
    assert store.revision("run-1") == 1
    # 落库的事件 = 创建(1) + 成功那一条(2)
    assert len(store.list_events("run-1")) == 2
    assert [e.sequence for e in store.list_events("run-1")] == [1, 2]


# ===========================================================================
# SQLite 异常映射收窄（修复项 3）
# ===========================================================================


@pytest.mark.parametrize(
    ("exc", "expect_conflict"),
    [
        # 可重读重试的并发竞争 → CAS 冲突
        (sqlite3.OperationalError("database is locked"), True),
        (sqlite3.OperationalError("database table is locked"), True),
        (sqlite3.OperationalError("database is busy"), True),
        (
            sqlite3.IntegrityError(
                "UNIQUE constraint failed: lead_events.run_id, lead_events.sequence"
            ),
            True,
        ),
        # 重试也没用的：表结构错 / I-O / 其它约束 → 换一类领域异常
        (sqlite3.OperationalError("no such table: lead_events"), False),
        (sqlite3.OperationalError("disk I/O error"), False),
        (sqlite3.OperationalError("database disk image is malformed"), False),
        (sqlite3.OperationalError('near "MODIFY": syntax error'), False),
        (sqlite3.IntegrityError("CHECK constraint failed: length(trim(thread_id))"), False),
        (sqlite3.IntegrityError("UNIQUE constraint failed: lead_events.event_id"), False),
        (sqlite3.IntegrityError("FOREIGN KEY constraint failed"), False),
    ],
)
def test_sqlite_error_mapping_is_narrow(
    exc: sqlite3.Error, expect_conflict: bool
) -> None:
    """只有真能靠重读重试解决的错误才是 CAS 冲突。

    谎报成冲突是有害的：调用方会对着磁盘坏了的操作无限重试，
    真正的故障被重试彻底掩盖。
    """
    mapped = SQLiteLeadRunStore._as_conflict(exc, "run-1", expected=3)
    if expect_conflict:
        assert isinstance(mapped, LeadRunConflictError)
        assert mapped.run_id == "run-1"
        assert mapped.expected == 3
    else:
        assert isinstance(mapped, LeadStoreWriteError)
        assert not isinstance(mapped, LeadRunConflictError)


def test_dropped_table_is_not_reported_as_cas_conflict(tmp_path: Path) -> None:
    """集成路径：表被改坏 → LeadStoreWriteError，且事务已回滚。"""
    handle = SQLiteLeadRunStore(tmp_path / "broken.db")
    try:
        handle.create(_run("run-b", thread_id="t"))
        handle._conn.execute("DROP TABLE lead_events")

        with pytest.raises(LeadStoreWriteError) as excinfo:
            handle.save(
                _run("run-b", thread_id="t"),
                expected_revision=0,
                event=_event(run_id="run-b"),
            )
        assert not isinstance(excinfo.value, LeadRunConflictError)
        # 快照也没被推进（事务整体回滚）
        assert handle.revision("run-b") == 0
    finally:
        handle.close()


def test_event_run_id_must_match_state(store) -> None:
    store.create(_run())
    before_revision = store.revision("run-1")
    before_events = store.list_events("run-1")

    # 领域异常，不是 ValueError：调用方该换事件重来，而不是修输入
    with pytest.raises(EventRunMismatchError, match="不一致"):
        store.save(_run(), expected_revision=0, event=_event(run_id="other-run"))

    # 整笔回滚：快照不更新、revision 不增、事件不写
    assert store.revision("run-1") == before_revision
    assert store.get("run-1").status is LeadRunStatus.CREATED
    assert store.list_events("run-1") == before_events


# ---------------------------------------------------------------------------
# 快照与事件原子性
# ---------------------------------------------------------------------------


def test_invalid_event_payload_rolls_back_whole_save(store) -> None:
    """事件不合格 → 状态与事件都不推进（同一事务）。"""
    store.create(_run())
    before_events = store.list_events("run-1")
    before_revision = store.revision("run-1")

    state = _run()
    state.transition_to(LeadRunStatus.PLANNING, now=T0 + 1)
    # 凭据类键在事件构造期就被拒
    with pytest.raises(ValueError, match="不得包含键"):
        _event(payload={"api_key": "sk-leak-should-never-persist"})

    assert store.revision("run-1") == before_revision
    assert store.get("run-1").status is LeadRunStatus.CREATED
    assert store.list_events("run-1") == before_events


def test_save_rejects_non_state_types(store) -> None:
    store.create(_run())
    with pytest.raises(TypeError):
        store.save("not-a-state", expected_revision=0, event=_event())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        store.save(_run(), expected_revision=0, event="not-an-event")  # type: ignore[arg-type]


def test_create_rejects_non_state() -> None:
    with pytest.raises(TypeError):
        InMemoryLeadRunStore().create("nope")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 事件
# ---------------------------------------------------------------------------


def test_event_sequence_is_monotonic_and_unique(store) -> None:
    """创建事件占 sequence=1，之后每次 save 递增 1，且 event_id 互不重复。

    状态只转换一次：重复 `transition_to(PLANNING)` 会撞状态机
    （非法转换），这里验的是事件序号而非状态机。
    """
    store.create(_run())
    state = store.get("run-1")
    state.transition_to(LeadRunStatus.PLANNING, now=T0)
    revision = 0
    for index in range(5):
        revision = store.save(
            store.get("run-1"),
            expected_revision=revision,
            event=_event(payload={"index": index}),
        )
    events = store.list_events("run-1")
    assert [e.sequence for e in events] == [1, 2, 3, 4, 5, 6]
    assert len({e.event_id for e in events}) == len(events)


def test_event_sequence_ignores_caller_value(store) -> None:
    """调用方传的 sequence 会被 Store 覆盖——序号只能由它分配。"""
    store.create(_run())
    state = store.get("run-1")
    state.transition_to(LeadRunStatus.PLANNING, now=T0)
    store.save(state, expected_revision=0, event=_event())
    events = store.list_events("run-1")
    # create 占 1，首条 save 落 2（不是调用方写的 0）
    assert [e.sequence for e in events] == [1, 2]


def test_event_cursor_pagination(store) -> None:
    store.create(_run())
    revision = 0
    for index in range(4):
        revision = store.save(
            store.get("run-1"),
            expected_revision=revision,
            event=_event(payload={"i": index}),
        )
    # after_sequence=0 必须能读到 run.created——分页首屏不能丢第一条
    first_page = store.list_events("run-1", after_sequence=0, limit=2)
    assert [e.sequence for e in first_page] == [1, 2]
    assert first_page[0].event_type == "run.created"
    second_page = store.list_events("run-1", after_sequence=2, limit=2)
    assert [e.sequence for e in second_page] == [3, 4]
    # 游标越过后返回空
    assert store.list_events("run-1", after_sequence=99) == []


def test_event_default_page_size(store) -> None:
    store.create(_run())
    assert DEFAULT_EVENT_PAGE == 100
    assert store.list_events("run-1") == store.list_events("run-1", limit=100)


@pytest.mark.parametrize("bad_limit", [0, -1, 501, "10", True])
def test_event_limit_bounds(store, bad_limit) -> None:
    store.create(_run())
    with pytest.raises(ValueError, match="limit"):
        store.list_events("run-1", limit=bad_limit)


@pytest.mark.parametrize("bad_cursor", [-1, "0", True])
def test_event_cursor_bounds(store, bad_cursor) -> None:
    store.create(_run())
    with pytest.raises(ValueError, match="after_sequence"):
        store.list_events("run-1", after_sequence=bad_cursor)


def test_list_events_unknown_run_raises(store) -> None:
    with pytest.raises(LeadRunNotFoundError):
        store.list_events("ghost")


# ---------------------------------------------------------------------------
# 列表查询
# ---------------------------------------------------------------------------


def test_list_by_thread_filters_and_paginates(store) -> None:
    for index in range(3):
        store.create(_run(f"run-{index}", thread_id="thread-a"))
    store.create(_run("run-other", thread_id="thread-b"))

    first = store.list_by_thread("thread-a", limit=2, offset=0)
    assert [s.lead_run_id for s in first] == ["run-0", "run-1"]
    second = store.list_by_thread("thread-a", limit=2, offset=2)
    assert [s.lead_run_id for s in second] == ["run-2"]
    assert store.list_by_thread("thread-b", limit=5, offset=0) != []
    assert store.list_by_thread("thread-z", limit=5, offset=0) == []


def test_list_by_thread_stable_order_across_pages(store) -> None:
    for index in range(5):
        store.create(_run(f"run-{index}", thread_id="t"))
    collected = []
    for offset in range(0, 5, 2):
        collected.extend(
            s.lead_run_id for s in store.list_by_thread("t", limit=2, offset=offset)
        )
    assert collected == sorted(collected)
    assert len(collected) == 5


@pytest.mark.parametrize("bad_limit", [0, -1, MAX_LIST_LIMIT + 1, "5", True])
def test_list_by_thread_limit_bounds(store, bad_limit) -> None:
    with pytest.raises(ValueError, match="limit"):
        store.list_by_thread("t", limit=bad_limit, offset=0)


@pytest.mark.parametrize("bad_offset", [-1, "0", True])
def test_list_by_thread_offset_bounds(store, bad_offset) -> None:
    with pytest.raises(ValueError, match="offset"):
        store.list_by_thread("t", limit=5, offset=bad_offset)


def test_list_by_thread_rejects_empty_thread_id(store) -> None:
    with pytest.raises(ValueError, match="thread_id"):
        store.list_by_thread("", limit=5, offset=0)


def test_list_recoverable_excludes_terminal_runs(store) -> None:
    """只有非终态 Run 需要恢复。"""
    store.create(_state("run-active-1", LeadRunStatus.EXECUTING, thread_id="t"))
    store.create(_run("run-created", thread_id="t"))  # CREATED 也是非终态
    for index, terminal in enumerate(sorted(EXPECTED_TERMINAL_RUN_STATUSES)):
        store.create(
            _state(
                f"run-done-{index}",
                LeadRunStatus(terminal),
                thread_id="t",
            )
        )

    found = {s.lead_run_id for s in store.list_recoverable(limit=50)}
    assert found == {"run-active-1", "run-created"}
    # 五个终态都被排除了
    assert len(EXPECTED_TERMINAL_RUN_STATUSES) == 5


@pytest.mark.parametrize("bad_limit", [0, -1, MAX_LIST_LIMIT + 1, True])
def test_list_recoverable_limit_bounds(store, bad_limit) -> None:
    with pytest.raises(ValueError, match="limit"):
        store.list_recoverable(limit=bad_limit)


def test_list_recoverable_respects_limit(store) -> None:
    for index in range(4):
        store.create(_run(f"run-{index}", thread_id="t"))
    assert len(store.list_recoverable(limit=2)) == 2


# ---------------------------------------------------------------------------
# 删除与关闭
# ---------------------------------------------------------------------------


def test_delete_removes_state_and_events(store) -> None:
    store.create(_run())
    state = store.get("run-1")
    state.transition_to(LeadRunStatus.PLANNING, now=T0)
    store.save(state, expected_revision=0, event=_event())
    assert store.list_events("run-1")

    store.delete("run-1")

    with pytest.raises(LeadRunNotFoundError):
        store.get("run-1")
    with pytest.raises(LeadRunNotFoundError):
        store.list_events("run-1")


def test_delete_is_idempotent(store) -> None:
    store.create(_run())
    store.delete("run-1")
    store.delete("run-1")  # 不抛
    with pytest.raises(LeadRunNotFoundError):
        store.get("run-1")


def test_closed_store_rejects_operations() -> None:
    handle = InMemoryLeadRunStore()
    handle.create(_run())
    handle.close()
    handle.close()  # 幂等
    for call in (
        lambda: handle.get("run-1"),
        lambda: handle.create(_run()),
        lambda: handle.list_recoverable(limit=5),
        lambda: handle.delete("run-1"),
    ):
        with pytest.raises(LeadStoreClosedError):
            call()


# ===========================================================================
# SQLite 专属：重启恢复、PRAGMA、级联、损坏（InMemory 无此行为）
# ===========================================================================


def test_sqlite_state_survives_restart(tmp_path: Path) -> None:
    """关掉再打开，快照、事件与 revision 全部恢复。"""
    db = tmp_path / "restart.db"
    first = SQLiteLeadRunStore(db)
    first.create(_run("run-restart", thread_id="t"))
    state = first.get("run-restart")
    state.transition_to(LeadRunStatus.PLANNING, now=T0 + 1)
    first.save(
        state, expected_revision=0, event=_event(run_id="run-restart")
    )
    before_events = first.list_events("run-restart")
    first.close()

    reopened = SQLiteLeadRunStore(db)
    assert reopened.get("run-restart").status is LeadRunStatus.PLANNING
    assert reopened.revision("run-restart") == 1
    after_events = reopened.list_events("run-restart")
    assert [e.sequence for e in after_events] == [e.sequence for e in before_events]
    assert after_events[-1].payload == before_events[-1].payload
    reopened.close()


def test_sqlite_schema_initialisation_is_idempotent(tmp_path: Path) -> None:
    """同一文件反复打开不报错，也不丢已有数据。"""
    db = tmp_path / "idem.db"
    first = SQLiteLeadRunStore(db)
    first.create(_run("run-idem", thread_id="t"))
    first.close()

    for _ in range(3):
        handle = SQLiteLeadRunStore(db)
        assert handle.get("run-idem").lead_run_id == "run-idem"
        handle.close()


def test_sqlite_enables_foreign_keys_for_cascade(tmp_path: Path) -> None:
    """foreign_keys 必须真的开着，否则「级联删除」只是文档承诺。"""
    handle = SQLiteLeadRunStore(tmp_path / "fk.db")
    enabled = handle._conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert enabled == 1
    handle.close()


def test_sqlite_delete_cascades_to_events(tmp_path: Path) -> None:
    handle = SQLiteLeadRunStore(tmp_path / "cascade.db")
    handle.create(_run("run-c", thread_id="t"))
    state = handle.get("run-c")
    state.transition_to(LeadRunStatus.PLANNING, now=T0)
    handle.save(state, expected_revision=0, event=_event(run_id="run-c"))
    assert handle.count_events("run-c") == 2

    handle.delete("run-c")

    with pytest.raises(LeadRunNotFoundError):
        handle.get("run-c")
    with pytest.raises(LeadRunNotFoundError):
        handle.list_events("run-c")
    # 事件行也被外键清掉了
    remaining = handle._conn.execute(
        "SELECT COUNT(*) FROM lead_events WHERE run_id = ?", ("run-c",)
    ).fetchone()[0]
    assert remaining == 0
    handle.close()


def test_sqlite_rejects_corrupted_state_json(tmp_path: Path) -> None:
    """损坏数据必须明确失败，不能当作空状态继续跑。"""
    db = tmp_path / "corrupt.db"
    handle = SQLiteLeadRunStore(db)
    handle.create(_run("run-x", thread_id="t"))
    handle.close()

    raw = sqlite3.connect(db)
    raw.execute(
        "UPDATE lead_runs SET state_json = 'not-json-at-all' WHERE lead_run_id = ?",
        ("run-x",),
    )
    raw.commit()
    raw.close()

    reopened = SQLiteLeadRunStore(db)
    with pytest.raises(LeadStoreDataCorruptedError) as excinfo:
        reopened.get("run-x")
    assert "JSON" in str(excinfo.value)
    reopened.close()


def test_sqlite_rejects_unknown_schema_version(tmp_path: Path) -> None:
    """未知 schema_version 明确失败，不静默当作当前版本读取。"""
    db = tmp_path / "version.db"
    handle = SQLiteLeadRunStore(db)
    handle.create(_run("run-v", thread_id="t"))
    handle.close()

    raw = sqlite3.connect(db)
    payload = json.loads(
        raw.execute(
            "SELECT state_json FROM lead_runs WHERE lead_run_id = ?", ("run-v",)
        ).fetchone()[0]
    )
    payload["schema_version"] = 999
    raw.execute(
        "UPDATE lead_runs SET state_json = ? WHERE lead_run_id = ?",
        (json.dumps(payload), "run-v"),
    )
    raw.commit()
    raw.close()

    reopened = SQLiteLeadRunStore(db)
    with pytest.raises(LeadStoreDataCorruptedError, match="schema_version"):
        reopened.get("run-v")
    reopened.close()


def test_sqlite_closed_store_rejects_operations(tmp_path: Path) -> None:
    handle = SQLiteLeadRunStore(tmp_path / "closed.db")
    handle.create(_run("run-c", thread_id="t"))
    handle.close()
    handle.close()  # 幂等
    with pytest.raises(LeadStoreClosedError):
        handle.get("run-c")
    with pytest.raises(LeadStoreClosedError):
        handle.save(
            _run("run-c", thread_id="t"),
            expected_revision=0,
            event=_event(run_id="run-c"),
        )


def test_sqlite_status_column_tracks_state(tmp_path: Path) -> None:
    """冗余 `status` 列必须随快照同步更新。

    `list_recoverable()` 直接在 SQL 里 `NOT IN` 终态，依赖这一列；
    一旦它与 `LeadRunState.status` 漂移，恢复列表就会漏掉或误收 Run。
    """
    db = tmp_path / "status.db"
    handle = SQLiteLeadRunStore(db)
    handle.create(_run("run-s", thread_id="t"))
    assert handle.list_recoverable(limit=5) != []      # CREATED 非终态

    state = handle.get("run-s")
    state.transition_to(LeadRunStatus.PLANNING, now=T0)
    handle.save(
        state, expected_revision=0, event=_event(run_id="run-s")
    )
    rows = handle._conn.execute(
        "SELECT status FROM lead_runs WHERE lead_run_id = ?", ("run-s",)
    ).fetchall()
    assert rows[0][0] == "planning"
    assert rows[0][0] == handle.get("run-s").status.value   # 与状态对象一致
    assert handle.list_recoverable(limit=5) != []           # PLANNING 仍非终态

    # 走到终态：列与过滤必须同步
    state = handle.get("run-s")
    state.transition_to(LeadRunStatus.ROUTING, now=T0)
    handle.save(
        state, expected_revision=1, event=_event(run_id="run-s")
    )
    state = handle.get("run-s")
    state.transition_to(LeadRunStatus.EXECUTING, now=T0)
    handle.save(
        state, expected_revision=2, event=_event(run_id="run-s")
    )
    state = handle.get("run-s")
    state.transition_to(LeadRunStatus.VERIFYING, now=T0)
    handle.save(
        state, expected_revision=3, event=_event(run_id="run-s")
    )
    state = handle.get("run-s")
    state.transition_to(LeadRunStatus.SYNTHESIZING, now=T0)
    handle.save(
        state, expected_revision=4, event=_event(run_id="run-s")
    )
    state = handle.get("run-s")
    state.transition_to(LeadRunStatus.SUCCEEDED, now=T0)
    handle.save(
        state, expected_revision=5, event=_event(run_id="run-s")
    )

    rows = handle._conn.execute(
        "SELECT status FROM lead_runs WHERE lead_run_id = ?", ("run-s",)
    ).fetchall()
    assert rows[0][0] == "succeeded"
    assert handle.list_recoverable(limit=5) == []   # 终态被 SQL 层排除
    handle.close()


def test_sqlite_uses_file_database(tmp_path: Path) -> None:
    """明确用文件库：`:memory:` 的多连接共享语义不可依赖。"""
    db = tmp_path / "file.db"
    assert db.exists() is False
    handle = SQLiteLeadRunStore(db)
    assert db.exists() is True
    handle.create(_run("run-f", thread_id="t"))
    handle.close()
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM lead_runs").fetchone()[0] == 1


def test_sqlite_concurrent_reads_and_writes(tmp_path: Path) -> None:
    """多线程混合读写不抛异常、不产生损坏数据。"""
    handle = SQLiteLeadRunStore(tmp_path / "mt.db")
    handle.create(_run("run-mt", thread_id="t"))
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            for _ in range(5):
                handle.get("run-mt")
                handle.list_recoverable(limit=5)
                handle.list_events("run-mt", limit=5)
        except BaseException as exc:  # noqa: BLE001 - 收集断言
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not errors, f"并发读写出错: {errors}"
    assert handle.get("run-mt").status is LeadRunStatus.CREATED
    handle.close()


# ===========================================================================
# 跨连接 CAS（修复项 2）
# ===========================================================================


def test_cross_connection_cas_exactly_one_wins(tmp_path: Path) -> None:
    """两个**独立连接**用相同 expected_revision 并发保存，严格一个成功。

    这里刻意用两个 `SQLiteLeadRunStore` 实例：它们各有自己的 `RLock`，
    互不串行——如果只用同一个实例，测到的只是进程内锁，跨连接的真实竞争
    （写锁、WAL 的 BUSY_SNAPSHOT）根本不会发生。
    """
    db = tmp_path / "xconn.db"
    writer_a = SQLiteLeadRunStore(db)
    writer_b = SQLiteLeadRunStore(db)
    try:
        writer_a.create(_run("run-x", thread_id="t"))
        # 另一连接必须能立刻看到刚创建的数据（证明确实是两个连接）
        assert writer_b.get("run-x").lead_run_id == "run-x"

        successes: list[int] = []
        failures: list[BaseException] = []
        barrier = threading.Barrier(2)

        def writer(store, index: int) -> None:
            state = store.get("run-x")
            state.transition_to(LeadRunStatus.PLANNING, now=T0 + index)
            try:
                barrier.wait(timeout=10)
                successes.append(
                    store.save(
                        state,
                        expected_revision=0,
                        event=_event(run_id="run-x", payload={"writer": index}),
                    )
                )
            except BaseException as exc:  # noqa: BLE001 - 收集断言
                failures.append(exc)

        threads = [
            threading.Thread(target=writer, args=(writer_a, 0)),
            threading.Thread(target=writer, args=(writer_b, 1)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert len(successes) == 1, f"应当恰好一个成功，实际 {successes}"
        assert len(failures) == 1
        # 失败必须被统一映射成领域冲突异常，
        # 不能把 sqlite 的 IntegrityError / OperationalError / "database is locked" 泄漏出去
        assert isinstance(failures[0], LeadRunConflictError), (
            f"泄漏了底层异常: {type(failures[0]).__name__}: {failures[0]}"
        )
        assert not isinstance(
            failures[0], (sqlite3.IntegrityError, sqlite3.OperationalError)
        )
    finally:
        writer_a.close()
        writer_b.close()


def test_cross_connection_cas_leaves_no_partial_event(tmp_path: Path) -> None:
    """失败事务不得残留事件行——否则审计链会出现「有事件无状态推进」的碎片。"""
    db = tmp_path / "xpartial.db"
    writer_a = SQLiteLeadRunStore(db)
    writer_b = SQLiteLeadRunStore(db)
    try:
        writer_a.create(_run("run-p", thread_id="t"))
        barrier = threading.Barrier(2)
        results: list[str] = []

        def writer(store, index: int) -> None:
            state = store.get("run-p")
            state.transition_to(LeadRunStatus.PLANNING, now=T0)
            try:
                barrier.wait(timeout=10)
                store.save(
                    state,
                    expected_revision=0,
                    event=_event(run_id="run-p", payload={"w": index}),
                )
                results.append("ok")
            except LeadRunConflictError:
                results.append("conflict")
            except BaseException:
                results.append("other")

        threads = [
            threading.Thread(target=writer, args=(writer_a, 0)),
            threading.Thread(target=writer, args=(writer_b, 1)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert sorted(results) == ["conflict", "ok"]
        assert writer_a.revision("run-p") == 1
        events = writer_a.list_events("run-p")
        # created + 恰好一条成功写入；失败方不留痕
        assert len(events) == 2
        sequences = [event.sequence for event in events]
        assert sequences == sorted(sequences)
        assert len(sequences) == len(set(sequences)), "sequence 不得重复"
    finally:
        writer_a.close()
        writer_b.close()


def test_closed_connection_rejects_further_operations(tmp_path: Path) -> None:
    """关闭的是**这个连接**：该连接拒绝操作，新连接仍可正常读写同一文件。"""
    db = tmp_path / "closed2.db"
    first = SQLiteLeadRunStore(db)
    first.create(_run("run-c2", thread_id="t"))
    first.close()
    first.close()  # close 幂等

    with pytest.raises(LeadStoreClosedError):
        first.get("run-c2")
    with pytest.raises(LeadStoreClosedError):
        first.save(
            _run("run-c2", thread_id="t"),
            expected_revision=0,
            event=_event(run_id="run-c2"),
        )
    with pytest.raises(LeadStoreClosedError):
        first.list_events("run-c2")

    # 文件本身没坏：新连接读得到
    second = SQLiteLeadRunStore(db)
    assert second.get("run-c2").lead_run_id == "run-c2"
    second.close()


# ===========================================================================
# thread_id 必填（修复项 3）
# ===========================================================================


def test_thread_id_is_required_on_construction() -> None:
    with pytest.raises(TypeError):
        LeadRunState.start(lead_run_id="r", goal=_goal(), now=T0)  # 缺必填参数


@pytest.mark.parametrize("blank", ["", "   "])
def test_thread_id_rejects_blank(blank: str) -> None:
    with pytest.raises(ValueError, match="thread_id"):
        LeadRunState(
            lead_run_id="r",
            goal=_goal(),
            status=LeadRunStatus.CREATED,
            thread_id=blank,
            created_at=T0,
        )


def test_from_dict_requires_non_empty_thread_id() -> None:
    payload = _run("r", thread_id="t").to_dict()
    assert payload["thread_id"] == "t"

    missing = dict(payload)
    del missing["thread_id"]
    with pytest.raises(ValueError):
        LeadRunState.from_dict(missing)

    blank = dict(payload, thread_id="  ")
    with pytest.raises(ValueError, match="thread_id"):
        LeadRunState.from_dict(blank)


def test_store_rejects_blank_thread_id_defensively(tmp_path: Path) -> None:
    """Store 是第二道闸：即使绕过模型构造，空 thread_id 也不许落库。"""
    state = _run("run-x", thread_id="t")
    object.__setattr__(state, "thread_id", "  ")   # 模拟被绕过模型校验

    with pytest.raises(ValueError, match="thread_id"):
        InMemoryLeadRunStore().create(state)

    sqlite_store = SQLiteLeadRunStore(tmp_path / "blank.db")
    try:
        with pytest.raises(ValueError, match="thread_id"):
            sqlite_store.create(state)
    finally:
        sqlite_store.close()


def test_sqlite_rejects_blank_thread_id_at_schema_level(tmp_path: Path) -> None:
    """数据库层也有 CHECK：空 thread_id 写不进去。"""
    handle = SQLiteLeadRunStore(tmp_path / "check.db")
    # 绕过 Store 直接写，CHECK 必须拦下
    with pytest.raises(sqlite3.IntegrityError):
        handle._conn.execute(
            "INSERT INTO lead_runs (lead_run_id, revision, thread_id, status,"
            " state_json) VALUES ('bad', 0, '   ', 'created', '{}')"
        )
    handle.close()


# ===========================================================================
# 事件 payload 递归检查（修复项 4）
# ===========================================================================


def test_payload_rejects_nested_credential_keys() -> None:
    """敏感键藏在嵌套结构里也必须拦下——只查顶层等于留了一扇门。"""
    store = InMemoryLeadRunStore()
    store.create(_run("run-e", thread_id="t"))
    state = store.get("run-e")

    for nested in (
        {"context": {"api_key": "sk-x"}},
        {"items": [{"secret": "x"}]},
        {"a": {"b": {"chain_of_thought": "..."}}},
        {"list": [[{"token": "t"}]]},
    ):
        with pytest.raises(ValueError):
            _event(run_id="run-e", payload=nested)


@pytest.mark.parametrize(
    "spelling",
    ["api_key", "API-KEY", "ApiKey", "APIKEY", "api-key", " api_key "],
)
def test_payload_rejects_key_spelling_variants(spelling: str) -> None:
    """大小写 / 连字符 / 下划线 / 空白都指同一个键，必须一视同仁。

    只做 `lower()` 会让 `API-KEY` 与 `ApiKey` 从缝里过去，
    而 `ApiKey` 恰恰是最常见的写法。
    """
    with pytest.raises(ValueError, match="不得包含键"):
        _event(run_id="run-e", payload={spelling: "sk-x"})


@pytest.mark.parametrize(
    "spelling", ["chain_of_thought", "Chain-Of-Thought", "COT", "system-Prompt"]
)
def test_payload_rejects_reasoning_key_variants(spelling: str) -> None:
    """推理过程 / 模型输入键同样按规范化后比对。"""
    with pytest.raises(ValueError, match="不得包含键"):
        _event(run_id="run-e", payload={"outer": {spelling: "..."}})


def test_payload_allows_keys_that_merely_contain_a_forbidden_word() -> None:
    """只比对**整个键名**，不做子串匹配——别把过滤做过头。"""
    _event(
        run_id="run-e",
        payload={"tokenizer_version": "v1", "tokens_used": 3, "prompt_len": 12},
    )


def test_payload_key_filter_is_not_a_full_secret_scanner() -> None:
    """键名过滤拦不住写在普通值里的密钥——这条测试钉住这个已知边界。

    规格明确要求：文档不得声称能检测普通字符串中所有秘密。
    因此这里断言的是**它确实拦不住**，而不是它能拦住。
    """
    with pytest.raises(ValueError, match="不得包含键"):
        _event(run_id="run-e", payload={"api_key": "sk-..."})
    # 但放在普通文本里就放行了 —— 调用方必须只提交结构化元数据
    event = _event(run_id="run-e", payload={"note": "key is sk-ABCDEF"})
    assert event.payload["note"].startswith("key is")


def test_payload_allows_safe_nested_metadata() -> None:
    """正常的多层结构化元数据必须放行，别把过滤做过头。"""
    store = InMemoryLeadRunStore()
    store.create(_run("run-e2", thread_id="t"))
    state = store.get("run-e2")
    state.transition_to(LeadRunStatus.PLANNING, now=T0)
    store.save(
        state,
        expected_revision=0,
        event=_event(
            run_id="run-e2",
            payload={"counts": {"model": 2, "tool": 1}, "tags": ["a", "b"]},
        ),
    )
    stored = store.list_events("run-e2")[-1]
    assert stored.payload["counts"]["model"] == 2
    assert stored.payload["tags"] == ["a", "b"]


def test_payload_rejects_non_serialisable_value() -> None:
    store = InMemoryLeadRunStore()
    store.create(_run("run-e3", thread_id="t"))
    with pytest.raises(ValueError):
        _event(run_id="run-e3", payload={"fn": lambda: None})


def test_event_run_id_mismatch_rolls_back_everything(store) -> None:
    """事件与状态不属于同一个 run：整笔回滚，审计链不串。"""
    store.create(_run("run-m", thread_id="t"))
    before_revision = store.revision("run-m")
    before_events = store.list_events("run-m")
    before_state = store.get("run-m")

    with pytest.raises(EventRunMismatchError, match="不一致"):
        store.save(
            store.get("run-m"),
            expected_revision=0,
            event=_event(run_id="someone-else"),
        )

    assert store.revision("run-m") == before_revision, "revision 不得增加"
    assert store.get("run-m") == before_state, "快照不得更新"
    assert store.list_events("run-m") == before_events, "事件不得写入"
    # 回滚后同一份状态仍可正常保存（说明没有留下半截事务）
    store.save(
        store.get("run-m"),
        expected_revision=before_revision,
        event=_event(run_id="run-m"),
    )
    assert store.revision("run-m") == before_revision + 1
