"""Phase 25A / 领域模型测试。

覆盖规格必测场景：序列化往返、frozen 数据不可意外修改、目标字段非空、
WorkItem「首次执行 + 一次修订」上限、VerifiedFact 必须带验证与证据。
"""

from __future__ import annotations

import dataclasses

import pytest

from mini_deerflow.lead_agent import (
    EmptyGoalFieldError,
    EvidenceReceipt,
    ExecutionAttempt,
    ExecutionRoute,
    GoalAmendment,
    GoalContract,
    InvalidTransitionError,
    LeadRunState,
    LeadRunStatus,
    PlanningDecision,
    ResourceAccessMode,
    ResourceClaim,
    RevisionLimitExceededError,
    VerificationResult,
    VerifiedFact,
    WorkItem,
    WorkItemProposal,
    WorkItemStatus,
    validate_transition,
)

T0 = 1_700_000_000.0


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def _goal(**overrides) -> GoalContract:
    payload: dict = dict(
        goal_id="goal-1",
        objective="验证滚动批次可用",
        acceptance_criteria=("批次校验通过", "状态机闭环"),
        created_at=T0,
    )
    payload.update(overrides)
    return GoalContract(**payload)


def _receipt(rid: str = "rcpt-1") -> EvidenceReceipt:
    return EvidenceReceipt(receipt_id=rid, source="unit-test", observed_at=T0)


def _proposal(wid: str = "w-1", **overrides) -> WorkItemProposal:
    payload: dict = dict(
        work_id=wid,
        objective=f"完成 {wid}",
        depends_on=(),
        claims=(),
        created_at=T0,
    )
    payload.update(overrides)
    return WorkItemProposal(**payload)


# ---------------------------------------------------------------------------
# GoalContract
# ---------------------------------------------------------------------------


def test_goal_contract_requires_non_empty_objective() -> None:
    """objective 为空抛领域异常（与 acceptance_criteria 一致，
    便于调用方按「目标不完整」统一处理）。"""
    with pytest.raises(EmptyGoalFieldError, match="objective"):
        _goal(objective="   ")


def test_goal_contract_requires_non_empty_acceptance_criteria() -> None:
    with pytest.raises(EmptyGoalFieldError, match="acceptance_criteria"):
        _goal(acceptance_criteria=())


def test_goal_contract_rejects_blank_criteria_item() -> None:
    with pytest.raises(ValueError, match="acceptance_criteria"):
        _goal(acceptance_criteria=("有效项", "   "))


def test_goal_contract_create_uses_wall_clock() -> None:
    goal = GoalContract.create(
        goal_id="g", objective="目标", acceptance_criteria=["a"]
    )
    assert goal.created_at > 0


def test_goal_amend_returns_new_contract_original_untouched() -> None:
    """frozen：修订单返回新契约，原契约逐字段不变。"""
    original = _goal()
    amendment = GoalAmendment(
        amendment_id="amd-1",
        objective="修订后的目标",
        acceptance_criteria=("新验收项",),
        reason="需求澄清",
        created_at=T0 + 1,
    )
    amended = original.amend(amendment)

    assert amended is not original
    assert original.amendments == ()
    assert original.objective == "验证滚动批次可用"
    assert len(amended.amendments) == 1
    assert amended.amendments[0].amendment_id == "amd-1"
    # 修订不改写旧 objective 字段本身，只追加记录（记录携带新目标）
    assert amended.objective == original.objective


def test_goal_amendment_requires_criteria() -> None:
    with pytest.raises(EmptyGoalFieldError):
        GoalAmendment(
            amendment_id="amd",
            objective="目标",
            acceptance_criteria=(),
            reason="",
            created_at=T0,
        )


# ---------------------------------------------------------------------------
# 提案与资源声明
# ---------------------------------------------------------------------------


def test_proposal_rejects_empty_work_id_and_objective() -> None:
    with pytest.raises(ValueError, match="work_id"):
        _proposal(wid="  ")
    with pytest.raises(ValueError, match="objective"):
        WorkItemProposal(work_id="w", objective=" ", created_at=T0)


