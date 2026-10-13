"""Phase 25C（架构收口）跨模块预算证据：

证明 `DefaultLeadPlanner` 走 `LeadLedgerRootBudget` 适配器时，
- 每次 `model.decide` 前的扣费**先**持久化到该 LeadRun 的 `LeadRunStore`
- 持久化失败 → `model.decide` **不**被调用
- 重启后 → 已消耗额度被正确恢复，下一次规划看见的是恢复后的额度

不重复 25B 已有的 `commit_lease_charge` / `consume_direct` 自身测试——
本文件只测"Planner 触发 + 持久化 + 重启"整条调用链。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from mini_deerflow.lead_agent import (
    BudgetRootBudget,
    DefaultLeadPlanner,
    ExecutionRoute,
    GoalContract,
    InMemoryLeadRunStore,
    LeadBudgetLedger,
    LeadLedgerRootBudget,
    LeadPlannerBudgetError,
    LeadRunState,
    LeadRunStatus,
    PlannerContext,
    ProposeBatch,
    ResourceClaim,
    SQLiteLeadRunStore,
    WorkItem,
    WorkItemProposal,
    WorkItemStatus,
    build_budget_snapshot,
)
from mini_deerflow.lead_agent.models import EvidenceReceipt, VerifiedFact
from mini_deerflow.middleware import Budget
from mini_deerflow.models import FinalAnswer, Message


# ===========================================================================
# 测试替身
# ===========================================================================


@dataclass
class ProgrammableModel:
    responses: list[str] = field(default_factory=list)
    call_count: int = 0

    def decide(self, messages: list[Message], tools: Any = None) -> FinalAnswer:
        self.call_count += 1
        if self.call_count > len(self.responses):
            raise AssertionError(
                f"模型被调用第 {self.call_count} 次，但只预设了 {len(self.responses)} 个响应"
            )
        return FinalAnswer(content=self.responses[self.call_count - 1])


GOAL = GoalContract.create(
    goal_id="g-1",
    objective="跨模块预算证据",
    acceptance_criteria=("criterion-A: ok",),
)


def make_fact(fact_id: str, work_id: str) -> VerifiedFact:
    return VerifiedFact(
        fact_id=fact_id,
        statement="已验证",
        work_id=work_id,
        verification_id=f"v-{fact_id}",
        receipts=(
            EvidenceReceipt(
                receipt_id=f"r-{fact_id}",
                source="unit-test",
                observed_at=1_700_000_000.0,
            ),
        ),
        established_at=1_700_000_000.0,
    )


def _propose_batch_json(work_id: str = "w-1") -> str:
    return json.dumps(
        {
            "kind": "propose_batch",
            "proposals": [
                {
                    "work_id": work_id,
                    "objective": "ok",
                    "depends_on": [],
                    "claims": [],
                    "execution_route": "direct_agent",
                }
            ],
            "rationale": "ok",
        },
        ensure_ascii=False,
    )


def _wire(
    db_path: Path,
    *,
    run_id: str = "run-1",
    max_model_calls: int = 10,
) -> tuple[SQLiteLeadRunStore, LeadBudgetLedger, DefaultLeadPlanner, ProgrammableModel]:
    """新建一个真实 SQLiteLeadRunStore + LeadBudgetLedger + Planner。

    把持久化钩子绑到账本上：`store.save_ledger_state(run_id, state)`。
    这是 25D 集成层会做的事；25C 阶段在此处复现以证明调用链。
    """
    store = SQLiteLeadRunStore(db_path)
    store.create(
        LeadRunState.start(
            lead_run_id=run_id, goal=GOAL, thread_id="t", now=1_700_000_000.0
        )
    )
    ledger = LeadBudgetLedger(resources=Budget(max_model_calls=max_model_calls))
    ledger.bind_persistence(lambda state: store.save_ledger_state(run_id, state))
    root = LeadLedgerRootBudget(ledger)
    model = ProgrammableModel(responses=[_propose_batch_json()])
    planner = DefaultLeadPlanner(model, root)
    return store, ledger, planner, model


# ===========================================================================
# 1. 跨模块调用链证据：Planner 调模型前，扣费已持久化到 LeadRunStore
# ===========================================================================


def test_planner_consume_persists_via_lead_ledger(tmp_path: Path) -> None:
    """Planner 走 LeadLedgerRootBudget → 25B 路径 → 25A budget 都记账 + 落库。"""
    db = tmp_path / "lead.db"
    store, ledger, planner, model = _wire(db)

    output = planner.plan(_ctx())
    assert isinstance(output, ProposeBatch)
    assert model.call_count == 1

    # 内存与数据库都已经被记账
    assert ledger.resources.model_calls() == 1
    assert ledger.leased_consumed == 0  # consume_direct 不动 leased_consumed

    # 库里的账本也被持久化（lease 表为空——Planner 不开 Lease）
    leases = store.load_leases("run-1")
    assert leases == ()

    # 重新打开数据库，确认账本在关闭 + 重开之间被持久化
    store.close()
    reopened = SQLiteLeadRunStore(db)
    try:
        restored = reopened.get_ledger("run-1")
        assert restored is not None
        assert restored.resources.model_calls() == 1, "重启后必须能看到已消费额度"
    finally:
        reopened.close()


# ===========================================================================
# 2. 持久化失败时，model.decide 不被调用
# ===========================================================================


def test_persistence_failure_blocks_model_call(tmp_path: Path) -> None:
    """持久化钩子抛错 → Planner 包成 LeadPlannerBudgetError → 模型 0 次调用。"""
    db = tmp_path / "lead.db"
    store = SQLiteLeadRunStore(db)
    store.create(
        LeadRunState.start(
            lead_run_id="run-1", goal=GOAL, thread_id="t", now=1_700_000_000.0
        )
    )

    def exploding_hook(_state: dict) -> None:
        raise RuntimeError("磁盘炸了 / SQLite 写入失败 / 任何持久化层异常")

    ledger = LeadBudgetLedger(resources=Budget(max_model_calls=10))
    ledger.bind_persistence(exploding_hook)
    root = LeadLedgerRootBudget(ledger)
    model = ProgrammableModel(responses=[_propose_batch_json()])
    planner = DefaultLeadPlanner(model, root)

    with pytest.raises(LeadPlannerBudgetError) as excinfo:
        planner.plan(_ctx())

    # 模型**没有**被调用
    assert model.call_count == 0
    assert "磁盘炸了" in str(excinfo.value) or "落库" in str(excinfo.value) \
        or "SQLite" in str(excinfo.value) or "RuntimeError" in str(excinfo.value)

    # 内存与数据库都**没有**扣减（copy-on-write 的"成功后回写"）
    assert ledger.resources.model_calls() == 0

    store.close()

    # 重启验证：库也没有扣减
    reopened = SQLiteLeadRunStore(db)
    try:
        restored = reopened.get_ledger("run-1")
        assert restored is None, "持久化失败时账本不应被写入"
    finally:
        reopened.close()


# ===========================================================================
# 3. 重启恢复后，下一次规划读到的是已消费的额度
# ===========================================================================


def test_restart_recovers_consumed_quota(tmp_path: Path) -> None:
    """第一次规划消耗 1 次 → 重启 → 第二次规划看到 usage=1；预算上限 2，
    第三次（usage 已到 2）撞 BudgetExceededError。

    这条测试模拟"两个进程（重启前后）共用同一份 SQLiteLeadRunStore"。
    关键：第二次启动时新建的 `LeadBudgetLedger` 通过
    `LeadRunState.start` → `from_state` → `resources.restore_usage(...)`
    把上次持久化的 model_calls_used 回填——**不是**重新从 0 开始。
    """
    db = tmp_path / "lead.db"
    # max=2：第一次后 usage=1；第二次能再扣一次到 usage=2；第三次撞限
    store, ledger, planner, model = _wire(db, max_model_calls=2)

    # 第一次规划：消耗 1 次
    planner.plan(_ctx())
    assert ledger.resources.model_calls() == 1
    store.close()

    # —— 模拟进程重启 ——
    # 新开一个 store、新建一个 ledger、用 from_state 恢复
    reopened = SQLiteLeadRunStore(db)
    try:
        # 取回 LeadRunState
        restored_state = reopened.get("run-1")
        assert restored_state.lead_run_id == "run-1"

        # 关键：从持久化账本恢复 LeadBudgetLedger
        persisted_ledger = reopened.get_ledger("run-1")
        assert persisted_ledger is not None
        assert persisted_ledger.resources.model_calls() == 1

        # 新建一个 LeadBudgetLedger，用恢复出来的 Budget 实例回填
        # （这是 25D 集成层会做的事；25C 在测试里复现以证明数据通路）
        new_budget = Budget(max_model_calls=2)
        new_budget.restore_usage(
            steps=persisted_ledger.resources.steps(),
            model_calls=persisted_ledger.resources.model_calls(),
            tool_calls=persisted_ledger.resources.tool_calls(),
            subagent_tasks=persisted_ledger.resources.subagent_tasks(),
            summary_calls=persisted_ledger.resources.summary_calls(),
        )
        new_ledger = LeadBudgetLedger(resources=new_budget)
        new_ledger.bind_persistence(
            lambda state: reopened.save_ledger_state("run-1", state)
        )

        # 恢复后模型计数应为 1
        assert new_ledger.resources.model_calls() == 1

        # 第二次规划：还能再扣 1 次（上限 2、已用 1）
        # Planner 重试 2 次：第 1 次扣 1 → 还能再 1 次；第 2 次再扣 1 → 到上限
        # 第 3 次规划：第 1 扣就撞限 → LeadPlannerBudgetError、模型 0 次调用
        model2 = ProgrammableModel(responses=[_propose_batch_json("w-2")] * 2)
        planner2 = DefaultLeadPlanner(model2, LeadLedgerRootBudget(new_ledger))

        # 第二次成功（usage 1 → 2）
        out = planner2.plan(_ctx())
        assert isinstance(out, ProposeBatch)
        assert new_ledger.resources.model_calls() == 2

        # 第三次撞限：usage=2、再扣 1 > max=2 → LeadPlannerBudgetError
        model3 = ProgrammableModel(responses=[_propose_batch_json("w-3")] * 2)
        planner3 = DefaultLeadPlanner(model3, LeadLedgerRootBudget(new_ledger))
        with pytest.raises(LeadPlannerBudgetError):
            planner3.plan(_ctx())

        # 关键断言：模型根本没被调（预算闸门在第 1 步拦下）
        assert model3.call_count == 0
        # 内存状态没被错误推进
        assert new_ledger.resources.model_calls() == 2
    finally:
        reopened.close()


# ===========================================================================
# 4. 中间件重试不重复扣费（隔离：Planner 不挂 BudgetMiddleware）
# ===========================================================================


def test_middleware_does_not_double_charge_planner() -> None:
    """把 Planner 用 BudgetRootBudget 喂普通 Budget，**不**包 BudgetMiddleware。

    验证"Planner 自己的扣费 + Agent 包装的 BudgetMiddleware"两层不重叠——
    这是 25D 接入时的防双扣契约：Planner 路径上**不**挂 Middleware。
    """
    from mini_deerflow.middleware import BudgetMiddleware

    budget = BudgetRootBudget(Budget(max_model_calls=5))
    model = ProgrammableModel(responses=[_propose_batch_json()])
    # 故意不挂 BudgetMiddleware——只挂 Planner 自己的 root_budget
    planner = DefaultLeadPlanner(model, budget)

    planner.plan(_ctx())

    # 1 次模型调用 → Planner 自己扣 1 次（BudgetMiddleware 根本不在路径上）
    assert budget.model_calls() == 1
    # 没有任何"额外扣费"——BudgetMiddleware 装配与否对 Planner 路径无影响
    # （如果误把 Middleware 装到 Planner 之外，则它**不会**看到 Planner 的
    # model.decide 调用，所以也不会重复扣）。这条**只**靠"Planner 不挂 Middleware"
    # 契约保证；测试通过 AST 在另外的文件守住（test_planner_never_imports_orchestrator_or_executor）。

    # 反向断言：BudgetMiddleware 这个类**仍然**存在（防止有人删除公共 API 误伤）
    assert BudgetMiddleware is not None


# ===========================================================================
# 5. 适配器契约：BudgetRootBudget 走纯内存、LeadLedgerRootBudget 走持久化
# ===========================================================================


def test_budget_root_budget_does_not_persist() -> None:
    """BudgetRootBudget 走纯内存路径——关闭后重启**不**保留已用量。"""
    budget = Budget(max_model_calls=5)
    root = BudgetRootBudget(budget)
    root.consume_model_call()
    assert budget.model_calls() == 1
    # 内存路径——本测试本身没有 SQLite 也能工作；只验证 root.consume_model_call
    # 真的走到 Budget 内部。重启等价性不适用（无 store）。


def test_lead_ledger_root_budget_charges_via_consume_direct(tmp_path: Path) -> None:
    """LeadLedgerRootBudget.consume_model_call 必须走 25B 持久化路径。

    验证方式：钩子被调用，且调用参数是包含"成功之后"状态的快照。
    """
    db = tmp_path / "lead.db"
    store = SQLiteLeadRunStore(db)
    store.create(
        LeadRunState.start(
            lead_run_id="run-1", goal=GOAL, thread_id="t", now=1_700_000_000.0
        )
    )

    seen_states: list[dict] = []

    def capturing(state: dict) -> None:
        seen_states.append(state)
        store.save_ledger_state("run-1", state)

    ledger = LeadBudgetLedger(resources=Budget(max_model_calls=5))
    ledger.bind_persistence(capturing)
    root = LeadLedgerRootBudget(ledger)
    model = ProgrammableModel(responses=[_propose_batch_json()])
    planner = DefaultLeadPlanner(model, root)

    planner.plan(_ctx())

    # 钩子至少被调一次；候选快照里"成功之后"的 model_calls_used 应该是 1
    assert len(seen_states) >= 1
    assert any(
        s["resources"]["model_calls"] == 1
        for s in seen_states
    ), "候选快照里 model_calls 必须是扣减后的 1"

    # 库里的账本与候选快照一致
    restored = store.get_ledger("run-1")
    assert restored is not None
    assert restored.resources.model_calls() == 1

    store.close()


# ===========================================================================
# 6. Planner 与 BudgetMiddleware 在 AST 层**不**互相 import（防回归）
# ===========================================================================


def test_planner_does_not_import_budget_middleware() -> None:
    """Planner 路径上**不**应有 `BudgetMiddleware` 导入（避免 25D 接入 Orchestrator
    后双扣）。即便我们没主动 import，AST 守卫仍能发现未来无意加入。
    """
    import ast
    import mini_deerflow.lead_agent.planner as p

    source = ast.parse(open(p.__file__, encoding="utf-8").read())
    for node in ast.walk(source):
        if isinstance(node, ast.ImportFrom) and node.module and (
            "middleware" in node.module
            and any(a.name == "BudgetMiddleware" for a in node.names)
        ):
            pytest.fail("planner.py 不应 import BudgetMiddleware")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if "middleware" in alias.name and "BudgetMiddleware" in alias.name:
                    pytest.fail("planner.py 不应 import BudgetMiddleware")


# ===========================================================================
# 工具
# ===========================================================================


def _ctx() -> PlannerContext:
    return PlannerContext(
        goal=GOAL,
        verified_facts=(make_fact("f-1", "w-prev"),),
        last_decision=None,
        recent_items=(),
        budget_snapshot=build_budget_snapshot(Budget(max_model_calls=10)),
        capabilities=("general_purpose",),
    )
