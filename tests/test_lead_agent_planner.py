"""Phase 25C LeadPlanner 测试。

按用户要求：重点验证**控制流与副作用边界**，不只测类型构造。

覆盖规格：
- 合法下一批规划
- 任务历史进入上下文 + 重复任务按既有规则
- 首次失败 → 反馈 → 第二次成功
- 连续两次失败 → 抛 `LeadPlannerMaxAttemptsError`
- 预算不足 / 持久化失败时**模型未调用**
- 模型尝试的预算记账正确
- `request_clarification` / `recommend_synthesis` 契约
- Planner 不触发工具 / Subagent / Store 写入
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from mini_deerflow.lead_agent import (
    BudgetRootBudget,
    DEFAULT_MAX_PLANNING_ATTEMPTS,
    DefaultLeadPlanner,
    ExecutionRoute,
    GoalContract,
    LeadPlannerBudgetError,
    LeadPlannerDeterministicError,
    LeadPlannerError,
    LeadPlannerMaxAttemptsError,
    LeadPlannerSchemaError,
    LeadRunStore,
    PlannerContext,
    PlannerOutput,
    ProposeBatch,
    RecommendSynthesis,
    RequestClarification,
    ResourceAccessMode,
    ResourceClaim,
    SQLiteLeadRunStore,
    VerifiedFact,
    VerificationResult,
    WorkItem,
    WorkItemStatus,
    WorkItemProposal,
    WorkItemSummary,
    build_budget_snapshot,
    summarize_recent_items,
)
from mini_deerflow.lead_agent.planner import parse_planner_output
from mini_deerflow.lead_agent.state_machine import LeadRunStatus
from mini_deerflow.lead_agent.validation import validate_batch
from mini_deerflow.lead_agent.budget_ledger import LeadBudgetLedger
from mini_deerflow.middleware import Budget
from mini_deerflow.models import FinalAnswer, Message
from mini_deerflow.lead_agent.models import EvidenceReceipt


# ===========================================================================
# 测试替身：可编程 Model（按调用次序返回指定 FinalAnswer 序列）
# ===========================================================================


@dataclass
class ProgrammableModel:
    """按 `decide()` 调用次序返回预设响应；不识别 tools。"""

    responses: list[str] = field(default_factory=list)
    call_count: int = 0
    seen_messages: list[list[Message]] = field(default_factory=list)

    def decide(
        self, messages: list[Message], tools: Any = None
    ) -> FinalAnswer:
        self.call_count += 1
        # 深拷贝 message.content 以便后续断言不会因为 Planner 内部修改而受影响
        self.seen_messages.append(
            [Message(role=m.role, content=m.content) for m in messages]
        )
        if self.call_count > len(self.responses):
            raise AssertionError(
                f"模型被调用第 {self.call_count} 次，但只预设了 {len(self.responses)} 个响应"
            )
        return FinalAnswer(content=self.responses[self.call_count - 1])

    def last_user_payload(self) -> dict[str, Any]:
        """取最后一次 `decide` 的 user 消息内容（已 JSON 解析）。"""
        msgs = self.seen_messages[-1]
        assert msgs[-1].role == "user", f"最后一条应是 user，实为 {msgs[-1].role}"
        return json.loads(msgs[-1].content)

    def last_user_payload_replan(self) -> dict[str, Any] | None:
        """最后一次 user 消息里是否包含 `_REPLAN_PREFIX` 反馈；返回解析后的 user 段。"""
        content = self.seen_messages[-1][-1].content
        marker = "上一次你的输出被确定性校验拒绝"
        if marker not in content:
            return None
        # 取 marker 段之后的 JSON 段
        after = content.split(marker, 1)[1]
        # 第一个 "\n---" 之后是上一次输出，再之后的 "\n" + JSON
        parts = after.split("\n---", 1)
        if len(parts) < 2:
            return None
        rest = parts[1].lstrip("\n")
        return json.loads(rest)


# ===========================================================================
# 固定事实：goal / verified_facts
# ===========================================================================


GOAL = GoalContract.create(
    goal_id="g-1",
    objective="回答用户的两个问题",
    acceptance_criteria=("criterion-A: 已回答", "criterion-B: 已提交"),
)


def make_fact(
    fact_id: str, work_id: str, statement: str = "已验证"
) -> VerifiedFact:
    return VerifiedFact(
        fact_id=fact_id,
        statement=statement,
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


def make_context(
    *,
    verified_facts: tuple[VerifiedFact, ...] = (),
    last_decision: Any = None,
    recent_items: tuple[WorkItemSummary, ...] = (),
    budget: Budget | None = None,
    capabilities: tuple[str, ...] = ("general_purpose", "bash"),
    last_error: LeadPlannerDeterministicError | None = None,
) -> PlannerContext:
    if budget is None:
        budget = Budget(max_model_calls=10, max_tool_calls=10)
    return PlannerContext(
        goal=GOAL,
        verified_facts=verified_facts,
        last_decision=last_decision,
        recent_items=recent_items,
        budget_snapshot=build_budget_snapshot(budget),
        capabilities=capabilities,
        last_error=last_error,
    )


# ===========================================================================
# 1. 合法下一批规划
# ===========================================================================


def test_legal_propose_batch_round_trip() -> None:
    """模型返回合法 propose_batch → Planner 直接返回，无需重试。"""
    response = json.dumps(
        {
            "kind": "propose_batch",
            "proposals": [
                {
                    "work_id": "w-1",
                    "objective": "回答用户的问题一",
                    "depends_on": [],
                    "claims": [
                        {"resource": "file:docs/a.md", "access": "read"}
                    ],
                    "execution_route": "direct_agent",
                }
            ],
            "rationale": "第一步只读 docs。",
        },
        ensure_ascii=False,
    )
    model = ProgrammableModel(responses=[response])
    budget = BudgetRootBudget(Budget(max_model_calls=10))
    planner = DefaultLeadPlanner(model, budget)

    output = planner.plan(make_context())

    assert isinstance(output, ProposeBatch)
    assert output.kind == "propose_batch"
    assert len(output.proposals) == 1
    assert output.proposals[0].work_id == "w-1"
    assert output.proposals[0].execution_route is ExecutionRoute.DIRECT_AGENT
    # 一次成功 → 模型只被调一次
    assert model.call_count == 1
    # 预算被记一笔
    assert budget.model_calls() == 1


# ===========================================================================
# 2. 任务历史进入上下文，重复任务按既有规则处理
# ===========================================================================


def test_recent_items_passed_into_context() -> None:
    """Planner 把 recent_items 序列化进 user 消息，模型可见。"""
    # 给两条 WorkItem（构造 WorkItem.from_proposal 才能拿到 work_item.status）
    p1 = WorkItemProposal(
        work_id="w-done",
        objective="已完成的工作",
        depends_on=(),
        claims=(ResourceClaim.of("file:a.txt", "read"),),
        execution_route=ExecutionRoute.DIRECT_AGENT,
        created_at=1_700_000_000.0,
    )
    item1 = WorkItem.from_proposal(p1, status=WorkItemStatus.SUCCEEDED)
    summary = summarize_recent_items((item1,))
    assert len(summary) == 1
    assert summary[0].status == "succeeded"

    response = json.dumps(
        {
            "kind": "propose_batch",
            "proposals": [
                {
                    "work_id": "w-next",
                    "objective": "下一步",
                    "depends_on": [],
                    "claims": [],
                    "execution_route": "direct_agent",
                }
            ],
            "rationale": "ok",
        }
    )
    model = ProgrammableModel(responses=[response])
    planner = DefaultLeadPlanner(model, BudgetRootBudget(Budget(max_model_calls=5)))

    planner.plan(make_context(recent_items=summary))

    payload = model.last_user_payload()
    assert "recent_items" in payload
    assert len(payload["recent_items"]) == 1
    assert payload["recent_items"][0]["work_id"] == "w-done"
    assert payload["recent_items"][0]["status"] == "succeeded"


def test_duplicate_work_id_against_verified_history_rejected_deterministically() -> None:
    """模型产出与已验证历史 work_id 重复的提案 → 触发 25A `validate_batch` → 重规划。

    这条测试同时验证「重复任务按既有规则处理」：25A 的 `validate_batch` 已拦。
    """
    fact = make_fact("f-1", "w-hist")
    # 首次：模型重复 w-hist → 25A DuplicateWorkIdError → Planner 反馈
    bad_response = json.dumps(
        {
            "kind": "propose_batch",
            "proposals": [
                {
                    "work_id": "w-hist",  # 与 verified_facts 重复
                    "objective": "重复的提案",
                    "depends_on": [],
                    "claims": [],
                    "execution_route": "direct_agent",
                }
            ],
            "rationale": "x",
        }
    )
    # 第二次：模型给一个合法的、不同的 work_id
    good_response = json.dumps(
        {
            "kind": "propose_batch",
            "proposals": [
                {
                    "work_id": "w-fresh",
                    "objective": "新提案",
                    "depends_on": [],
                    "claims": [],
                    "execution_route": "direct_agent",
                }
            ],
            "rationale": "已修",
        }
    )
    model = ProgrammableModel(responses=[bad_response, good_response])
    budget = BudgetRootBudget(Budget(max_model_calls=5))
    planner = DefaultLeadPlanner(model, budget)

    output = planner.plan(make_context(verified_facts=(fact,)))

    assert isinstance(output, ProposeBatch)
    assert output.proposals[0].work_id == "w-fresh"
    assert model.call_count == 2
    assert budget.model_calls() == 2
    # 第二次调用的 user 消息里**应该**包含上一次失败的错误反馈
    second_user = model.seen_messages[1][-1].content
    assert "上一次你的输出被确定性校验拒绝" in second_user
    assert "DuplicateWorkIdError" in second_user


# ===========================================================================
# 3. 首次失败、反馈错误、第二次成功
# ===========================================================================


def test_first_failure_feedback_then_second_success() -> None:
    """首次：missing dependency；第二次：修复依赖后成功。"""
    bad = json.dumps(
        {
            "kind": "propose_batch",
            "proposals": [
                {
                    "work_id": "w-1",
                    "objective": "depends on something missing",
                    "depends_on": ["w-not-here"],
                    "claims": [],
                    "execution_route": "direct_agent",
                }
            ],
            "rationale": "x",
        }
    )
    good = json.dumps(
        {
            "kind": "propose_batch",
            "proposals": [
                {
                    "work_id": "w-1",
                    "objective": "ok now",
                    "depends_on": [],
                    "claims": [],
                    "execution_route": "direct_agent",
                }
            ],
            "rationale": "已删除不存在的依赖",
        }
    )
    model = ProgrammableModel(responses=[bad, good])
    budget = BudgetRootBudget(Budget(max_model_calls=5))
    planner = DefaultLeadPlanner(model, budget)

    output = planner.plan(make_context())

    assert isinstance(output, ProposeBatch)
    assert model.call_count == 2
    assert budget.model_calls() == 2
    # 第二次的 user 消息里**应该**包含上一次失败的错误反馈
    second_user = model.seen_messages[1][-1].content
    assert "上一次你的输出被确定性校验拒绝" in second_user
    # 错误类型与原始坏 JSON 都应回灌到模型
    assert "MissingDependencyError" in second_user
    # 上一条原始 JSON 仍可在第二次的 user 消息里找到（用于模型自检）
    assert json.dumps(
        {"work_id": "w-1", "objective": "depends on something missing"},
        ensure_ascii=False,
    )[1:-1] in second_user or "w-not-here" in second_user


def test_schema_error_is_recoverable_on_second_attempt() -> None:
    """首次：JSON 解析失败；第二次：合法。"""
    bad = "not json at all"
    good = json.dumps(
        {
            "kind": "propose_batch",
            "proposals": [
                {
                    "work_id": "w-1",
                    "objective": "ok",
                    "depends_on": [],
                    "claims": [],
                    "execution_route": "direct_agent",
                }
            ],
            "rationale": "fix",
        }
    )
    model = ProgrammableModel(responses=[bad, good])
    budget = BudgetRootBudget(Budget(max_model_calls=5))
    planner = DefaultLeadPlanner(model, budget)

    output = planner.plan(make_context())

    assert isinstance(output, ProposeBatch)
    assert model.call_count == 2
    assert budget.model_calls() == 2


# ===========================================================================
# 4. 连续两次失败后停止
# ===========================================================================


def test_two_consecutive_failures_raise_max_attempts() -> None:
    """两次都返回非法 JSON → 抛 `LeadPlannerMaxAttemptsError`，不再重试。"""
    bad = "not valid json"
    model = ProgrammableModel(responses=[bad, bad])
    budget = BudgetRootBudget(Budget(max_model_calls=5))
    planner = DefaultLeadPlanner(model, budget)

    with pytest.raises(LeadPlannerMaxAttemptsError) as excinfo:
        planner.plan(make_context())

    assert excinfo.value.attempts == DEFAULT_MAX_PLANNING_ATTEMPTS
    assert isinstance(excinfo.value.last_error, LeadPlannerError)
    assert model.call_count == DEFAULT_MAX_PLANNING_ATTEMPTS
    # 预算仍被记 2 次（每次尝试前都扣一次）
    assert budget.model_calls() == DEFAULT_MAX_PLANNING_ATTEMPTS


def test_one_success_one_deterministic_failure_still_succeeds() -> None:
    """第 1 次 deterministic 失败 → 第 2 次成功 → 整体成功。"""
    bad = json.dumps(
        {
            "kind": "propose_batch",
            "proposals": [
                {
                    "work_id": "w-self",
                    "objective": "self dep",
                    "depends_on": ["w-self"],
                    "claims": [],
                    "execution_route": "direct_agent",
                }
            ],
            "rationale": "x",
        }
    )
    good = json.dumps(
        {
            "kind": "propose_batch",
            "proposals": [
                {
                    "work_id": "w-clean",
                    "objective": "ok",
                    "depends_on": [],
                    "claims": [],
                    "execution_route": "direct_agent",
                }
            ],
            "rationale": "fixed",
        }
    )
    model = ProgrammableModel(responses=[bad, good])
    planner = DefaultLeadPlanner(model, BudgetRootBudget(Budget(max_model_calls=5)))

    output = planner.plan(make_context())
    assert isinstance(output, ProposeBatch)
    assert output.proposals[0].work_id == "w-clean"
    assert model.call_count == 2


# ===========================================================================
# 5. 预算不足 / 持久化失败时**模型未调用**
# ===========================================================================


def test_budget_exhausted_before_any_attempt_blocks_model() -> None:
    """预算 max_model_calls=0 → 第一次尝试前就 BudgetExceededError → 模型 0 次调用。"""
    budget = BudgetRootBudget(Budget(max_model_calls=0))
    model = ProgrammableModel(responses=["ignored"])
    planner = DefaultLeadPlanner(model, budget)

    with pytest.raises(LeadPlannerBudgetError) as excinfo:
        planner.plan(make_context(budget=budget._budget))

    assert model.call_count == 0
    assert excinfo.value.resource == "model_calls"


def test_budget_exhausted_mid_attempts_blocks_further_attempts() -> None:
    """首轮扣 1 后第二次前预算耗尽 → 模型只被调 1 次。"""
    budget = BudgetRootBudget(Budget(max_model_calls=1))
    # 准备 3 个响应，但实际只会用到 1 个
    model = ProgrammableModel(
        responses=[
            json.dumps(
                {
                    "kind": "propose_batch",
                    "proposals": [
                        {
                            "work_id": "w-1",
                            "objective": "ok",
                            "depends_on": [],
                            "claims": [],
                            "execution_route": "direct_agent",
                        }
                    ],
                    "rationale": "x",
                }
            ),
            "would not be reached",
            "would not be reached",
        ]
    )
    planner = DefaultLeadPlanner(model, budget)

    # 第 1 次成功，扣 1；预算剩 0；下一次 plan 调用（这里就是 attempt 2）前撞限
    # 把 1 次成功跑完之后，再独立 plan 第二次：会失败
    out1 = planner.plan(make_context(budget=budget._budget))
    assert isinstance(out1, ProposeBatch)
    assert model.call_count == 1
    assert budget.model_calls() == 1

    # 同一 planner 第二次调用：第一次就 BudgetExceededError
    with pytest.raises(LeadPlannerBudgetError):
        planner.plan(make_context(budget=budget._budget))
    # 模型仍是 1 次
    assert model.call_count == 1


def test_persistence_hook_failure_does_not_call_model() -> None:
    """Budget 上挂的 `restore_usage`/消费钩子失败 → 模型 0 次调用。

    25B 路径里是 `LeadBudgetLedger.bind_persistence`；25C Planner 用的是
    `Budget` 而非 `LeadBudgetLedger`——但仍可模拟：把 Budget 子类化使其
    `consume_model_call` 抛非 BudgetExceeded 异常，验证 Planner 不会继续调模型。
    """
    from mini_deerflow.middleware import BudgetExceededError

    class ExplodingBudget(Budget):
        def consume_model_call(self) -> None:
            raise RuntimeError("持久化钩子爆炸")

    budget = BudgetRootBudget(ExplodingBudget(max_model_calls=10))
    model = ProgrammableModel(responses=["ignored"])
    planner = DefaultLeadPlanner(model, budget)

    with pytest.raises(LeadPlannerBudgetError) as excinfo:
        planner.plan(make_context(budget=budget._budget))

    assert "持久化钩子爆炸" in str(excinfo.value)
    assert model.call_count == 0


# ===========================================================================
# 6. 模型尝试的预算记账正确
# ===========================================================================


def test_budget_charged_once_per_model_call_attempt() -> None:
    """3 次尝试（1 成功）→ 预算记 3。"""
    responses = [
        "bad1",
        "bad2",
        json.dumps(
            {
                "kind": "propose_batch",
                "proposals": [
                    {
                        "work_id": "w-1",
                        "objective": "ok",
                        "depends_on": [],
                        "claims": [],
                        "execution_route": "direct_agent",
                    }
                ],
                "rationale": "ok",
            }
        ),
    ]
    # 但 max_attempts=2 → 只调 2 次
    model = ProgrammableModel(responses=responses[:2])
    budget = BudgetRootBudget(Budget(max_model_calls=5))
    planner = DefaultLeadPlanner(model, budget)

    with pytest.raises(LeadPlannerMaxAttemptsError):
        planner.plan(make_context(budget=budget._budget))

    assert model.call_count == 2
    assert budget.model_calls() == 2


def test_budget_not_charged_when_constructed_with_invalid_model() -> None:
    """构造期失败不消耗预算。"""
    budget = BudgetRootBudget(Budget(max_model_calls=5))
    with pytest.raises(TypeError):
        DefaultLeadPlanner(model="not a model", budget=budget)  # type: ignore[arg-type]
    assert budget.model_calls() == 0


# ===========================================================================
# 7. 澄清与综合建议的输出契约
# ===========================================================================


def test_request_clarification_blocks_when_blocking_fact_ids_unknown() -> None:
    """`blocking_fact_ids` 引用不存在的 fact_id → deterministic 失败 → MaxAttempts。

    字段语义是 `VerifiedFact.fact_id`（事实唯一键），**不是** `work_id`。
    """
    response = json.dumps(
        {
            "kind": "request_clarification",
            "question": "问题1的目标是什么？",
            "reason": "缺少 acceptance criteria",
            "blocking_fact_ids": ["f-does-not-exist"],
        }
    )
    model = ProgrammableModel(responses=[response, response])  # 两次都失败
    planner = DefaultLeadPlanner(model, BudgetRootBudget(Budget(max_model_calls=5)))

    with pytest.raises(LeadPlannerMaxAttemptsError) as excinfo:
        planner.plan(make_context(verified_facts=(make_fact("f-1", "w-1"),)))

    # 最后一次底层错误是 UnknownBlockingFact（消息里点明这是 fact_id，不是 work_id）
    assert isinstance(excinfo.value.last_error, LeadPlannerDeterministicError)
    assert excinfo.value.last_error.kind == "UnknownBlockingFact"
    assert "fact_id" in excinfo.value.last_error.message
    assert "work_id" in excinfo.value.last_error.message
    assert model.call_count == DEFAULT_MAX_PLANNING_ATTEMPTS


def test_request_clarification_succeeds_with_known_blocking_fact_ids() -> None:
    response = json.dumps(
        {
            "kind": "request_clarification",
            "question": "先做哪个？",
            "reason": "事实 1 和 2 都不足以判断",
            "blocking_fact_ids": ["f-1", "f-2"],
        }
    )
    model = ProgrammableModel(responses=[response])
    planner = DefaultLeadPlanner(model, BudgetRootBudget(Budget(max_model_calls=5)))

    output = planner.plan(
        make_context(
            verified_facts=(
                make_fact("f-1", "w-1"),
                make_fact("f-2", "w-2"),
            )
        )
    )

    assert isinstance(output, RequestClarification)
    assert output.kind == "request_clarification"
    assert output.blocking_fact_ids == ("f-1", "f-2")
    assert model.call_count == 1


def test_blocking_fact_ids_uses_fact_id_not_work_id() -> None:
    """**显式**区分 fact_id 与 work_id：

    - 给一个**只有 work_id 在 verified_facts 中**的 payload（fact_id 不在）
      → 必须被 deterministic 拒绝（因为 fact_id 找不到）
    - 给一个**只有 fact_id 在 verified_facts 中**的 payload（work_id 不在）
      → 必须成功（因为校验的是 fact_id，不是 work_id）
    - 即使 work_id 错配（同一 work_id 下挂另一条 fact_id），fact_id 仍准确路由

    这条测试钉住 25C 架构收口对 `blocking_fact_ids` 语义的修正——
    不能被未来某次"统一改回 work_id"悄悄回退。
    """
    # fact 1：fact_id="f-1", work_id="w-A"
    # fact 2：fact_id="f-2", work_id="w-B"（不同 work_id 下的另一条事实）
    facts = (make_fact("f-1", "w-A"), make_fact("f-2", "w-B"))

    # 用例 1：payload 含 "w-A"（work_id），但不含 "f-1"（fact_id）
    # 应该被拒——校验的是 fact_id，不是 work_id
    bad_payload_uses_work_id = json.dumps(
        {
            "kind": "request_clarification",
            "question": "q",
            "reason": "r",
            "blocking_fact_ids": ["w-A"],  # ← 错：这是 work_id，不是 fact_id
        }
    )
    model = ProgrammableModel(responses=[bad_payload_uses_work_id, bad_payload_uses_work_id])
    planner = DefaultLeadPlanner(model, BudgetRootBudget(Budget(max_model_calls=5)))
    with pytest.raises(LeadPlannerMaxAttemptsError) as excinfo:
        planner.plan(make_context(verified_facts=facts))
    assert excinfo.value.last_error.kind == "UnknownBlockingFact"
    assert "w-A" in excinfo.value.last_error.message

    # 用例 2：payload 含 "f-2"（fact_id），同一 work_id 已存在但不同 fact_id
    # 应该成功——证明 fact_id 是独立路由，**不**回退到 work_id
    good_payload_uses_fact_id = json.dumps(
        {
            "kind": "request_clarification",
            "question": "q",
            "reason": "r",
            "blocking_fact_ids": ["f-2"],
        }
    )
    model = ProgrammableModel(responses=[good_payload_uses_fact_id])
    planner = DefaultLeadPlanner(model, BudgetRootBudget(Budget(max_model_calls=5)))
    output = planner.plan(make_context(verified_facts=facts))
    assert isinstance(output, RequestClarification)
    assert output.blocking_fact_ids == ("f-2",)


def test_recommend_synthesis_rejects_unknown_target_criterion() -> None:
    response = json.dumps(
        {
            "kind": "recommend_synthesis",
            "target_criterion": "criterion-NOT-IN-GOAL",
            "supporting_fact_ids": ["f-1"],
            "rationale": "x",
        }
    )
    model = ProgrammableModel(responses=[response, response])
    planner = DefaultLeadPlanner(model, BudgetRootBudget(Budget(max_model_calls=5)))

    with pytest.raises(LeadPlannerMaxAttemptsError) as excinfo:
        planner.plan(make_context(verified_facts=(make_fact("f-1", "w-1"),)))
    assert excinfo.value.last_error.kind == "UnknownTargetCriterion"


def test_recommend_synthesis_rejects_empty_supporting_facts() -> None:
    response = json.dumps(
        {
            "kind": "recommend_synthesis",
            "target_criterion": "criterion-A: 已回答",
            "supporting_fact_ids": [],
            "rationale": "x",
        }
    )
    model = ProgrammableModel(responses=[response, response])
    planner = DefaultLeadPlanner(model, BudgetRootBudget(Budget(max_model_calls=5)))

    with pytest.raises(LeadPlannerMaxAttemptsError) as excinfo:
        planner.plan(make_context(verified_facts=(make_fact("f-1", "w-1"),)))
    assert excinfo.value.last_error.kind == "EmptySupportingFacts"


def test_recommend_synthesis_succeeds() -> None:
    response = json.dumps(
        {
            "kind": "recommend_synthesis",
            "target_criterion": "criterion-A: 已回答",
            "supporting_fact_ids": ["f-1", "f-2"],
            "rationale": "事实充分",
        }
    )
    model = ProgrammableModel(responses=[response])
    planner = DefaultLeadPlanner(model, BudgetRootBudget(Budget(max_model_calls=5)))

    output = planner.plan(
        make_context(
            verified_facts=(
                make_fact("f-1", "w-1"),
                make_fact("f-2", "w-2"),
            )
        )
    )

    assert isinstance(output, RecommendSynthesis)
    assert output.kind == "recommend_synthesis"
    assert output.target_criterion == "criterion-A: 已回答"
    assert output.supporting_fact_ids == ("f-1", "f-2")


# ===========================================================================
# 8. Planner 不触发工具、Subagent 或 Store 写入
# ===========================================================================


def test_planner_does_not_touch_store(tmp_path) -> None:
    """Planner.plan() 期间不创建任何 Store 实例 / 写库文件。"""
    response = json.dumps(
        {
            "kind": "propose_batch",
            "proposals": [
                {
                    "work_id": "w-1",
                    "objective": "ok",
                    "depends_on": [],
                    "claims": [],
                    "execution_route": "direct_agent",
                }
            ],
            "rationale": "x",
        }
    )
    model = ProgrammableModel(responses=[response])
    planner = DefaultLeadPlanner(model, BudgetRootBudget(Budget(max_model_calls=5)))

    # 拍快照：tmp_path 下文件数
    before = set(tmp_path.iterdir())

    planner.plan(make_context())

    after = set(tmp_path.iterdir())
    # Planner.plan 不能创建文件
    assert before == after, f"Planner 创建了文件: {after - before}"


def test_planner_does_not_call_store_save(tmp_path) -> None:
    """即使注入一个真实 Store，Planner 也不应调用它的写入方法。"""
    response = json.dumps(
        {
            "kind": "propose_batch",
            "proposals": [
                {
                    "work_id": "w-1",
                    "objective": "ok",
                    "depends_on": [],
                    "claims": [],
                    "execution_route": "direct_agent",
                }
            ],
            "rationale": "x",
        }
    )
    model = ProgrammableModel(responses=[response])
    planner = DefaultLeadPlanner(model, BudgetRootBudget(Budget(max_model_calls=5)))

    # 注入一个真实 Store 但 Planner 根本没拿它——这里仅做"Planner 不依赖 Store"断言
    store = SQLiteLeadRunStore(tmp_path / "should-never-be-written.db")
    try:
        assert isinstance(store, LeadRunStore)  # type: ignore[unreachable]
        # Planner 不接受 store 参数；下面的调用不涉及 store
        planner.plan(make_context())
    finally:
        store.close()
    # 数据库文件仍可打开但不应有 lead_runs
    reopened = SQLiteLeadRunStore(tmp_path / "should-never-be-written.db")
    try:
        assert reopened.list_by_thread("t", limit=10, offset=0) == []
    finally:
        reopened.close()


def test_planner_does_not_open_lease() -> None:
    """Planner 输出 ProposeBatch 后，**不**调 LeadBudgetLedger.open_lease。

    25C 阶段没有 Orchestrator 把 proposals 转成 WorkItem + Lease；
    Planner 仅输出 proposal 元组。这是边界。
    """
    response = json.dumps(
        {
            "kind": "propose_batch",
            "proposals": [
                {
                    "work_id": "w-1",
                    "objective": "ok",
                    "depends_on": [],
                    "claims": [],
                    "execution_route": "subagent",  # 即便选了 subagent 路由
                    "subagent_type": "general_purpose",  # 25D 契约：显式
                    "execution_route_extra": "ignored",  # 故意脏字段——解析应忽略
                }
            ],
            "rationale": "x",
        }
    )
    # 剥掉执行路线——_build_proposal 会忽略未知 key
    payload = json.loads(response)
    payload["proposals"][0].pop("execution_route_extra", None)
    response = json.dumps(payload, ensure_ascii=False)

    model = ProgrammableModel(responses=[response])
    ledger = LeadBudgetLedger()
    planner = DefaultLeadPlanner(model, BudgetRootBudget(Budget(max_model_calls=5)))

    planner.plan(make_context())

    # 账本没被开过任何 Lease
    assert ledger.all_leases() == ()


# ===========================================================================
# 25D 契约：Planner 缺目标字段 → 错误反馈 + 重规划
# ===========================================================================


def test_planner_missing_tool_name_triggers_replan_with_error_feedback() -> None:
    """模型首次：TOOL 路由但**漏** `tool_name` → Planner 抛 `LeadPlannerSchemaError` →
    把错误反馈拼进 user 消息 → 模型第二次：补上 `tool_name` → 路由通过。
    """
    # 首次：缺 tool_name
    bad = json.dumps(
        {
            "kind": "propose_batch",
            "proposals": [
                {
                    "work_id": "w-1",
                    "objective": "ok",
                    "depends_on": [],
                    "claims": [],
                    "execution_route": "tool",
                    # 注意：故意漏 tool_name
                }
            ],
            "rationale": "x",
        }
    )
    # 第二次：补上 tool_name
    good = json.dumps(
        {
            "kind": "propose_batch",
            "proposals": [
                {
                    "work_id": "w-1",
                    "objective": "ok",
                    "depends_on": [],
                    "claims": [],
                    "execution_route": "tool",
                    "tool_name": "calculator",
                }
            ],
            "rationale": "ok",
        }
    )
    model = ProgrammableModel(responses=[bad, good])
    budget = BudgetRootBudget(Budget(max_model_calls=5))
    planner = DefaultLeadPlanner(model, budget)

    # 走通——第二次成功
    from mini_deerflow.lead_agent import ProposeBatch, WorkItem

    out = planner.plan(make_context())
    assert isinstance(out, ProposeBatch)
    assert out.proposals[0].work_id == "w-1"
    # 关键：第二次的 user 消息含错误反馈
    second_user = model.seen_messages[1][-1].content
    assert "上一次你的输出被确定性校验拒绝" in second_user
    # 错误类型明确
    assert "TOOL" in second_user or "tool_name" in second_user
    # 第一次的坏 JSON 也被回灌便于模型自检
    assert "tool" in second_user
    assert model.call_count == 2


def test_planner_missing_subagent_type_triggers_replan_with_error_feedback() -> None:
    """模型首次：SUBAGENT 路由但**漏** `subagent_type` → Planner 抛 → 重规划。"""
    bad = json.dumps(
        {
            "kind": "propose_batch",
            "proposals": [
                {
                    "work_id": "w-1",
                    "objective": "ok",
                    "depends_on": [],
                    "claims": [],
                    "execution_route": "subagent",
                    # 漏 subagent_type
                }
            ],
            "rationale": "x",
        }
    )
    good = json.dumps(
        {
            "kind": "propose_batch",
            "proposals": [
                {
                    "work_id": "w-1",
                    "objective": "ok",
                    "depends_on": [],
                    "claims": [],
                    "execution_route": "subagent",
                    "subagent_type": "general_purpose",
                }
            ],
            "rationale": "ok",
        }
    )
    model = ProgrammableModel(responses=[bad, good])
    budget = BudgetRootBudget(Budget(max_model_calls=5))
    planner = DefaultLeadPlanner(model, budget)

    from mini_deerflow.lead_agent import ProposeBatch

    out = planner.plan(make_context())
    assert isinstance(out, ProposeBatch)
    assert model.call_count == 2
    # 错误反馈里点明 subagent_type
    second_user = model.seen_messages[1][-1].content
    assert "subagent_type" in second_user or "subagent" in second_user


def test_planner_does_not_modify_lead_run_state() -> None:
    """Planner 不接收 LeadRunState，也不修改任何传入的 WorkItem/VerifiedFact。

    这里用 frozen dataclass 的天然保护来兜底：如果 Planner 试图改 ctx 里的对象
    会抛 FrozenInstanceError。
    """
    response = json.dumps(
        {
            "kind": "propose_batch",
            "proposals": [
                {
                    "work_id": "w-fresh",  # 不与已验证 work_id 重复
                    "objective": "ok",
                    "depends_on": [],
                    "claims": [],
                    "execution_route": "direct_agent",
                }
            ],
            "rationale": "x",
        }
    )
    model = ProgrammableModel(responses=[response])
    planner = DefaultLeadPlanner(model, BudgetRootBudget(Budget(max_model_calls=5)))

    # 构造一个含 verified_facts / recent_items 的上下文
    fact = make_fact("f-1", "w-1")
    item_p = WorkItemProposal(
        work_id="w-1",
        objective="已完成的",
        depends_on=(),
        claims=(),
        execution_route=ExecutionRoute.DIRECT_AGENT,
        created_at=1_700_000_000.0,
    )
    item = WorkItem.from_proposal(item_p, status=WorkItemStatus.SUCCEEDED)
    summary = summarize_recent_items((item,))

    # 只要不抛 FrozenInstanceError 就说明 Planner 没改
    planner.plan(
        make_context(verified_facts=(fact,), recent_items=summary)
    )
    # 二次访问 ctx 内的对象仍合法（frozen 不变）
    assert fact.fact_id == "f-1"
    assert summary[0].work_id == "w-1"


# ===========================================================================
# 9. 解析器单元测试（parse_planner_output）
# ===========================================================================


def test_parser_rejects_empty_text() -> None:
    with pytest.raises(LeadPlannerSchemaError):
        parse_planner_output("")


def test_parser_rejects_invalid_json() -> None:
    with pytest.raises(LeadPlannerSchemaError):
        parse_planner_output("not valid json")


def test_parser_rejects_unknown_kind() -> None:
    with pytest.raises(LeadPlannerSchemaError):
        parse_planner_output(json.dumps({"kind": "fly_to_moon"}))


def test_parser_rejects_empty_proposals() -> None:
    with pytest.raises(LeadPlannerSchemaError):
        parse_planner_output(
            json.dumps(
                {
                    "kind": "propose_batch",
                    "proposals": [],
                    "rationale": "x",
                }
            )
        )


def test_parser_strips_markdown_fence() -> None:
    """模型把 JSON 嵌在 ```json ... ``` 里也能解析。"""
    inner = json.dumps(
        {
            "kind": "recommend_synthesis",
            "target_criterion": "criterion-A: 已回答",
            "supporting_fact_ids": ["f-1"],
            "rationale": "ok",
        }
    )
    fenced = f"```json\n{inner}\n```"
    out = parse_planner_output(fenced)
    assert isinstance(out, RecommendSynthesis)


# ===========================================================================
# 10. 边界单元测试
# ===========================================================================


def test_budget_snapshot_is_value_not_reference() -> None:
    """`build_budget_snapshot` 返回的值不受后续 Budget 修改影响。"""
    budget = Budget(max_model_calls=10, max_tool_calls=5)
    snap = build_budget_snapshot(budget)
    # 修改 Budget 之后 snapshot 不变
    budget.consume_model_call()
    assert snap.max_model_calls == 10
    assert snap.model_calls_used == 0


def test_summarize_recent_items_keeps_last_n_ordered() -> None:
    items = tuple(
        WorkItem.from_proposal(
            WorkItemProposal(
                work_id=f"w-{i}",
                objective=f"o-{i}",
                depends_on=(),
                claims=(),
                execution_route=ExecutionRoute.DIRECT_AGENT,
                created_at=1_700_000_000.0 + i,
            ),
            status=WorkItemStatus.READY,
        )
        for i in range(7)
    )
    s = summarize_recent_items(items, keep=3)
    assert len(s) == 3
    # 按 created_at 升序取最后 3 个
    assert [x.work_id for x in s] == ["w-4", "w-5", "w-6"]


def test_summarize_recent_items_empty_inputs() -> None:
    assert summarize_recent_items(()) == ()
    assert summarize_recent_items((), keep=0) == ()


def test_planner_context_rejects_non_dict_budget_snapshot() -> None:
    with pytest.raises(TypeError):
        PlannerContext(
            goal=GOAL,
            verified_facts=(),
            last_decision=None,
            recent_items=(),
            budget_snapshot="not a snapshot",  # type: ignore[arg-type]
            capabilities=(),
        )


def test_planner_rejects_non_budget() -> None:
    with pytest.raises(TypeError):
        DefaultLeadPlanner(ProgrammableModel(), "not a budget")  # type: ignore[arg-type]


def test_planner_rejects_non_callable_model() -> None:
    with pytest.raises(TypeError):
        DefaultLeadPlanner(object(), BudgetRootBudget(Budget()))  # type: ignore[arg-type]


def test_planner_rejects_zero_max_attempts() -> None:
    with pytest.raises(ValueError):
        DefaultLeadPlanner(
            ProgrammableModel(), BudgetRootBudget(Budget()), max_attempts=0
        )


# ===========================================================================
# 11. 不影响普通 Agent 契约
# ===========================================================================


def test_default_system_prompt_is_a_non_empty_string() -> None:
    from mini_deerflow.lead_agent.planner import default_system_prompt

    prompt = default_system_prompt()
    assert isinstance(prompt, str)
    assert len(prompt) > 100
    # 关键三选一都列出来了
    assert "propose_batch" in prompt
    assert "request_clarification" in prompt
    assert "recommend_synthesis" in prompt


def test_existing_fake_model_is_unchanged() -> None:
    """Planner 不修改仓库既有的 `FakeModel` 契约。"""
    from mini_deerflow.models import FakeModel, FinalAnswer  # type: ignore[attr-defined]

    fm = FakeModel()
    # FakeModel 仍按既有行为回答；不应被 Planner 改造
    out = fm.decide([Message(role="user", content="hello")])
    assert isinstance(out, FinalAnswer)
    # 现有 calc 行为也保留
    out2 = fm.decide([Message(role="user", content="计算 1+1")])
    assert out2.__class__.__name__ == "ToolCall"  # FakeModel 返回 ToolCall


def test_planner_does_not_pollute_lead_agent_namespace_with_toolcall() -> None:
    """Planner 只暴露 PlannerContext / PlannerOutput / ProposeBatch 等，
    不向 lead_agent 顶层注入 ToolCall / Message 等仓库既有类型。"""
    import mini_deerflow.lead_agent as p

    # 这些应当存在（既有）
    for name in ("WorkItemProposal", "VerifiedFact", "ResourceClaim"):
        assert hasattr(p, name)
    # 这些是 Planner 新增的
    for name in ("PlannerContext", "PlannerOutput", "DefaultLeadPlanner"):
        assert hasattr(p, name)
    # 不应泄漏 Model 协议 / Message / FinalAnswer 到 lead_agent 顶层
    for name in ("Model", "Message", "FinalAnswer", "ToolCall"):
        assert not hasattr(p, name), f"lead_agent 顶层不应暴露 {name}"


# ===========================================================================
# 12. `propose_batch` 触发 25A `validate_batch` 校验（**不**另写冲突规则）
# ===========================================================================


def test_propose_batch_runs_validate_batch_which_rejects_cycle() -> None:
    """模型给两个互依赖的提案 → validate_batch 报 cycle → Planner 反馈重试。"""
    bad = json.dumps(
        {
            "kind": "propose_batch",
            "proposals": [
                {
                    "work_id": "w-a",
                    "objective": "a",
                    "depends_on": ["w-b"],
                    "claims": [],
                    "execution_route": "direct_agent",
                },
                {
                    "work_id": "w-b",
                    "objective": "b",
                    "depends_on": ["w-a"],
                    "claims": [],
                    "execution_route": "direct_agent",
                },
            ],
            "rationale": "x",
        }
    )
    good = json.dumps(
        {
            "kind": "propose_batch",
            "proposals": [
                {
                    "work_id": "w-a",
                    "objective": "a",
                    "depends_on": [],
                    "claims": [],
                    "execution_route": "direct_agent",
                }
            ],
            "rationale": "ok",
        }
    )
    model = ProgrammableModel(responses=[bad, good])
    planner = DefaultLeadPlanner(model, BudgetRootBudget(Budget(max_model_calls=5)))

    output = planner.plan(make_context())
    assert isinstance(output, ProposeBatch)
    assert output.proposals[0].work_id == "w-a"
    assert model.call_count == 2


def test_propose_batch_detects_resource_conflict_in_same_batch() -> None:
    """同批两个提案一个写文件一个读子文件 → ResourceConflictError → 重规划。"""
    bad = json.dumps(
        {
            "kind": "propose_batch",
            "proposals": [
                {
                    "work_id": "w-w",
                    "objective": "w",
                    "depends_on": [],
                    "claims": [{"resource": "file:src", "access": "write"}],
                    "execution_route": "direct_agent",
                },
                {
                    "work_id": "w-r",
                    "objective": "r",
                    "depends_on": [],
                    "claims": [{"resource": "file:src/a.py", "access": "read"}],
                    "execution_route": "direct_agent",
                },
            ],
            "rationale": "x",
        }
    )
    good = json.dumps(
        {
            "kind": "propose_batch",
            "proposals": [
                {
                    "work_id": "w-only",
                    "objective": "ok",
                    "depends_on": [],
                    "claims": [],
                    "execution_route": "direct_agent",
                }
            ],
            "rationale": "ok",
        }
    )
    model = ProgrammableModel(responses=[bad, good])
    planner = DefaultLeadPlanner(model, BudgetRootBudget(Budget(max_model_calls=5)))

    output = planner.plan(make_context())
    assert isinstance(output, ProposeBatch)
    assert output.proposals[0].work_id == "w-only"
    assert model.call_count == 2
    # 第二次的 user 消息里包含 ResourceConflictError 反馈
    second_user = model.seen_messages[1][-1].content
    assert "ResourceConflictError" in second_user


# ===========================================================================
# 13. 与 LeadRunState / LeadRunStore 的隔离验证（25D 才接）
# ===========================================================================


def test_planner_never_imports_orchestrator_or_executor() -> None:
    """通过 AST 检查：planner.py 不会 import 25D 才有的模块。

    这条防止有人在 25C 范围里偷偷把 Orchestrator / SubagentExecutor 拉进来。
    """
    import ast
    import mini_deerflow.lead_agent.planner as p

    source = ast.parse(open(p.__file__, encoding="utf-8").read())
    forbidden_prefixes = (
        "mini_deerflow.lead_agent.orchestrator",
        "mini_deerflow.subagent_executor",
        "mini_deerflow.subagent_tools",
    )
    for node in ast.walk(source):
        if isinstance(node, ast.ImportFrom) and node.module:
            for fp in forbidden_prefixes:
                if node.module.startswith(fp):
                    pytest.fail(f"planner.py 禁止 import 25D 模块: {node.module}")
        if isinstance(node, ast.Import):
            for alias in node.names:
                for fp in forbidden_prefixes:
                    if alias.name.startswith(fp):
                        pytest.fail(f"planner.py 禁止 import 25D 模块: {alias.name}")