def test_proposal_rejects_duplicate_dependency() -> None:
    with pytest.raises(ValueError, match="depends_on 重复"):
        _proposal(depends_on=("a", "a"))


def test_proposal_resources_deduplicated_in_order() -> None:
    proposal = _proposal(
        claims=(
            ResourceClaim(resource="/db", access=ResourceAccessMode.WRITE),
            ResourceClaim(resource="/db", access=ResourceAccessMode.READ),
            ResourceClaim(resource="/api", access=ResourceAccessMode.READ),
        )
    )
    assert proposal.resources == ("/db", "/api")


# ---------------------------------------------------------------------------
# 执行路线（ExecutionRoute）—— 与读写方式正交
# ---------------------------------------------------------------------------


def test_proposal_execution_route_defaults_to_direct_agent() -> None:
    """未指定执行路线时默认由父线程内的 Agent 直接跑。"""
    assert _proposal().execution_route is ExecutionRoute.DIRECT_AGENT


def test_proposal_execution_route_accepts_all_four_values() -> None:
    # 每种路线配齐必填的显式路由目标
    extras = {
        ExecutionRoute.DIRECT_AGENT: {},
        ExecutionRoute.TOOL: {"tool_name": "calculator"},
        ExecutionRoute.SUBAGENT: {"subagent_type": "general_purpose"},
        ExecutionRoute.SUBAGENT_BATCH: {"subagent_type": "general_purpose"},
    }
    for route in (
        ExecutionRoute.DIRECT_AGENT,
        ExecutionRoute.TOOL,
        ExecutionRoute.SUBAGENT,
        ExecutionRoute.SUBAGENT_BATCH,
    ):
        proposal = WorkItemProposal(
            work_id=f"w-{route.value}",
            objective="o",
            execution_route=route,
            **extras[route],
        )
        assert proposal.execution_route is route
        # 枚举值可直接序列化往返
        assert WorkItemProposal.from_dict(proposal.to_dict()).execution_route is route


def test_proposal_execution_route_roundtrip() -> None:
    proposal = WorkItemProposal(
        work_id="w-sub",
        objective="派给子代理",
        execution_route=ExecutionRoute.SUBAGENT_BATCH,
        subagent_type="general_purpose",
    )
    payload = proposal.to_dict()
    assert payload["execution_route"] == "subagent_batch"
    assert payload["subagent_type"] == "general_purpose"
    assert WorkItemProposal.from_dict(payload) == proposal


def test_proposal_rejects_non_route_execution_route() -> None:
    with pytest.raises(ValueError, match="ExecutionRoute"):
        WorkItemProposal(
            work_id="w", objective="o", execution_route="read"  # type: ignore[arg-type]
        )


def test_item_inherits_execution_route_from_proposal() -> None:
    """执行路线随提案落到执行单元——路由阶段的决策不丢失。"""
    proposal = WorkItemProposal(
        work_id="w-tool",
        objective="当工具跑",
        execution_route=ExecutionRoute.TOOL,
        tool_name="calculator",
    )
    item = WorkItem.from_proposal(proposal)
    assert item.execution_route is ExecutionRoute.TOOL
    assert item.tool_name == "calculator"

    payload = item.to_dict()
    assert payload["execution_route"] == "tool"
    assert WorkItem.from_dict(payload).execution_route is ExecutionRoute.TOOL


def test_item_execution_route_defaults_to_direct_agent() -> None:
    item = _ready_item("w-plain")
    assert item.execution_route is ExecutionRoute.DIRECT_AGENT


def test_item_rejects_non_route_execution_route() -> None:
    """字段类型必须是 ExecutionRoute 实例，不接受字符串等替身。"""
    with pytest.raises(ValueError, match="ExecutionRoute"):
        WorkItem(
            work_id="w",
            objective="o",
            status=WorkItemStatus.READY,
            depends_on=(),
            claims=(),
            created_at=T0,
            execution_route="subagent",  # type: ignore[arg-type]
        )


