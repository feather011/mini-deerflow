"""Phase 25B / 预算账本与 WorkItem 租约。

覆盖规格第八节：调用前原子扣减、多线程消费不超卖、Lease 预留/消费/归还/
幂等关闭、重启后预算不重置、活动 Lease 查询、删除级联、预算超限不参与 Retry。

关键期望值独立写死：编排默认值 4 / 12 / 4 / 2 / 1 / 600，
不 import 生产常量做对照——改默认值测试必须跟着红。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from mini_deerflow.lead_agent import (
    BudgetLease,
    GoalContract,
    LeadBudgetLedger,
    LeadRunState,
    SQLiteLeadRunStore,
)
from mini_deerflow.middleware import Budget, BudgetExceededError, RetryPolicy

# --- 独立写死的期望默认值（不 import 生产常量） ---
EXPECTED_MAX_BATCHES = 4
EXPECTED_MAX_WORK_ITEMS = 12
EXPECTED_MAX_ITEMS_PER_BATCH = 4
EXPECTED_MAX_CONCURRENCY = 2
EXPECTED_MAX_REVISIONS_PER_ITEM = 1
EXPECTED_MAX_WALL_TIME_SECONDS = 600.0

T0 = 1_700_000_000.0


def _ledger(**overrides) -> LeadBudgetLedger:
    payload: dict = {}
    payload.update(overrides)
    return LeadBudgetLedger(**payload)


def _run(run_id: str = "run-1", thread_id: str = "t") -> LeadRunState:
    return LeadRunState.start(
        lead_run_id=run_id,
        goal=GoalContract.create(
            goal_id="g", objective="o", acceptance_criteria=["a"], now=T0
        ),
        thread_id=thread_id,
        now=T0,
    )


# ---------------------------------------------------------------------------
# 默认编排限制
# ---------------------------------------------------------------------------


def test_default_orchestration_limits() -> None:
    ledger = _ledger()
    assert ledger.max_batches == EXPECTED_MAX_BATCHES
    assert ledger.max_work_items == EXPECTED_MAX_WORK_ITEMS
    assert ledger.max_items_per_batch == EXPECTED_MAX_ITEMS_PER_BATCH
    assert ledger.max_concurrency == EXPECTED_MAX_CONCURRENCY
    assert ledger.max_revisions_per_item == EXPECTED_MAX_REVISIONS_PER_ITEM
    assert ledger.max_wall_time_seconds == EXPECTED_MAX_WALL_TIME_SECONDS


def test_ledger_reuses_existing_budget_not_a_third_implementation() -> None:
    """账本持有**现有的** middleware.Budget 实例，不重建一套。"""
    shared = Budget(max_model_calls=3)
    ledger = _ledger(resources=shared)
    assert ledger.resources is shared
    # 消耗走的是现有 Budget 的原子接口
    ledger.resources.consume_model_call()
    assert shared.model_calls() == 1
    with pytest.raises(BudgetExceededError):
        shared.consume_model_call()
        shared.consume_model_call()
        shared.consume_model_call()


def test_ledger_rejects_non_budget_resources() -> None:
    with pytest.raises(TypeError, match="Budget"):
        _ledger(resources="not-a-budget")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 编排消耗：原子扣减
# ---------------------------------------------------------------------------


def test_batches_consume_then_exceed() -> None:
    ledger = _ledger()
    for expected in range(1, EXPECTED_MAX_BATCHES + 1):
        assert ledger.consume_batch() == expected
    with pytest.raises(BudgetExceededError) as excinfo:
        ledger.consume_batch()
    assert excinfo.value.resource == "batches"
    assert excinfo.value.limit == EXPECTED_MAX_BATCHES
    # 超限不改变已用计数
    assert ledger.batches_used == EXPECTED_MAX_BATCHES


def test_work_items_respect_total_and_per_batch() -> None:
    ledger = _ledger()
    assert ledger.consume_work_item(2) == 2
    # 单批上限：一次超过 4 直接拒
    with pytest.raises(BudgetExceededError) as excinfo:
        ledger.consume_work_item(5)
    assert excinfo.value.resource == "items_per_batch"
    # 总量上限：4 批 × 4 条 = 16 > 12，第 3 批就该撞总量
    ledger2 = _ledger()
    for _ in range(3):
        ledger2.consume_work_item(4)
    with pytest.raises(BudgetExceededError) as excinfo:
        ledger2.consume_work_item(1)
    assert excinfo.value.resource == "work_items"
    assert excinfo.value.limit == EXPECTED_MAX_WORK_ITEMS
    assert ledger2.items_used == 12


@pytest.mark.parametrize("bad", [0, -1, "3", True, 1.5])
def test_work_item_rejects_bad_count(bad) -> None:
    with pytest.raises(ValueError, match="count"):
        _ledger().consume_work_item(bad)


def test_concurrency_slots() -> None:
    ledger = _ledger()
    assert ledger.acquire_slot() == 1
    assert ledger.acquire_slot() == EXPECTED_MAX_CONCURRENCY
    with pytest.raises(BudgetExceededError) as excinfo:
        ledger.acquire_slot()
    assert excinfo.value.resource == "concurrency"
    assert ledger.release_slot() == 1
    assert ledger.acquire_slot() == EXPECTED_MAX_CONCURRENCY
    assert ledger.peak_concurrency == EXPECTED_MAX_CONCURRENCY


def test_release_without_acquire_is_rejected() -> None:
    with pytest.raises(ValueError, match="配对"):
        _ledger().release_slot()


def test_wall_time_tracking() -> None:
    ledger = _ledger(max_wall_time_seconds=10.0)
    assert ledger.wall_time_exceeded(now=T0) is False  # 未 start 视为未超时
    ledger.begin_wall_time(T0)
    assert ledger.wall_time_exceeded(now=T0 + 5) is False
    assert ledger.wall_time_exceeded(now=T0 + 10) is True
    ledger.begin_wall_time(T0 + 100)  # 幂等：不重置起点
    assert ledger.started_at == T0


def test_concurrent_batch_consumption_never_exceeds_cap() -> None:
    """多线程抢同一个批次额度：成功次数必须**恰好等于**上限。"""
    ledger = _ledger()
    successes: list[int] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            successes.append(ledger.consume_batch())
        except BudgetExceededError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(successes) == EXPECTED_MAX_BATCHES
    assert len(errors) == 8 - EXPECTED_MAX_BATCHES
    assert ledger.batches_used == EXPECTED_MAX_BATCHES


def test_concurrent_work_item_consumption_never_oversells() -> None:
    """并发消耗工作项名额：总量不超过 12。"""
    ledger = _ledger()
    ok: list[int] = []
    rejected: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            for _ in range(3):
                ok.append(ledger.consume_work_item(2))
        except BudgetExceededError as exc:
            rejected.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert ledger.items_used <= EXPECTED_MAX_WORK_ITEMS
    assert ledger.items_used % 2 == 0
    assert rejected, "8 线程 × 3 次 × 2 条必然超出 12，上限必须拦下"
    assert len(ok) == ledger.items_used // 2


# ---------------------------------------------------------------------------
# Lease
# ---------------------------------------------------------------------------


def test_lease_reserve_consume_close() -> None:
    ledger = _ledger()
    lease = ledger.open_lease(run_id="run-1", work_id="w-1", reserved=5, now=T0)
    assert lease.reserved == 5
    assert lease.remaining_reserved == 5
    assert lease.active is True

    lease.consume("model_calls", 2)
    assert lease.consumed == 2
    assert lease.remaining_reserved == 3

    returned = lease.close(now=T0 + 1)
    assert returned == 3          # 只归还未用部分
    assert lease.returned == 3
    assert lease.consumed == 2     # consumed 不被归零
    assert lease.active is False
    assert lease.closed_at == T0 + 1


def test_lease_close_is_idempotent() -> None:
    lease = BudgetLease.open(run_id="r", work_id="w", reserved=4, now=T0)
    lease.consume("model_calls", 1)
    assert lease.close() == 3
    assert lease.close() == 0       # 幂等
    assert lease.close() == 0
    assert lease.returned == 3
    assert lease.consumed == 1


def test_lease_cannot_exceed_reservation() -> None:
    lease = BudgetLease.open(run_id="r", work_id="w", reserved=3, now=T0)
    lease.consume("model_calls", 3)
    assert lease.is_exhausted is True
    with pytest.raises(BudgetExceededError) as excinfo:
        lease.consume("model_calls", 1)
    assert excinfo.value.limit == 3
    assert lease.consumed == 3      # 超限不扣减


def test_lease_rejects_bad_consume_amount() -> None:
    lease = BudgetLease.open(run_id="r", work_id="w", reserved=3)
    for bad in (0, -1, "1", True, 1.5):
        with pytest.raises(ValueError, match="amount"):
            lease.consume("model_calls", bad)


def test_closed_lease_rejects_consumption() -> None:
    lease = BudgetLease.open(run_id="r", work_id="w", reserved=3)
    lease.close()
    with pytest.raises(BudgetExceededError, match="已关闭"):
        lease.consume("model_calls", 1)


def test_lease_belongs_to_single_run_and_work_item() -> None:
    lease = BudgetLease.open(run_id="run-A", work_id="w-1", reserved=2)
    assert lease.run_id == "run-A"
    assert lease.work_id == "w-1"
    payload = lease.to_dict()
    assert payload["run_id"] == "run-A"
    assert payload["work_id"] == "w-1"
    # 序列化后归属不变
    assert BudgetLease.from_dict(payload).run_id == "run-A"


@pytest.mark.parametrize("field", ["lease_id", "run_id", "work_id"])
def test_lease_rejects_blank_identity(field: str) -> None:
    payload = BudgetLease.open(run_id="r", work_id="w", reserved=1).to_dict()
    payload[field] = "  "
    with pytest.raises(ValueError):
        BudgetLease.from_dict(payload)


def test_lease_rejects_negative_reserved() -> None:
    with pytest.raises(ValueError, match="reserved"):
        BudgetLease(lease_id="l", run_id="r", work_id="w", reserved=-1)


def test_lease_rejects_corrupted_persistence() -> None:
    base = BudgetLease.open(run_id="r", work_id="w", reserved=3).to_dict()
    bad_consumed = dict(base, consumed=99)
    with pytest.raises(ValueError, match="consumed"):
        BudgetLease.from_dict(bad_consumed)
    bad_negative = dict(base, consumed=-1)
    with pytest.raises(ValueError, match="负"):
        BudgetLease.from_dict(bad_negative)
    # 已关闭但 returned 对不上恒等式
    closed = BudgetLease.open(run_id="r", work_id="w", reserved=3).to_dict()
    closed.update({"returned": 9, "closed_at": 1.0})
    with pytest.raises(ValueError, match="returned"):
        BudgetLease.from_dict(closed)


def test_concurrent_lease_consume_never_exceeds_reservation() -> None:
    """并发消费同一租约：总消耗不超过 reserved。"""
    lease = BudgetLease.open(run_id="r", work_id="w", reserved=6)
    ok: list[int] = []
    rejected: list[BaseException] = []
    barrier = threading.Barrier(6)

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            ok.append(lease.consume("model_calls", 1))
        except BudgetExceededError as exc:
            rejected.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert lease.consumed == 6
    assert len(ok) == 6
    assert lease.remaining_reserved == 0
    # 全部 consumed → 无可归还；consumed 永远不归还
    assert lease.close() == 0
    assert lease.consumed == 6


def test_active_lease_query() -> None:
    ledger = _ledger()
    a = ledger.open_lease(run_id="run-1", work_id="w-a", reserved=3)
    b = ledger.open_lease(run_id="run-1", work_id="w-b", reserved=4)
    c = ledger.open_lease(run_id="run-2", work_id="w-c", reserved=5)
    a.close()
    b.close()

    assert {x.lease_id for x in ledger.active_leases()} == {c.lease_id}
    assert {x.lease_id for x in ledger.active_leases(run_id="run-2")} == {c.lease_id}
    assert ledger.active_leases(work_id="w-b") == ()   # 已关闭
    assert len(ledger.all_leases()) == 3                # 全量含已关闭
    assert ledger.reserved_total == 12
    assert ledger.reserved_active == 5


def test_interrupt_lease_returns_unused_only() -> None:
    ledger = _ledger()
    lease = ledger.open_lease(run_id="run-1", work_id="w-1", reserved=10)
    lease.consume("model_calls", 4)
    assert ledger.interrupt_lease(lease.lease_id) == 6
    assert ledger.interrupt_lease(lease.lease_id) == 0   # 幂等


def test_interrupt_all_leases() -> None:
    ledger = _ledger()
    ledger.open_lease(run_id="r", work_id="w1", reserved=10)
    ledger.open_lease(run_id="r", work_id="w2", reserved=5)
    assert ledger.interrupt_all_leases() == 15
    assert ledger.active_leases() == ()
    assert ledger.interrupt_all_leases() == 0


def test_get_missing_lease_raises() -> None:
    from mini_deerflow.lead_agent import LeaseNotFoundError

    with pytest.raises(LeaseNotFoundError):
        _ledger().get_lease("lease-nope")


# ---------------------------------------------------------------------------
# 持久化与恢复
# ---------------------------------------------------------------------------


def test_ledger_state_roundtrip_preserves_everything() -> None:
    ledger = _ledger(resources=Budget(max_model_calls=10, max_tool_calls=4))
    ledger.consume_batch()
    ledger.consume_batch()
    ledger.consume_work_item(3)
    ledger.acquire_slot()
    ledger.begin_wall_time(T0)
    lease = ledger.open_lease(run_id="run-1", work_id="w-1", reserved=7)
    lease.consume("model_calls", 2)
    closed = ledger.open_lease(run_id="run-1", work_id="w-2", reserved=3)
    closed.close()
    ledger.resources.consume_model_call()
    ledger.resources.consume_tool_call()

    state = ledger.to_state()
    # 状态必须可 JSON 序列化（要落库）
    json.dumps(state, ensure_ascii=False)

    restored = LeadBudgetLedger.from_state(state)
    assert restored.batches_used == 2
    assert restored.items_used == 3
    assert restored.concurrency_in_use == 1
    assert restored.started_at == T0
    assert restored.max_batches == EXPECTED_MAX_BATCHES
    assert len(restored.all_leases()) == 2
    assert restored.reserved_total == ledger.reserved_total
    # 现有资源 Budget 的用量也被恢复：
    # 租约消耗 2 + Direct 消费 1 = 3 —— 两侧都记在同一根 Budget 上
    assert restored.resources.model_calls() == 3
    assert restored.resources.tool_calls() == 1
    assert restored.resources.max_model_calls == 10
    active = restored.active_leases()
    assert len(active) == 1
    assert active[0].consumed == 2   # 活动租约的消耗也被恢复
    assert active[0].consumed_by_resource.get("model_calls") == 2


def test_restore_does_not_reset_budget_after_restart() -> None:
    """重启后预算**不重置**：已消耗的额度仍然占着。"""
    ledger = _ledger()
    for _ in range(2):
        ledger.consume_batch()
    ledger.consume_work_item(2)

    restarted = LeadBudgetLedger.from_state(ledger.to_state())

    # 还能再消费剩余额度
    restarted.consume_batch()
    restarted.consume_batch()
    with pytest.raises(BudgetExceededError):
        restarted.consume_batch()          # 第 5 次必撞 4 的上限
    assert restarted.batches_used == 4

    restarted.consume_work_item(2)        # 4 + 2 = 6 ≤ 12
    with pytest.raises(BudgetExceededError):
        for _ in range(10):
            restarted.consume_work_item(4)  # 必然撞 12
    assert restarted.items_used <= EXPECTED_MAX_WORK_ITEMS


def test_from_state_reuses_given_budget_instance() -> None:
    shared = Budget(max_model_calls=5)
    source = _ledger(resources=shared)
    source.resources.consume_model_call()
    target = Budget()
    restored = LeadBudgetLedger.from_state(source.to_state(), resources=target)
    assert restored.resources is target
    assert target.model_calls() == 1


def test_ledger_rejects_corrupted_state() -> None:
    with pytest.raises(ValueError, match="字典"):
        LeadBudgetLedger.from_state("nope")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="字典"):
        LeadBudgetLedger.from_state({"resources": "nope"})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="数组"):
        LeadBudgetLedger.from_state({"leases": "nope"})


# ---------------------------------------------------------------------------
# SQLite 持久化：重启恢复 + 级联
# ---------------------------------------------------------------------------


def test_sqlite_ledger_survives_restart(tmp_path: Path) -> None:
    db = tmp_path / "budget.db"
    ledger = _ledger()
    ledger.consume_batch()
    ledger.open_lease(run_id="run-1", work_id="w-1", reserved=8).consume("model_calls", 3)
    ledger.resources.consume_model_call()

    first = SQLiteLeadRunStore(db)
    first.create(_run("run-1", thread_id="t"))
    first.save_ledger("run-1", ledger)
    first.close()

    reopened = SQLiteLeadRunStore(db)
    restored = reopened.get_ledger("run-1")
    assert restored is not None
    assert restored.batches_used == 1
    # 租约扣 3 + Direct 扣 1 = 4（重启后预算不重置）
    assert restored.resources.model_calls() == 4
    leases = reopened.load_leases("run-1")
    assert len(leases) == 1
    assert leases[0].consumed == 3
    assert leases[0].reserved == 8
    assert leases[0].consumed_by_resource.get("model_calls") == 3
    # 用恢复出的账本继续扣减，上限照常生效
    restored.consume_batch()
    reopened.save_ledger("run-1", restored)
    again = SQLiteLeadRunStore(db)
    assert again.get_ledger("run-1").batches_used == 2  # type: ignore[union-attr]
    reopened.close()
    again.close()


def test_sqlite_ledger_upsert_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "upsert.db"
    handle = SQLiteLeadRunStore(db)
    handle.create(_run("run-1", thread_id="t"))
    ledger = _ledger()
    ledger.open_lease(run_id="run-1", work_id="w-1", reserved=4)
    handle.save_ledger("run-1", ledger)
    handle.save_ledger("run-1", ledger)          # 重复保存不留双份
    assert len(handle.load_leases("run-1")) == 1

    # interrupt 只是**关闭**租约，不删除——已关闭的租约仍要留档供审计
    ledger.interrupt_all_leases()
    handle.save_ledger("run-1", ledger)
    reloaded = handle.load_leases("run-1")
    assert len(reloaded) == 1
    assert reloaded[0].closed_at is not None
    assert reloaded[0].returned == 4          # 归还额度已记账

    # 真正的「移除」来自删除整个 Run：外键级联清掉租约
    handle.delete("run-1")
    assert handle.load_leases("run-1") == ()
    assert handle.get_ledger("run-1") is None
    handle.close()


def test_sqlite_get_ledger_missing_returns_none(tmp_path: Path) -> None:
    handle = SQLiteLeadRunStore(tmp_path / "none.db")
    handle.create(_run("run-1", thread_id="t"))
    assert handle.get_ledger("run-1") is None
    handle.close()


def test_sqlite_ledger_rejects_non_ledger(tmp_path: Path) -> None:
    handle = SQLiteLeadRunStore(tmp_path / "bad.db")
    handle.create(_run("run-1", thread_id="t"))
    with pytest.raises(TypeError):
        handle.save_ledger("run-1", "not-a-ledger")  # type: ignore[arg-type]
    handle.close()


def test_sqlite_ledger_rejects_corrupted_json(tmp_path: Path) -> None:
    from mini_deerflow.lead_agent import LeadStoreDataCorruptedError

    db = tmp_path / "badledger.db"
    handle = SQLiteLeadRunStore(db)
    handle.create(_run("run-1", thread_id="t"))
    handle.save_ledger("run-1", _ledger())
    handle.close()

    raw = sqlite3.connect(db)
    raw.execute("UPDATE lead_budget_ledgers SET ledger_json = 'oops' WHERE run_id = ?", ("run-1",))
    raw.commit()
    raw.close()

    reopened = SQLiteLeadRunStore(db)
    with pytest.raises(LeadStoreDataCorruptedError, match="JSON"):
        reopened.get_ledger("run-1")
    reopened.close()


def test_sqlite_delete_run_cascades_to_ledger_and_leases(tmp_path: Path) -> None:
    """删 Run 时预算记录与租约一并级联清理。"""
    db = tmp_path / "cascade.db"
    handle = SQLiteLeadRunStore(db)
    handle.create(_run("run-1", thread_id="t"))
    ledger = _ledger()
    ledger.open_lease(run_id="run-1", work_id="w-1", reserved=3)
    handle.save_ledger("run-1", ledger)
    assert handle.load_leases("run-1")

    handle.delete("run-1")

    assert handle.get_ledger("run-1") is None
    assert handle.load_leases("run-1") == ()
    assert handle.count_events("run-1") == 0
    leftover = sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM lead_budget_leases WHERE run_id = ?", ("run-1",)
    ).fetchone()[0]
    assert leftover == 0
    handle.close()


# ---------------------------------------------------------------------------
# 与现有 Budget / Retry 语义的关系
# ---------------------------------------------------------------------------


def test_budget_exceeded_is_not_retryable() -> None:
    """BudgetExceededError 不参与 Retry：重试一个已超限的操作只会再超一次。

    判定方式与 `RetryMiddleware` 内部一致（`isinstance(exc, policy.retryable_types)`），
    而不是去调一个并不存在的方法。
    """
    from mini_deerflow.lead_agent import LeadRunConflictError

    policy = RetryPolicy(max_attempts=3, retryable_types=(RuntimeError, ConnectionError))

    exhausted = BudgetExceededError("model_calls", 3)
    assert not isinstance(exhausted, policy.retryable_types)
    assert not issubclass(BudgetExceededError, RuntimeError)
    assert not issubclass(BudgetExceededError, ConnectionError)

    # 同理，CAS 冲突属于「重读再写」而非「原地重试」
    conflict = LeadRunConflictError("r", expected=0, actual=1)
    assert not isinstance(conflict, policy.retryable_types)
    # 而普通 RuntimeError 确实可重试（说明上面的判断确实有意义）
    assert isinstance(RuntimeError("x"), policy.retryable_types)


def test_ledger_rejects_invalid_limits() -> None:
    for field in (
        "max_batches",
        "max_work_items",
        "max_items_per_batch",
        "max_concurrency",
        "max_revisions_per_item",
    ):
        with pytest.raises(ValueError, match=field):
            _ledger(**{field: -1})
    with pytest.raises(ValueError, match="max_wall_time_seconds"):
        _ledger(max_wall_time_seconds=0)


def test_restore_usage_rejects_negative() -> None:
    budget = Budget(max_model_calls=3)
    with pytest.raises(ValueError, match="不能为负"):
        budget.restore_usage(model_calls=-1)
    with pytest.raises(TypeError, match="必须是整数"):
        budget.restore_usage(steps="1")  # type: ignore[arg-type]


def test_restore_usage_is_explicit_not_toctou() -> None:
    """restore_usage 只写值不做上限判断；超限留给下一次 consume 报错。"""
    budget = Budget(max_model_calls=2)
    budget.restore_usage(model_calls=99)
    assert budget.model_calls() == 99
    with pytest.raises(BudgetExceededError):
        budget.consume_model_call()


# ===========================================================================
# 根额度与租约的原子联动（修复项 1）
# ===========================================================================


def test_available_formula() -> None:
    """available = max_lease_credits - leased_consumed - Σ(活动租约 remaining_reserved)"""
    ledger = _ledger(max_lease_credits=10)
    assert ledger.available == 10

    lease = ledger.open_lease(run_id="r", work_id="w1", reserved=4)
    assert ledger.available == 6          # 预留 4 未消费也算占用
    lease.consume("model_calls", 1)
    assert ledger.leased_consumed == 1
    assert lease.remaining_reserved == 3
    assert ledger.available == 10 - 1 - 3  # = 6，预留中未花的仍占着

    other = ledger.open_lease(run_id="r", work_id="w2", reserved=3)
    # 此时 leased_consumed 仍=1（第二个只预留未消费），
    # 两个租约的 remaining = 3 + 3 = 6 全部占着额度
    assert ledger.available == 10 - 1 - 3 - 3   # = 3

    other.consume("model_calls", 3)                       # 第二个全部花掉 → 它的 remaining 归 0
    # 第一个租约 remaining 仍是 3，继续占着；leased_consumed 累计 4
    assert ledger.leased_consumed == 4
    assert ledger.available == 10 - 4 - 3   # = 3，未花的预留不因别人花完而释放

    # 关闭第二个租约：它的 reserved 已全部被 consumed 吃掉，close 归还 0，
    # available 不变——真正释放发生在「租约关闭且 remaining 归还」
    assert other.close() == 0
    assert ledger.available == 10 - 4 - 3


def test_lease_consumption_is_capped_by_reservation() -> None:
    ledger = _ledger(max_lease_credits=10)
    lease = ledger.open_lease(run_id="r", work_id="w1", reserved=3)
    lease.consume("model_calls", 3)
    assert lease.consumed == 3
    assert ledger.leased_consumed == 3
    with pytest.raises(BudgetExceededError):
        lease.consume("model_calls", 1)                     # 超过本租约预留
    assert ledger.leased_consumed == 3        # 失败的消费不计入根账本


def test_two_leases_cannot_oversell_root_credit() -> None:
    """根额度 4，两个租约各尝试消费 3 —— 合计成功消费必须 ≤ 4。

    这正是「只改 self.consumed 无法证明根预算不超卖」的判据：
    第二个租约要么开不出预留，要么消费时撞 available。
    """
    ledger = _ledger(max_lease_credits=4)
    opened: list = []
    for index in range(2):
        try:
            opened.append(
                ledger.open_lease(run_id="r", work_id=f"w{index}", reserved=3)
            )
        except BudgetExceededError:
            break
    assert len(opened) == 1, "根额度 4 开不出两个 reserved=3 的租约"

    # 第一个租约能消费 3
    opened[0].consume("model_calls", 3)
    assert opened[0].consumed == 3
    assert ledger.leased_consumed == 3

    # 再开一个租约，消费 3 必须失败（available 只剩 1）
    with pytest.raises(BudgetExceededError) as excinfo:
        second = ledger.open_lease(run_id="r", work_id="w2", reserved=3)
        second.consume("model_calls", 3)
    # 无论失败发生在开预留还是消费，根额度都不许被击穿
    assert ledger.leased_consumed <= 4
    assert sum(l.consumed for l in ledger.all_leases()) <= 4


def test_root_credit_blocks_second_lease_even_when_its_own_reservation_fits() -> None:
    """根额度不足时 open_lease 直接失败——两个线程不能都拿到名义预留。"""
    ledger = _ledger(max_lease_credits=4)
    ledger.open_lease(run_id="r", work_id="w1", reserved=3)
    with pytest.raises(BudgetExceededError) as excinfo:
        ledger.open_lease(run_id="r", work_id="w2", reserved=2)
    assert excinfo.value.resource == "lease_credits"
    assert ledger.available == 1


def test_lease_close_releases_only_remaining_reserved() -> None:
    """关闭租约释放未消费预留，consumed 永不归还。"""
    ledger = _ledger(max_lease_credits=10)
    lease = ledger.open_lease(run_id="r", work_id="w1", reserved=6)
    lease.consume("model_calls", 2)
    assert ledger.leased_consumed == 2
    assert lease.close() == 4                      # 只归还未用的 4
    assert ledger.leased_consumed == 2             # 已消费的不动
    assert ledger.available == 10 - 2 - 0          # = 8


def test_lease_view_charges_root_resources_once_per_call() -> None:
    """leased view 的消费**必须计入根 Budget**——否则 Executor 能靠租约
    绕过 `max_model_calls`，全局 Budget 变成摆设。

    同时验证**不双扣**：一笔模型调用在根上恰好 +1（不是租约+1 再根+1=2），
    且 model 与 tool 各自独立计数。
    """
    ledger = _ledger(max_lease_credits=10, resources=Budget(max_model_calls=100))
    lease = ledger.open_lease(run_id="r", work_id="w1", reserved=5)
    view = lease.view()
    view.consume_model_call()
    view.consume_model_call()
    view.consume_tool_call()

    # 租约侧
    assert lease.consumed == 3
    assert lease.consumed_by_resource.get("model_calls") == 2
    assert lease.consumed_by_resource.get("tool_calls") == 1
    assert ledger.leased_consumed == 3

    # 根侧：被扣了，且**恰好等于调用次数**（不是翻倍）
    assert ledger.resources.model_calls() == 2, "根 model_calls 必须随租约消费增加"
    assert ledger.resources.tool_calls() == 1
    assert ledger.resources.steps() == 0, "未调用的资源不该被牵连扣减"

    # 读取器返回租约侧该资源的用量
    assert view.model_calls() == 2
    assert view.tool_calls() == 1
    assert view.total_provider_calls() == 2


def test_lease_model_calls_hit_root_limit() -> None:
    """两个 Lease 共同消费 model_calls，合计不能超过根 max_model_calls。"""
    root_limit = 3
    ledger = _ledger(
        max_lease_credits=100,
        resources=Budget(max_model_calls=root_limit),
    )
    first = ledger.open_lease(run_id="r", work_id="w1", reserved=5)
    second = ledger.open_lease(run_id="r", work_id="w2", reserved=5)

    ok = 0
    exceeded: list[BaseException] = []
    for lease in (first, second, first, second, first, second):
        try:
            lease.view().consume_model_call()
            ok += 1
        except BudgetExceededError as exc:
            exceeded.append(exc)

    # 三个租约能各自拿到预留，但根只认 max_model_calls=3
    assert ledger.resources.model_calls() == root_limit
    assert ok == root_limit
    assert exceeded, "根上限必须拦下超出的调用"
    # 根侧撞限的资源就是 model_calls
    assert all(
        isinstance(e, BudgetExceededError) and e.resource == "model_calls"
        for e in exceeded
    )


def test_direct_consumption_cannot_oversell_leased_reservation() -> None:
    """Direct Planner 消费**不能**抢走活动 Lease 已预留的额度。"""
    ledger = _ledger(
        max_lease_credits=100,
        resources=Budget(max_model_calls=4),
    )
    # 租约预留 3：这 3 个额度被占住，Direct 不能碰
    ledger.open_lease(
        run_id="r", work_id="w1", reserved=10, reserved_by_resource={"model_calls": 3}
    )
    assert ledger.available_for("model_calls") == 1

    # Direct 先用掉剩余的 1 个
    ledger.consume_direct("model_calls")
    assert ledger.resources.model_calls() == 1
    assert ledger.available_for("model_calls") == 0

    # 再用就必须失败——它抢不到租约的预留
    with pytest.raises(BudgetExceededError) as excinfo:
        ledger.consume_direct("model_calls")
    assert excinfo.value.resource == "model_calls"
    assert ledger.resources.model_calls() == 1

    # 被占住的额度确实还在：租约消费仍然成功
    active = ledger.active_leases()[0]
    active.view().consume_model_call()
    assert ledger.resources.model_calls() == 2


def test_resource_reservations_are_independent() -> None:
    """不同资源分别计数：model 的预留不占 tool_calls 的额度。"""
    ledger = _ledger(
        max_lease_credits=100,
        resources=Budget(max_model_calls=10, max_tool_calls=10),
    )
    lease = ledger.open_lease(
        run_id="r",
        work_id="w1",
        reserved=20,
        reserved_by_resource={"model_calls": 5, "tool_calls": 2},
    )
    # available = limit - usage - Σoutstanding（租约预留也占额度）
    assert ledger.available_for("model_calls") == 10 - 0 - 5   # = 5
    assert ledger.available_for("tool_calls") == 10 - 0 - 2    # = 8

    # 租约内部消费只把「预留」转成「已用」：usage +1、outstanding −1，
    # available = limit − usage − outstanding 因此不变（10−2−3 = 5）。
    lease.view().consume_model_call()
    lease.view().consume_model_call()
    assert ledger.resources.model_calls() == 2, "租约消费必须同步扣根 Budget"
    assert ledger.available_for("model_calls") == 5
    assert ledger.available_for("tool_calls") == 8, "model 消费不该动 tool 的可用量"

    # Direct 消费只减自己的资源：根 usage +1、outstanding 不变
    ledger.consume_direct("tool_calls")
    assert ledger.resources.tool_calls() == 1
    assert ledger.available_for("tool_calls") == 7
    assert ledger.available_for("model_calls") == 5, "tool 的 Direct 消费不该动 model"

    # 租约内 tool 超预留会被拦：预留 2，用满 2 之后第 3 次必须抛
    lease.view().consume_tool_call()
    lease.view().consume_tool_call()
    with pytest.raises(BudgetExceededError):
        lease.view().consume_tool_call()
    assert ledger.resources.tool_calls() == 3, "被拦的那笔不扣根 Budget"


def test_lease_view_rejects_over_reservation() -> None:
    ledger = _ledger(max_lease_credits=10)
    lease = ledger.open_lease(run_id="r", work_id="w1", reserved=2)
    view = lease.view()
    view.consume_model_call()
    view.consume_model_call()
    with pytest.raises(BudgetExceededError):
        view.consume_model_call()          # 超过 reserved=2
    assert lease.consumed == 2


def test_persistence_hook_runs_before_returning_credit() -> None:
    """成功路径：钩子收到**候选快照**，且每次消费都先落库再回写内存。"""
    ledger = _ledger(max_lease_credits=10)
    seen: list[dict] = []
    ledger.bind_persistence(lambda state: seen.append(state))
    lease = ledger.open_lease(run_id="r", work_id="w1", reserved=3)
    lease.consume("model_calls", 1)
    lease.consume("model_calls", 1)
    # 钩子拿到的是「成功之后会是什么样」，不是账本对象
    assert [s["leased_consumed"] for s in seen] == [1, 2]
    assert seen[0]["resources"]["model_calls"] == 1
    # 快照里的租约计数也已经是扣减后的值（落库即最终值）
    assert seen[1]["leases"][0]["consumed_by_resource"] == {"model_calls": 2}
    assert lease.consumed == 2
    # 候选快照不共享可变对象：改快照不会污染账本
    seen[1]["resources"]["model_calls"] = 999
    assert ledger.resources.model_calls() == 2


def test_bind_persistence_rejects_non_callable() -> None:
    with pytest.raises(TypeError):
        _ledger().bind_persistence("not-callable")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# copy-on-write 持久化：钩子失败必须「内存一处不变、库一处不变」
# ---------------------------------------------------------------------------


def _cow_fixture(tmp_path: Path):
    """建库 + 存一个 model_calls=0 / 租约预留 3 的基线，返回各句柄。"""
    db = tmp_path / "cow.db"
    store = SQLiteLeadRunStore(db)
    store.create(_run("run-1", thread_id="t"))
    ledger = _ledger(resources=Budget(max_model_calls=10, max_tool_calls=10))
    lease = ledger.open_lease(
        run_id="run-1",
        work_id="w-1",
        reserved=5,
        reserved_by_resource={"model_calls": 3, "tool_calls": 1},
    )
    store.save_ledger("run-1", ledger)
    return store, ledger, lease


def test_hook_failure_leaves_memory_untouched(tmp_path: Path) -> None:
    """钩子抛错：内存的根用量、租约计数、剩余预留全部保持扣减前。"""
    store, ledger, lease = _cow_fixture(tmp_path)

    def exploding(_state: dict) -> None:
        raise RuntimeError("落库失败")

    try:
        ledger.bind_persistence(exploding)

        external = {"called": False}

        def call_model() -> None:
            lease.consume("model_calls", 2)
            external["called"] = True      # 只有扣减成功才轮得到模型调用

        with pytest.raises(RuntimeError, match="落库失败"):
            call_model()

        assert external["called"] is False, "落库失败时不得发起外部调用"
        assert ledger.resources.model_calls() == 0, "根 Budget 用量必须未变"
        assert ledger.leased_consumed == 0, "信用池必须未变"
        assert lease.consumed == 0, "租约总消耗必须未变"
        assert lease.consumed_by_resource.get("model_calls", 0) == 0
        # 租约剩余预留：预留 3、没花掉任何一笔
        assert lease.remaining_reserved_for("model_calls") == 3
        assert ledger.available_for("model_calls") == 10 - 0 - 3
    finally:
        store.close()


def test_hook_failure_leaves_sqlite_untouched(tmp_path: Path) -> None:
    """钩子抛错：数据库里的账本与租约都还停在上一个一致状态。"""
    store, ledger, lease = _cow_fixture(tmp_path)

    def exploding(_state: dict) -> None:
        raise RuntimeError("落库失败")

    ledger.bind_persistence(exploding)
    with pytest.raises(RuntimeError, match="落库失败"):
        lease.consume("model_calls", 2)
    store.close()

    reopened = SQLiteLeadRunStore(tmp_path / "cow.db")
    try:
        restored = reopened.get_ledger("run-1")
        assert restored is not None
        assert restored.resources.model_calls() == 0, "库里的根用量必须未变"
        assert restored.leased_consumed == 0
        leases = reopened.load_leases("run-1")
        assert len(leases) == 1
        assert leases[0].consumed == 0, "库里的租约消耗必须未变"
        assert leases[0].reserved_by_resource == {"model_calls": 3, "tool_calls": 1}
    finally:
        reopened.close()


def test_hook_failure_also_blocks_direct_charge(tmp_path: Path) -> None:
    """Direct 扣减同样先落库后回写：钩子失败时根用量不动。"""
    store, ledger, _lease = _cow_fixture(tmp_path)

    def exploding(_state: dict) -> None:
        raise RuntimeError("落库失败")

    ledger.bind_persistence(exploding)
    with pytest.raises(RuntimeError, match="落库失败"):
        ledger.consume_direct("model_calls")
    assert ledger.resources.model_calls() == 0
    assert ledger.available_for("model_calls") == 10 - 0 - 3, "预留不该被 Direct 挤掉"
    store.close()


def test_hook_failure_then_retry_succeeds(tmp_path: Path) -> None:
    """失败不留残留：钩子恢复后重试一次成功，账本只被扣一次。"""
    store, ledger, lease = _cow_fixture(tmp_path)
    state = {"fail": True}

    def flaky(candidate: dict) -> None:
        if state["fail"]:
            raise RuntimeError("落库失败")
        store.save_ledger_state("run-1", candidate)

    ledger.bind_persistence(flaky)
    with pytest.raises(RuntimeError, match="落库失败"):
        lease.consume("model_calls", 2)
    assert ledger.resources.model_calls() == 0

    state["fail"] = False
    lease.consume("model_calls", 2)
    assert ledger.resources.model_calls() == 2
    assert lease.consumed == 2

    persisted = store.get_ledger("run-1")
    assert persisted is not None
    assert persisted.resources.model_calls() == 2, "成功路径必须真的落库"
    assert store.load_leases("run-1")[0].consumed_by_resource == {"model_calls": 2}
    store.close()


def test_closed_store_as_hook_fails_without_touching_memory(tmp_path: Path) -> None:
    """钩子是真实存储时，存储异常（连接已关）同样触发整笔回滚。"""
    from mini_deerflow.lead_agent.errors import LeadStoreClosedError

    store, ledger, lease = _cow_fixture(tmp_path)
    ledger.bind_persistence(lambda state: store.save_ledger_state("run-1", state))
    store.close()

    with pytest.raises(LeadStoreClosedError):
        lease.consume("model_calls", 2)
    assert ledger.resources.model_calls() == 0
    assert lease.consumed == 0
    assert lease.remaining_reserved_for("model_calls") == 3


def test_concurrent_lease_consumption_never_oversells_root() -> None:
    """8 线程抢同一根额度，合计成功消费不超过 max_lease_credits。"""
    ledger = _ledger(max_lease_credits=4)
    lease = ledger.open_lease(run_id="r", work_id="w1", reserved=4)
    ok: list[int] = []
    rejected: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            ok.append(lease.consume("model_calls", 1))
        except BaseException as exc:  # noqa: BLE001 - 收集断言
            rejected.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert lease.consumed == 4                # 恰好等于根额度
    assert len(ok) == 4
    assert len(rejected) == 4
    assert ledger.leased_consumed == 4


# ===========================================================================
# restore_usage 收紧（修复项 4）
# ===========================================================================


def test_restore_usage_can_only_be_called_once() -> None:
    budget = Budget(max_model_calls=5)
    budget.restore_usage(model_calls=1)
    with pytest.raises(RuntimeError, match="只能调用一次"):
        budget.restore_usage(model_calls=2)


def test_restore_usage_rejects_already_consumed_budget() -> None:
    """不能拿它把已有计数改小或清零。"""
    budget = Budget(max_model_calls=5)
    budget.consume_model_call()
    with pytest.raises(RuntimeError, match="尚未消费过"):
        budget.restore_usage(model_calls=0)


def test_restore_usage_rejects_clearing_existing_counts() -> None:
    budget = Budget(max_model_calls=5)
    budget.restore_usage(tool_calls=2)
    with pytest.raises(RuntimeError):
        budget.restore_usage()          # 第二次会把 tool_calls 清零


@pytest.mark.parametrize("bad", [True, False, "3", 1.5, None])
def test_restore_usage_rejects_bool_and_wrong_types(bad) -> None:
    budget = Budget(max_model_calls=5)
    with pytest.raises(TypeError, match="必须是整数"):
        budget.restore_usage(model_calls=bad)  # type: ignore[arg-type]


def test_restore_usage_rejects_unknown_field() -> None:
    budget = Budget(max_model_calls=5)
    with pytest.raises(TypeError):
        budget.restore_usage(mystery_counter=3)  # type: ignore[call-arg]


def test_restore_usage_may_exceed_limit_but_next_consume_fails() -> None:
    """恢复的历史事实即使超限也保留；超限由下一次 consume 暴露。"""
    budget = Budget(max_model_calls=2)
    budget.restore_usage(model_calls=5)
    assert budget.model_calls() == 5
    with pytest.raises(BudgetExceededError):
        budget.consume_model_call()


def test_restore_usage_concurrent_calls_only_one_wins() -> None:
    """并发恢复也只成功一次：闸门在 `Budget._lock` 内，不会两个人都过。"""
    budget = Budget(max_model_calls=10)
    thread_count = 8
    barrier = threading.Barrier(thread_count)
    ok: list[int] = []
    rejected: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            budget.restore_usage(model_calls=index + 1)
            ok.append(index)
        except BaseException as exc:  # noqa: BLE001 - 收集断言
            rejected.append(exc)

    threads = [
        threading.Thread(target=worker, args=(i,)) for i in range(thread_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(ok) == 1, f"只能一个成功，实际成功 {ok}"
    assert len(rejected) == thread_count - 1
    assert all(isinstance(exc, RuntimeError) for exc in rejected)
    # 落盘的是**获胜者**写入的值，不是被并发覆盖后的杂烩
    assert budget.model_calls() == ok[0] + 1


def test_ledger_state_roundtrip_preserves_credit_fields() -> None:
    ledger = _ledger(max_lease_credits=9)
    lease = ledger.open_lease(run_id="r", work_id="w1", reserved=5)
    lease.consume("model_calls", 2)
    restored = LeadBudgetLedger.from_state(ledger.to_state())
    assert restored.max_lease_credits == 9
    assert restored.leased_consumed == 2
    assert restored.available == 9 - 2 - 3   # 预留 3 未消费仍占着