def test_item_rejects_unknown_execution_route_string_on_load() -> None:
    """反序列化遇到未知执行路线必须报错，不能静默兜底成默认值。"""
    payload = _ready_item("w-x").to_dict()
    payload["execution_route"] = "teleport"
    with pytest.raises(ValueError, match="ExecutionRoute"):
        WorkItem.from_dict(payload)


def test_lead_run_state_roundtrip_keeps_execution_route() -> None:
    """嵌套在 LeadRunState 里的 WorkItem，执行路线照样往返。"""
    state = LeadRunState.start(
        lead_run_id="r",
        goal=_goal(),
        thread_id="thread-a",
        now=T0,
    )
    proposal = WorkItemProposal(
        work_id="w-batch",
        objective="并行子任务",
        execution_route=ExecutionRoute.SUBAGENT_BATCH,
        subagent_type="general_purpose",
        created_at=T0,
    )
    state.add_item(WorkItem.from_proposal(proposal))

    restored = LeadRunState.from_dict(state.to_dict())
    assert restored.item("w-batch") is not None
    assert restored.item("w-batch").execution_route is ExecutionRoute.SUBAGENT_BATCH  # type: ignore[union-attr]


def test_resource_claim_requires_non_empty_resource() -> None:
    with pytest.raises(ValueError, match="resource"):
        ResourceClaim(resource="   ", access=ResourceAccessMode.READ)


def test_resource_claim_of_normalizes_path() -> None:
    claim = ResourceClaim.of("  data\\\\store.db  ", ResourceAccessMode.WRITE)
    # 路径型资源：反斜杠归一 + 大小写折叠（Windows 下大小写可能同文件）
    assert claim.resource == "data/store.db"
    assert claim.access is ResourceAccessMode.WRITE


def test_resource_claim_of_rejects_unknown_route() -> None:
    with pytest.raises(Exception, match="未知访问方式"):
        ResourceClaim.of("/x", "append")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# WorkItem：状态转换与修订上限
# ---------------------------------------------------------------------------


def _ready_item(wid: str = "w-1") -> WorkItem:
    return WorkItem.from_proposal(_proposal(wid))


def test_item_from_proposal_starts_ready() -> None:
    item = _ready_item()
    assert item.status is WorkItemStatus.READY
    assert item.attempt_count == 0
    assert item.is_terminal is False


def test_item_full_lifecycle_first_execution_succeeds() -> None:
    item = _ready_item()
    item.transition_to(WorkItemStatus.RUNNING, now=T0, attempt_id="att-1")
    assert item.attempt_count == 1
    assert item.attempts[0].sequence == 1
    assert item.attempts[0].is_revision is False

    item.transition_to(
        WorkItemStatus.SUCCEEDED, now=T0 + 10, outcome="ok"
    )
    assert item.is_terminal
    # attempt 结束信息已补记
    assert item.attempts[0].finished_at == T0 + 10
    assert item.attempts[0].outcome == "ok"


def test_item_revision_once_then_must_terminate() -> None:
    """冻结设计：首次执行 + 一次修订；第二次失败不允许再修订。"""
    item = _ready_item()

    # 首次执行 -> 失败 -> BLOCKED（待修订）
    item.transition_to(WorkItemStatus.RUNNING, now=T0, attempt_id="att-1")
    item.transition_to(WorkItemStatus.BLOCKED, now=T0 + 5, outcome="boom")
    assert item.revised is False

    # 发起唯一一次修订
    item.transition_to(WorkItemStatus.READY, now=T0 + 6)
    assert item.revised is True

    # 第二次执行
    item.transition_to(WorkItemStatus.RUNNING, now=T0 + 7, attempt_id="att-2")
    assert item.attempt_count == 2
    assert item.attempts[1].sequence == 2
    assert item.attempts[1].is_revision is True

    # 第二次失败 -> 必须落到 FAILED 终态，不允许再修订
    with pytest.raises(RevisionLimitExceededError, match="w-1"):
        item.transition_to(WorkItemStatus.BLOCKED, now=T0 + 12, outcome="boom2")

    item.transition_to(WorkItemStatus.FAILED, now=T0 + 12, outcome="boom2")
    assert item.is_terminal
    assert item.attempts[1].outcome == "boom2"


def test_item_running_requires_attempt_id() -> None:
    item = _ready_item()
    with pytest.raises(ValueError, match="attempt_id"):
        item.transition_to(WorkItemStatus.RUNNING, now=T0)


def test_item_rejects_illegal_transition() -> None:
    item = _ready_item()
    with pytest.raises(InvalidTransitionError):
        item.transition_to(WorkItemStatus.SUCCEEDED)  # READY 不能直接成功


def test_item_terminal_transition_rejected_by_state_machine() -> None:
    item = _ready_item()
    item.transition_to(WorkItemStatus.RUNNING, now=T0, attempt_id="a")
    item.transition_to(WorkItemStatus.CANCELLED, now=T0 + 1)
    with pytest.raises(InvalidTransitionError):
        item.transition_to(WorkItemStatus.READY)


def test_item_rejects_bad_fields() -> None:
    with pytest.raises(ValueError, match="objective"):
        WorkItem(
            work_id="w",
            objective=" ",
            status=WorkItemStatus.READY,
            depends_on=(),
            claims=(),
            created_at=T0,
        )
    with pytest.raises(ValueError, match="WorkItemStatus"):
        WorkItem(
            work_id="w",
            objective="ok",
            status="ready",  # type: ignore[arg-type]
            depends_on=(),
            claims=(),
            created_at=T0,
        )


def test_execution_attempt_validates_sequence() -> None:
    with pytest.raises(ValueError, match="sequence"):
        ExecutionAttempt(
            attempt_id="a", work_id="w", sequence=0, started_at=T0
        )
    with pytest.raises(ValueError, match="finished_at"):
        ExecutionAttempt(
            attempt_id="a",
            work_id="w",
            sequence=1,
            started_at=T0 + 10,
            finished_at=T0,
        )


# ---------------------------------------------------------------------------
# 验证与事实
# ---------------------------------------------------------------------------


def test_verification_pass_requires_evidence() -> None:
    """通过验证但没有证据 = 假通过，构造期拒绝。"""
    with pytest.raises(ValueError, match="至少携带|receipts"):
        VerificationResult(
            verification_id="v-1",
            work_id="w-1",
            passed=True,
            receipts=(),
            verified_at=T0,
        )


def test_verification_failure_needs_no_evidence() -> None:
    result = VerificationResult(
        verification_id="v-1",
        work_id="w-1",
        passed=False,
        receipts=(),
        verified_at=T0,
    )
    assert result.passed is False


def test_verified_fact_requires_evidence() -> None:
    with pytest.raises(ValueError, match="EvidenceReceipt"):
        VerifiedFact(
            fact_id="f-1",
            statement="已验证",
            work_id="w-1",
            verification_id="v-1",
            receipts=(),
            established_at=T0,
        )


def test_verified_fact_requires_non_empty_statement() -> None:
    with pytest.raises(ValueError, match="statement"):
        VerifiedFact(
            fact_id="f-1",
            statement="  ",
            work_id="w-1",
            verification_id="v-1",
            receipts=(_receipt(),),
            established_at=T0,
        )


def test_evidence_receipt_requires_source() -> None:
    with pytest.raises(ValueError, match="source"):
        EvidenceReceipt(receipt_id="r", source=" ", observed_at=T0)


# ---------------------------------------------------------------------------
# LeadRunState
# ---------------------------------------------------------------------------


def _state(status: LeadRunStatus = LeadRunStatus.CREATED) -> LeadRunState:
    state = LeadRunState.start(
        lead_run_id="run-1", goal=_goal(), thread_id="thread-a", now=T0
    )
    state.status = status
    return state


def test_start_begins_created() -> None:
    state = LeadRunState.start(
        lead_run_id="run-1", goal=_goal(), thread_id="thread-a", now=T0
    )
    assert state.status is LeadRunStatus.CREATED
    assert state.is_terminal is False
    assert state.batch_index == 0
    assert state.verified_work_ids == frozenset()


def test_state_transition_updates_status_and_timestamp() -> None:
    state = LeadRunState.start(
        lead_run_id="run-1", goal=_goal(), thread_id="thread-a", now=T0
    )
    state.transition_to(LeadRunStatus.PLANNING, now=T0 + 1)
    assert state.status is LeadRunStatus.PLANNING
    assert state.updated_at == T0 + 1


def test_state_terminal_transition_raises() -> None:
    state = _state(LeadRunStatus.SUCCEEDED)
    with pytest.raises(InvalidTransitionError):
        state.transition_to(LeadRunStatus.PLANNING)


def test_state_add_decision_rejects_duplicate_id() -> None:
    state = _state(LeadRunStatus.PLANNING)
    decision = PlanningDecision(
        decision_id="d-1",
        batch_index=0,
        proposals=(_proposal("w-1"),),
        decided_at=T0,
    )
    state.add_decision(decision)
    with pytest.raises(ValueError, match="decision_id 重复"):
        state.add_decision(decision)
    assert len(state.decisions) == 1


def test_state_add_verified_fact_tracks_verified_history() -> None:
    state = _state(LeadRunStatus.EXECUTING)
    fact = VerifiedFact(
        fact_id="f-1",
        statement="批次校验可用",
        work_id="w-1",
        verification_id="v-1",
        receipts=(_receipt(),),
        established_at=T0,
    )
    state.add_verified_fact(fact)
    assert state.verified_work_ids == frozenset({"w-1"})

    with pytest.raises(ValueError, match="fact_id 重复"):
        state.add_verified_fact(fact)


def test_state_add_item_rejects_duplicate_work_id() -> None:
    state = _state(LeadRunStatus.EXECUTING)
    item = _ready_item("w-1")
    state.add_item(item)
    with pytest.raises(ValueError, match="work_id 重复"):
        state.add_item(_ready_item("w-1"))
    assert state.item("w-1") is item
    assert state.item("nope") is None


def test_state_replace_item_updates_in_place() -> None:
    state = _state(LeadRunStatus.EXECUTING)
    item = _ready_item("w-1")
    state.add_item(item)
    state.add_item(_ready_item("w-2"))

    item.transition_to(WorkItemStatus.RUNNING, now=T0, attempt_id="a")
    state.replace_item(item)

    assert state.item("w-1") is item
    assert len(state.items) == 2
    assert state.item("w-1").status is WorkItemStatus.RUNNING  # type: ignore[union-attr]

    with pytest.raises(KeyError, match="nope"):
        state.replace_item(_ready_item("nope"))


def test_state_never_offers_commit_method() -> None:
    """冻结设计：ThreadState 仅在整个 Run 成功后由上层提交，
    领域状态对象不该有 commit()。"""
    state = _state()
    assert not hasattr(state, "commit")
    assert not hasattr(state, "submit")


def test_state_rejects_bad_fields() -> None:
    with pytest.raises(ValueError, match="lead_run_id"):
        LeadRunState(
            lead_run_id=" ",
            goal=_goal(),
            status=LeadRunStatus.CREATED,
            thread_id="thread-a",
            created_at=T0,
        )
    with pytest.raises(ValueError, match="batch_index"):
        LeadRunState(
            lead_run_id="r",
            goal=_goal(),
            status=LeadRunStatus.CREATED,
            thread_id="thread-a",
            created_at=T0,
            batch_index=-1,
        )
    with pytest.raises(ValueError, match="GoalContract"):
        LeadRunState(
            lead_run_id="r",
            goal="not-a-goal",  # type: ignore[arg-type]
            status=LeadRunStatus.CREATED,
            thread_id="thread-a",
            created_at=T0,
        )


def test_state_rejects_duplicate_items_at_construction() -> None:
    with pytest.raises(ValueError, match="work_id 重复"):
        LeadRunState(
            lead_run_id="r",
            goal=_goal(),
            status=LeadRunStatus.CREATED,
            thread_id="thread-a",
            created_at=T0,
            items=(_ready_item("w-1"), _ready_item("w-1")),
        )


# ---------------------------------------------------------------------------
# 序列化往返
# ---------------------------------------------------------------------------

_ROUNDTRIP_MODELS: list[tuple[str, object]] = [
    ("GoalContract", _goal()),
    (
        "GoalAmendment",
        GoalAmendment(
            amendment_id="amd-1",
            objective="修订",
            acceptance_criteria=("项1",),
            reason="澄清",
            created_at=T0,
        ),
    ),
    (
        "WorkItemProposal",
        WorkItemProposal(
            work_id="w-1",
            objective="目标",
            depends_on=("dep",),
            claims=(ResourceClaim(resource="/db", access=ResourceAccessMode.WRITE),),
            created_at=T0,
        ),
    ),
    (
        "PlanningDecision",
        PlanningDecision(
            decision_id="d-1",
            batch_index=0,
            proposals=(_proposal("w-1"),),
            decided_at=T0,
            rationale="首批",
        ),
    ),
    (
        "ExecutionAttempt",
        ExecutionAttempt(
            attempt_id="att-1",
            work_id="w-1",
            sequence=1,
            started_at=T0,
            finished_at=T0 + 5,
            outcome="ok",
        ),
    ),
    ("EvidenceReceipt", _receipt()),
    (
        "VerificationResult",
        VerificationResult(
            verification_id="v-1",
            work_id="w-1",
            passed=True,
            receipts=(_receipt(),),
            verified_at=T0,
            notes="通过",
        ),
    ),
    (
        "VerifiedFact",
        VerifiedFact(
            fact_id="f-1",
            statement="事实",
            work_id="w-1",
            verification_id="v-1",
            receipts=(_receipt(),),
            established_at=T0,
        ),
    ),
    ("ResourceClaim", ResourceClaim(resource="/api", access=ResourceAccessMode.READ)),
]


@pytest.mark.parametrize("name,obj", _ROUNDTRIP_MODELS, ids=[n for n, _ in _ROUNDTRIP_MODELS])
def test_small_model_roundtrip(name: str, obj) -> None:
    payload = obj.to_dict()
    restored = type(obj).from_dict(payload)
    assert restored == obj, f"{name} 序列化往返不一致"
    assert restored.to_dict() == payload


def test_work_item_roundtrip_with_attempts() -> None:
    item = _ready_item("w-1")
    item.transition_to(WorkItemStatus.RUNNING, now=T0, attempt_id="att-1")
    item.transition_to(WorkItemStatus.BLOCKED, now=T0 + 5, outcome="boom")
    item.transition_to(WorkItemStatus.READY, now=T0 + 6)  # 标记 revised
    item.transition_to(WorkItemStatus.RUNNING, now=T0 + 7, attempt_id="att-2")

    payload = item.to_dict()
    restored = WorkItem.from_dict(payload)

    assert restored == item
    assert restored.revised is True
    assert restored.attempt_count == 2
    assert restored.attempts[1].sequence == 2
    assert restored.to_dict() == payload


def test_lead_run_state_full_roundtrip() -> None:
    state = _state(LeadRunStatus.EXECUTING)
    state.batch_index = 2
    state.add_decision(
        PlanningDecision(
            decision_id="d-1",
            batch_index=0,
            proposals=(_proposal("w-1"),),
            decided_at=T0,
        )
    )
    item = _ready_item("w-1")
    state.add_item(item)
    state.add_verified_fact(
        VerifiedFact(
            fact_id="f-1",
            statement="已验证事实",
            work_id="w-0",
            verification_id="v-0",
            receipts=(_receipt("rc-0"),),
            established_at=T0,
        )
    )
    state.goal = state.goal.amend(
        GoalAmendment(
            amendment_id="amd-1",
            objective="新目标",
            acceptance_criteria=("新项",),
            reason="",
            created_at=T0,
        )
    )

    payload = state.to_dict()
    restored = LeadRunState.from_dict(payload)

    assert restored == state
    assert restored.to_dict() == payload
    assert restored.status is LeadRunStatus.EXECUTING
    assert restored.batch_index == 2
    assert restored.verified_work_ids == frozenset({"w-0"})
    assert len(restored.goal.amendments) == 1
    assert restored.item("w-1") is not None


def test_roundtrip_rejects_wrong_schema_version() -> None:
    payload = _goal().to_dict()
    payload["schema_version"] = 99
    with pytest.raises(ValueError, match="schema_version"):
        GoalContract.from_dict(payload)


def test_roundtrip_rejects_missing_required_field() -> None:
    payload = _goal().to_dict()
    del payload["objective"]
    with pytest.raises(ValueError, match="缺少字段"):
        GoalContract.from_dict(payload)


def test_roundtrip_rejects_unknown_enum_value() -> None:
    payload = _state(LeadRunStatus.EXECUTING).to_dict()
    payload["status"] = "exploded"
    with pytest.raises(ValueError, match="LeadRunStatus"):
        LeadRunState.from_dict(payload)


def test_roundtrip_rejects_non_dict() -> None:
    with pytest.raises(ValueError, match="字典"):
        EvidenceReceipt.from_dict(["not", "a", "dict"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# frozen 不可意外修改
# ---------------------------------------------------------------------------

FROZEN_INSTANCES: list[tuple[str, object]] = [
    ("GoalContract", _goal()),
    (
        "GoalAmendment",
        GoalAmendment(
            amendment_id="a",
            objective="o",
            acceptance_criteria=("c",),
            reason="",
            created_at=T0,
        ),
    ),
    ("WorkItemProposal", _proposal()),
    (
        "PlanningDecision",
        PlanningDecision(
            decision_id="d",
            batch_index=0,
            proposals=(_proposal(),),
            decided_at=T0,
        ),
    ),
    (
        "ExecutionAttempt",
        ExecutionAttempt(
            attempt_id="a", work_id="w", sequence=1, started_at=T0
        ),
    ),
    ("EvidenceReceipt", _receipt()),
    ("ResourceClaim", ResourceClaim(resource="/x", access=ResourceAccessMode.READ)),
    (
        "VerificationResult",
        VerificationResult(
            verification_id="v",
            work_id="w",
            passed=False,
            receipts=(),
            verified_at=T0,
        ),
    ),
]


@pytest.mark.parametrize(
    "name,obj", FROZEN_INSTANCES, ids=[n for n, _ in FROZEN_INSTANCES]
)
def test_frozen_models_reject_field_assignment(name: str, obj) -> None:
    """frozen：任何字段赋值都必须抛 FrozenInstanceError（不可意外修改）。"""
    field = next(iter(dataclasses.fields(obj))).name
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(obj, field, "tampered")


def test_frozen_goal_cannot_append_amendments_directly() -> None:
    goal = _goal()
    with pytest.raises(dataclasses.FrozenInstanceError):
        goal.amendments = ()  # type: ignore[misc]
    # 只能通过 amend() 得到新对象
    assert isinstance(goal.amendments, tuple)


def test_nested_frozen_payload_is_immutable() -> None:
    """VerifiedFact 内嵌的 receipts 同样不可变。"""
    fact = VerifiedFact(
        fact_id="f",
        statement="s",
        work_id="w",
        verification_id="v",
        receipts=(_receipt(),),
        established_at=T0,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        fact.receipts = ()  # type: ignore[misc]


def test_verify_transition_helper_usable_from_models() -> None:
    """模型层直接复用状态机（单向依赖，无循环导入）。"""
    validate_transition("x", LeadRunStatus.CREATED, LeadRunStatus.PLANNING)


# ===========================================================================
# 25D 序列化兼容边界：明确"老 DIRECT_AGENT 加载 OK"+"老 TOOL/SUBAGENT
# 加载失败"是有意行为（**不**为通过而猜测执行目标）
# ===========================================================================


def test_legacy_direct_agent_payload_loads_with_none_targets() -> None:
    """25D 之前唯一可能持久化的路线是 DIRECT_AGENT——它**没有** target 字段。
    加载老 payload 时两个新字段默认 None → DIRECT_AGENT 不变式满足 → 正常加载。
    """
    legacy = {
        "schema_version": 1,
        "work_id": "w-legacy-direct",
        "objective": "o",
        "depends_on": [],
        "claims": [],
        "execution_route": "direct_agent",
        "created_at": 0.0,
    }
    p = WorkItemProposal.from_dict(legacy)
    assert p.execution_route is ExecutionRoute.DIRECT_AGENT
    assert p.tool_name is None
    assert p.subagent_type is None


def test_legacy_tool_payload_raises_construct_time_error() -> None:
    """老 TOOL payload（无 `tool_name`）→ `from_dict` 字段默认为 None → 构造期
    ValueError（**不**猜测执行目标——这是有意的破坏性变更）。

    25D 之前**没有**任何持久化数据用 TOOL 路线（Orchestrator 25E 才接
    ToolRegistry 注入 `tool_name`），所以"老 TOOL 数据"目前是空集。
    若未来真有，必须**重新规划**或**显式迁移**。
    """
    legacy = {
        "schema_version": 1,
        "work_id": "w-legacy-tool",
        "objective": "o",
        "depends_on": [],
        "claims": [],
        "execution_route": "tool",  # 缺 tool_name
        "created_at": 0.0,
    }
    with pytest.raises(ValueError, match="TOOL"):
        WorkItemProposal.from_dict(legacy)


def test_legacy_subagent_payload_raises_construct_time_error() -> None:
    """老 SUBAGENT / SUBAGENT_BATCH payload（无 `subagent_type`）→ 构造期 ValueError。"""
    legacy_sub = {
        "schema_version": 1,
        "work_id": "w-legacy-sub",
        "objective": "o",
        "depends_on": [],
        "claims": [],
        "execution_route": "subagent",  # 缺 subagent_type
        "created_at": 0.0,
    }
    legacy_batch = {
        "schema_version": 1,
        "work_id": "w-legacy-batch",
        "objective": "o",
        "depends_on": [],
        "claims": [],
        "execution_route": "subagent_batch",  # 缺 subagent_type
        "created_at": 0.0,
    }
    with pytest.raises(ValueError, match="subagent"):
        WorkItemProposal.from_dict(legacy_sub)
    with pytest.raises(ValueError, match="subagent"):
        WorkItemProposal.from_dict(legacy_batch)


def test_workitem_legacy_direct_agent_payload_also_loads() -> None:
    """WorkItem 同样：老 DIRECT_AGENT WorkItem payload 加载成功。"""
    legacy = {
        "schema_version": 1,
        "work_id": "w-legacy-wi",
        "objective": "o",
        "status": "ready",
        "depends_on": [],
        "claims": [],
        "attempts": [],
        "execution_route": "direct_agent",
        "revised": False,
        "created_at": 0.0,
    }
    wi = WorkItem.from_dict(legacy)
    assert wi.execution_route is ExecutionRoute.DIRECT_AGENT
    assert wi.tool_name is None
    assert wi.subagent_type is None


def test_workitem_legacy_tool_payload_raises() -> None:
    """WorkItem 同样：老 TOOL payload 加载会抛（与 proposal 一致）。"""
    legacy = {
        "schema_version": 1,
        "work_id": "w-legacy-wi-tool",
        "objective": "o",
        "status": "ready",
        "depends_on": [],
        "claims": [],
        "attempts": [],
        "execution_route": "tool",
        "revised": False,
        "created_at": 0.0,
    }
    with pytest.raises(ValueError, match="TOOL"):
        WorkItem.from_dict(legacy)


def test_system_prompt_explicitly_requires_target_fields() -> None:
    """25D 契约：系统提示词必须显式告诉模型输出 `tool_name` / `subagent_type`。

    这是 Planner 输出的 JSON 契约的一部分——模型读了 prompt 才能知道补字段。
    任何对系统提示词的修改都必须保留这两条规则。
    """
    from mini_deerflow.lead_agent.planner import default_system_prompt

    prompt = default_system_prompt()
    assert "tool_name" in prompt, "系统提示词必须包含 tool_name 字段说明"
    assert "subagent_type" in prompt, "系统提示词必须包含 subagent_type 字段说明"
    # 规则里也要点名——不只是 example
    assert "tool" in prompt and "subagent" in prompt
    # 必须显式说"选 tool 时必须给出 tool_name"
    assert "选" in prompt and "必填" in prompt, "必须显式说明选某种 route 时字段必填"
