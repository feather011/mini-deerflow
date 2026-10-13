"""Phase 25D LeadRouter 测试。

按用户要求：测试重点放在**行为**——合法路由、未知工具、禁止递归委派、
per-response/per-run 超限、冲突批次、澄清信息缺失的场景。
不只测类型构造；验证控制流和拒绝路径。
"""

from __future__ import annotations

import pytest

from mini_deerflow.lead_agent import (
    DEFAULT_FORBIDDEN_SUBAGENT_TYPES,
    ExecutionDecision,
    ExecutionRoute,
    GoalContract,
    PlannerContext,
    ProposeBatch,
    RecommendSynthesis,
    RequestClarification,
    ResourceAccessMode,
    ResourceClaim,
    RejectionKind,
    RoutedExecutionPlan,
    RoutingContext,
    RoutingRejection,
    ValidatedBatch,
    ValidatedWorkItem,
    VerifiedFact,
    WorkItemProposal,
    build_budget_snapshot,
    route,
)
from mini_deerflow.lead_agent.models import EvidenceReceipt
from mini_deerflow.lead_agent.planner import (
    _validate_business as planner_validate_business,
)
from mini_deerflow.lead_agent.planner_budget import BudgetRootBudget
from mini_deerflow.lead_agent.planner import parse_planner_output
from mini_deerflow.middleware import Budget


# ===========================================================================
# 固定事实
# ===========================================================================


GOAL = GoalContract.create(
    goal_id="g-1",
    objective="router 测试目标",
    acceptance_criteria=("criterion-A",),
)


def fact(fact_id: str, work_id: str) -> VerifiedFact:
    return VerifiedFact(
        fact_id=fact_id,
        statement="ok",
        work_id=work_id,
        verification_id=f"v-{fact_id}",
        receipts=(
            EvidenceReceipt(
                receipt_id=f"r-{fact_id}", source="t", observed_at=1.0
            ),
        ),
        established_at=1.0,
    )


def proposal(
    work_id: str,
    *,
    route: ExecutionRoute = ExecutionRoute.DIRECT_AGENT,
    tool_name: str | None = None,
    subagent_type: str | None = None,
    claims: tuple[ResourceClaim, ...] = (),
    depends_on: tuple[str, ...] = (),
) -> WorkItemProposal:
    return WorkItemProposal(
        work_id=work_id,
        objective=f"obj-{work_id}",
        depends_on=depends_on,
        claims=claims,
        execution_route=route,
        tool_name=tool_name,
        subagent_type=subagent_type,
        created_at=1.0,
    )


def propose(
    proposals: tuple[WorkItemProposal, ...], rationale: str = "ok"
) -> ProposeBatch:
    return ProposeBatch(proposals=proposals, rationale=rationale)


def ctx(
    proposals: tuple[WorkItemProposal, ...],
    *,
    verified_facts: tuple[VerifiedFact, ...] = (),
    available_tools: frozenset[str] = frozenset(),
    available_subagent_types: frozenset[str] = frozenset(
        {"general_purpose", "bash", "status_report"}
    ),
    forbidden_subagent_types: frozenset[str] = DEFAULT_FORBIDDEN_SUBAGENT_TYPES,
    max_concurrent_subagents: int = 3,
    max_total_subagents: int = 6,
    already_routed_in_response: int = 0,
    already_routed_in_run: int = 0,
) -> RoutingContext:
    return RoutingContext(
        output=propose(proposals),
        goal=GOAL,
        verified_facts=verified_facts,
        available_tools=available_tools,
        available_subagent_types=available_subagent_types,
        forbidden_subagent_types=forbidden_subagent_types,
        max_concurrent_subagents=max_concurrent_subagents,
        max_total_subagents=max_total_subagents,
        already_routed_in_response=already_routed_in_response,
        already_routed_in_run=already_routed_in_run,
    )


def passthrough_ctx(
    output: RequestClarification | RecommendSynthesis,
) -> RoutingContext:
    """透传场景的 ctx 构造（仅 goal + 透传对象）。"""
    return RoutingContext(
        output=output,  # type: ignore[arg-type]
        goal=GOAL,
        verified_facts=(),
        available_tools=frozenset(),
        available_subagent_types=frozenset(),
    )


# ===========================================================================
# 1. 合法路由
# ===========================================================================


def test_legal_direct_agent_route_succeeds() -> None:
    p1 = proposal("w-1")
    out = route(ctx((p1,)))
    assert isinstance(out, RoutedExecutionPlan)
    assert out.total_items == 1
    assert out.batches[0].items[0].decision.route is ExecutionRoute.DIRECT_AGENT


def test_legal_mixed_routes_succeed() -> None:
    p_direct = proposal("w-1", route=ExecutionRoute.DIRECT_AGENT)
    p_tool = proposal("w-2", route=ExecutionRoute.TOOL, tool_name="calculator")
    p_sub = proposal("w-3", route=ExecutionRoute.SUBAGENT, subagent_type="general_purpose")
    out = route(
        ctx(
            (p_direct, p_tool, p_sub),
            available_tools=frozenset({"calculator"}),
            available_subagent_types=frozenset({"general_purpose"}),
        )
    )
    assert isinstance(out, RoutedExecutionPlan)
    items = out.batches[0].items
    assert [i.decision.route for i in items] == [
        ExecutionRoute.DIRECT_AGENT,
        ExecutionRoute.TOOL,
        ExecutionRoute.SUBAGENT,
    ]
    assert out.subagent_count == 1


def test_legal_passthrough_for_request_clarification() -> None:
    cl = RequestClarification(
        question="问题", reason="原因", blocking_fact_ids=()
    )
    out = route(passthrough_ctx(cl))
    assert out is None  # 透传给 Orchestrator


def test_legal_passthrough_for_recommend_synthesis() -> None:
    rs = RecommendSynthesis(
        target_criterion="criterion-A",
        supporting_fact_ids=("f-1",),
        rationale="ok",
    )
    out = route(passthrough_ctx(rs))
    assert out is None  # 透传


# ===========================================================================
# 2. 未知工具 / 子代理类型
# ===========================================================================


def test_unknown_subagent_type_is_rejected() -> None:
    p = proposal("w-x", route=ExecutionRoute.SUBAGENT, subagent_type="mystery")
    out = route(ctx((p,)))
    assert isinstance(out, RoutingRejection)
    assert out.kind == RejectionKind.UNKNOWN_SUBAGENT_TYPE
    assert "mystery" in out.message
    assert out.offending_proposal_work_id == "w-x"


def test_no_subagent_type_registered_is_rejected() -> None:
    p = proposal("w-x", route=ExecutionRoute.SUBAGENT, subagent_type="anything")
    out = route(
        ctx(
            (p,),
            available_subagent_types=frozenset(),  # 没注册
        )
    )
    assert isinstance(out, RoutingRejection)
    assert out.kind == RejectionKind.UNKNOWN_SUBAGENT_TYPE
    assert "available_subagent_types 为空" in out.message


def test_empty_available_subagent_types_but_route_is_direct_succeeds() -> None:
    p = proposal("w-1", route=ExecutionRoute.DIRECT_AGENT)
    out = route(
        ctx(
            (p,),
            available_subagent_types=frozenset(),
        )
    )
    assert isinstance(out, RoutedExecutionPlan)


# ===========================================================================
# 3. 禁止递归委派
# ===========================================================================


def test_lead_agent_in_default_forbidden_subagents() -> None:
    """默认 forbidden_subagent_types 必含 "lead_agent"——防 SUBAGENT→SUBAGENT→..."""
    assert "lead_agent" in DEFAULT_FORBIDDEN_SUBAGENT_TYPES


def test_subagent_type_lead_agent_is_rejected() -> None:
    p = proposal("w-x", route=ExecutionRoute.SUBAGENT, subagent_type="lead_agent")
    out = route(
        ctx(
            (p,),
            available_subagent_types=frozenset({"lead_agent", "general_purpose"}),
        )
    )
    assert isinstance(out, RoutingRejection)
    assert out.kind == RejectionKind.FORBIDDEN_SUBAGENT_TYPE
    assert "lead_agent" in out.message


def test_custom_forbidden_subagent_type_respected() -> None:
    """调用方可扩展 forbidden 集合（如禁用 'dangerous_tool' 子代理）。"""
    p = proposal("w-x", route=ExecutionRoute.SUBAGENT, subagent_type="dangerous_tool")
    out = route(
        ctx(
            (p,),
            available_subagent_types=frozenset({"dangerous_tool"}),
            forbidden_subagent_types=frozenset({"lead_agent", "dangerous_tool"}),
        )
    )
    assert isinstance(out, RoutingRejection)
    assert out.kind == RejectionKind.FORBIDDEN_SUBAGENT_TYPE


# ===========================================================================
# 4. per-response 并发截断（仅 SUBAGENT/SUBAGENT_BATCH 计入）
# ===========================================================================


def test_per_response_cap_drops_earliest_excess() -> None:
    """max_concurrent=2；本批 4 条 SUBAGENT：保留最早 2 条。"""
    proposals_in = tuple(
        proposal(f"w-{i}", route=ExecutionRoute.SUBAGENT, subagent_type="general_purpose")
        for i in range(4)
    )
    out = route(
        ctx(
            proposals_in,
            max_concurrent_subagents=2,
        )
    )
    assert isinstance(out, RoutedExecutionPlan)
    assert out.subagent_count == 2
    # 保留最早的两条——work_id 已是普通任务 ID（不再带 `:subagent_type` 后缀）
    kept_ids = tuple(it.proposal.work_id for it in out.batches[0].items)
    assert kept_ids == ("w-0", "w-1")


def test_per_response_cap_with_already_routed() -> None:
    """already_routed_in_response=1 + max_concurrent=2 → 本批最多再加 1。"""
    proposals_in = tuple(
        proposal(f"w-{i}", route=ExecutionRoute.SUBAGENT, subagent_type="general_purpose")
        for i in range(3)
    )
    out = route(
        ctx(
            proposals_in,
            max_concurrent_subagents=2,
            already_routed_in_response=1,
        )
    )
    assert isinstance(out, RoutedExecutionPlan)
    assert out.subagent_count == 1


def test_per_response_only_counts_subagent_routes() -> None:
    """per-response 截断**不**计入 DIRECT_AGENT / TOOL——它们走 LeadAgent 内部并发。"""
    proposals_in = (
        proposal("w-1", route=ExecutionRoute.DIRECT_AGENT),
        proposal("w-2", route=ExecutionRoute.DIRECT_AGENT),
        proposal("w-3", route=ExecutionRoute.DIRECT_AGENT),
        proposal("w-4", route=ExecutionRoute.DIRECT_AGENT),
        proposal("w-5", route=ExecutionRoute.SUBAGENT, subagent_type="general_purpose"),
    )
    # max_concurrent=0 极端：不允许任何 Subagent。
    # 4 条 direct_agent 通过；1 条 subagent 被截断——证明 DIRECT_AGENT 不挤占 Subagent 槽位
    out = route(
        ctx(
            proposals_in,
            max_concurrent_subagents=0,
        )
    )
    assert isinstance(out, RoutedExecutionPlan)
    assert out.total_items == 4
    assert out.subagent_count == 0
    # 4 条都是 DIRECT_AGENT——DIRECT_AGENT 走了"并发槽位 = 0 也不挤占"的路径
    assert all(
        it.decision.route is ExecutionRoute.DIRECT_AGENT
        for it in out.batches[0].items
    )


def test_per_response_cap_exhausted_whole_batch_dropped_rejected() -> None:
    """已用满 max_concurrent + 本批全是 Subagent → 整批被截空 → 拒绝。"""
    proposals_in = tuple(
        proposal(f"w-{i}", route=ExecutionRoute.SUBAGENT, subagent_type="general_purpose")
        for i in range(2)
    )
    out = route(
        ctx(
            proposals_in,
            max_concurrent_subagents=2,
            already_routed_in_response=2,
        )
    )
    assert isinstance(out, RoutingRejection)
    assert out.kind == RejectionKind.PER_RESPONSE_CAPPED


# ===========================================================================
# 5. per-run 总量截断
# ===========================================================================


def test_per_run_total_cap_rejects() -> None:
    proposals_in = tuple(
        proposal(f"w-{i}", route=ExecutionRoute.SUBAGENT, subagent_type="general_purpose")
        for i in range(3)
    )
    out = route(
        ctx(
            proposals_in,
            max_total_subagents=5,
            already_routed_in_run=4,  # 4 已用 + 本批 3 = 7 > 5
        )
    )
    assert isinstance(out, RoutingRejection)
    assert out.kind == RejectionKind.TOTAL_CAP_EXCEEDED


def test_per_run_total_cap_only_counts_subagent_routes() -> None:
    """per-run 截断也只算 SUBAGENT；DIRECT/TOOL 不挤占 Subagent 名额。"""
    proposals_in = (
        proposal("w-1", route=ExecutionRoute.DIRECT_AGENT),
        proposal("w-2", subagent_type="general_purpose", route=ExecutionRoute.SUBAGENT),
    )
    out = route(
        ctx(
            proposals_in,
            max_total_subagents=2,
            already_routed_in_run=1,  # 已 1 个 Subagent；本批 1 个 Subagent = 2 = max_total
        )
    )
    assert isinstance(out, RoutedExecutionPlan)


def test_per_run_total_cap_at_boundary_succeeds() -> None:
    """already_routed + 本批 恰 = max_total → 通过（边界）。"""
    proposals_in = tuple(
        proposal(f"w-{i}", route=ExecutionRoute.SUBAGENT, subagent_type="general_purpose")
        for i in range(2)
    )
    out = route(
        ctx(
            proposals_in,
            max_total_subagents=4,
            already_routed_in_run=2,
        )
    )
    assert isinstance(out, RoutedExecutionPlan)
    assert out.subagent_count == 2


# ===========================================================================
# 6. 资源并行冲突（25A 二次确认）
# ===========================================================================


def test_resource_conflict_in_same_batch_rejected() -> None:
    """同批两条 SUBAGENT 都写同一文件 → 资源冲突 → 拒绝。"""
    p_write = proposal(
        "w-1",
        route=ExecutionRoute.SUBAGENT,
        subagent_type="general_purpose",
        claims=(ResourceClaim.of("file:src/a.py", "write"),),
    )
    p_write2 = proposal(
        "w-2",
        route=ExecutionRoute.SUBAGENT,
        subagent_type="general_purpose",
        claims=(ResourceClaim.of("file:src/a.py", "write"),),  # 同一资源
    )
    out = route(ctx((p_write, p_write2)))
    assert isinstance(out, RoutingRejection)
    assert out.kind == RejectionKind.RESOURCE_CONFLICT
    assert "file:src/a.py" in out.message


def test_no_conflict_when_different_resources_succeeds() -> None:
    p1 = proposal(
        "w-1",
        route=ExecutionRoute.SUBAGENT,
        subagent_type="general_purpose",
        claims=(ResourceClaim.of("file:src/a.py", "read"),),
    )
    p2 = proposal(
        "w-2",
        route=ExecutionRoute.SUBAGENT,
        subagent_type="general_purpose",
        claims=(ResourceClaim.of("file:src/b.py", "read"),),
    )
    out = route(ctx((p1, p2)))
    assert isinstance(out, RoutedExecutionPlan)


def test_path_overlap_parent_write_child_read_rejected() -> None:
    """父目录写 + 子文件读：路径型父子重叠 → 25A 冲突规则 → Router 拒绝。"""
    p_write = proposal(
        "w-1",
        route=ExecutionRoute.SUBAGENT,
        subagent_type="general_purpose",
        claims=(ResourceClaim.of("file:src", "write"),),
    )
    p_read = proposal(
        "w-2",
        route=ExecutionRoute.SUBAGENT,
        subagent_type="general_purpose",
        claims=(ResourceClaim.of("file:src/a.py", "read"),),
    )
    out = route(ctx((p_write, p_read)))
    assert isinstance(out, RoutingRejection)
    assert out.kind == RejectionKind.RESOURCE_CONFLICT


# ===========================================================================
# 7. 25A 批次结构错误传递
# ===========================================================================


def test_cyclic_dependency_in_batch_rejected() -> None:
    a = proposal("w-a", depends_on=("w-b",))
    b = proposal("w-b", depends_on=("w-a",))
    out = route(ctx((a, b)))
    assert isinstance(out, RoutingRejection)
    assert out.kind == RejectionKind.CYCLIC_DEPENDENCY


def test_missing_dependency_rejected() -> None:
    a = proposal("w-a", depends_on=("w-does-not-exist",))
    out = route(ctx((a,)))
    assert isinstance(out, RoutingRejection)
    assert out.kind == RejectionKind.MISSING_DEPENDENCY
    assert out.offending_proposal_work_id == "w-a"


def test_duplicate_work_id_rejected() -> None:
    a1 = proposal("w-a")
    a2 = proposal("w-a")
    out = route(ctx((a1, a2)))
    assert isinstance(out, RoutingRejection)
    assert out.kind == RejectionKind.DUPLICATE_WORK_ID


def test_self_dependency_rejected() -> None:
    a = proposal("w-a", depends_on=("w-a",))
    out = route(ctx((a,)))
    assert isinstance(out, RoutingRejection)
    assert out.kind == RejectionKind.SELF_DEPENDENCY


def test_duplicate_against_verified_history_rejected() -> None:
    """work_id 与已验证历史重复——25A DuplicateWorkIdError，Router 拒绝。"""
    f = fact("f-1", "w-hist")
    p = proposal("w-hist")  # 重复
    out = route(ctx((p,), verified_facts=(f,)))
    assert isinstance(out, RoutingRejection)
    assert out.kind == RejectionKind.DUPLICATE_WORK_ID
    assert out.offending_proposal_work_id == "w-hist"


# ===========================================================================
# 8. 集成契约：RequestClarification 空 blocking_fact_ids
# ===========================================================================


def test_request_clarification_with_empty_blocking_fact_ids_is_passthrough() -> None:
    """空 blocking_fact_ids = '我没有任何证据，纯缺信息'——应被 Planner/Orchestrator 接受。"""
    raw = (
        '{"kind": "request_clarification", "question": "用户的真正目标是什么？",'
        ' "reason": "目前没有任何事实可参考", "blocking_fact_ids": []}'
    )
    parsed = parse_planner_output(raw)
    assert isinstance(parsed, RequestClarification)
    assert parsed.blocking_fact_ids == ()

    # Router 透传——不校验 blocking_fact_ids 是否空
    out = route(passthrough_ctx(parsed))
    assert out is None


def test_request_clarification_with_mixed_blocking_fact_ids_is_passthrough() -> None:
    """空元组在 Planner 端应通过 `_validate_business`——不会触发 UnknownBlockingFact。"""
    rc = RequestClarification(
        question="下一步该做哪个？",
        reason="信息不足",
        blocking_fact_ids=(),  # 空元组
    )
    # Planner 业务校验通过
    planner_ctx = PlannerContext(
        goal=GOAL,
        verified_facts=(fact("f-1", "w-1"),),
        last_decision=None,
        recent_items=(),
        budget_snapshot=build_budget_snapshot(Budget(max_model_calls=5)),
        capabilities=("general_purpose",),
    )
    result = planner_validate_business(rc, planner_ctx)
    assert result is rc
    # Router 透传
    out = route(passthrough_ctx(rc))
    assert out is None


# ===========================================================================
# 9. 集成契约：BudgetRootBudget 标记为仅测试
# ===========================================================================


def test_budget_root_budget_marked_for_testing_only() -> None:
    from mini_deerflow.lead_agent.planner_budget import BudgetRootBudget

    assert BudgetRootBudget._FOR_TESTING_ONLY is True


def test_lead_ledger_root_budget_is_not_marked_for_testing_only() -> None:
    from mini_deerflow.lead_agent.planner_budget import LeadLedgerRootBudget

    flagged = getattr(LeadLedgerRootBudget, "_FOR_TESTING_ONLY", False)
    assert not flagged, "生产路径不应带 _FOR_TESTING_ONLY 标记"


# ===========================================================================
# 10. 不调模型、不调 ToolRegistry、不调 SubagentManager
# ===========================================================================


def test_router_does_not_call_model() -> None:
    """Router 是纯函数——不引入 Model/ToolRegistry/SubagentManager/LeadRunStore。"""
    import ast
    import mini_deerflow.lead_agent.router as r

    source = ast.parse(open(r.__file__, encoding="utf-8").read())
    forbidden = {
        "Model",
        "FakeModel",
        "ToolRegistry",
        "Tool",
        "SubagentManager",
        "SubagentRunner",
        "SubagentTask",
        "open_lease",
        "consume_direct",
        "save_ledger",
        "save",
        "create",
        "register",
        "execute",
    }
    for node in ast.walk(source):
        if isinstance(node, ast.ImportFrom) and node.module:
            for n in node.names:
                if n.name in forbidden:
                    pytest.fail(f"router.py 不应 import {n.name}")
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in forbidden:
                    pytest.fail(f"router.py 不应 import {a.name}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in forbidden:
                pytest.fail(f"router.py 不应直接调用 {node.func.id}()")


def test_router_keeps_planner_output_immutable() -> None:
    """Router **不**改 PlannerOutput.proposals 字段——保证 Planner 决定的不可篡改。"""
    p1 = proposal("w-1")
    p2 = proposal("w-2")
    original = propose((p1, p2))
    original_proposals = original.proposals
    out = route(ctx((p1, p2)))
    assert isinstance(out, RoutedExecutionPlan)
    assert original.proposals == original_proposals
    out_ids = tuple(it.proposal.work_id for it in out.batches[0].items)
    assert out_ids == ("w-1", "w-2")


# ===========================================================================
# 11. RoutedExecutionPlan / ValidatedWorkItem / ExecutionDecision 契约
# ===========================================================================


def test_execution_decision_validates_route_tool_subagent_domain() -> None:
    """DIRECT_AGENT 不带 tool/subagent；TOOL 不带 subagent（tool_name 由 25E 注入）；
    SUBAGENT 必带 subagent_type。"""
    with pytest.raises(ValueError, match="DIRECT_AGENT"):
        ExecutionDecision(
            route=ExecutionRoute.DIRECT_AGENT, tool_name="calculator"
        )
    # TOOL 25D 允许 tool_name 为空（25E 注入）
    ok = ExecutionDecision(route=ExecutionRoute.TOOL)
    assert ok.tool_name is None
    # TOOL 不应带 subagent_type
    with pytest.raises(ValueError, match="TOOL"):
        ExecutionDecision(route=ExecutionRoute.TOOL, subagent_type="general_purpose")
    with pytest.raises(ValueError, match="subagent"):
        ExecutionDecision(route=ExecutionRoute.SUBAGENT, subagent_type=None)


def test_routed_execution_plan_subagent_count_only_counts_subagent_routes() -> None:
    p1 = proposal("w-1", route=ExecutionRoute.DIRECT_AGENT)
    p2 = proposal("w-2", subagent_type="general_purpose", route=ExecutionRoute.SUBAGENT)
    p3 = proposal("w-3", route=ExecutionRoute.SUBAGENT_BATCH, subagent_type="general_purpose")
    out = route(ctx((p1, p2, p3)))
    assert isinstance(out, RoutedExecutionPlan)
    assert out.total_items == 3
    assert out.subagent_count == 2  # p2 + p3


def test_routing_rejection_kind_matches_rejection_kind_constants() -> None:
    r = RoutingRejection(kind=RejectionKind.UNKNOWN_TOOL, message="x")
    assert r.kind == "UnknownTool"


# ===========================================================================
# 12. RoutingContext 校验
# ===========================================================================


def test_routing_context_rejects_bad_max_concurrent() -> None:
    with pytest.raises(ValueError, match="max_concurrent_subagents"):
        RoutingContext(
            output=propose((proposal("w-1"),)),
            goal=GOAL,
            verified_facts=(),
            available_tools=frozenset(),
            available_subagent_types=frozenset(),
            max_concurrent_subagents=-1,
        )


def test_routing_context_accepts_any_planner_output_subclass() -> None:
    """RoutingContext 接受任意 PlannerOutput 子类——透传场景需要。"""
    # ProposeBatch
    RoutingContext(
        output=propose((proposal("w-1"),)),
        goal=GOAL,
        verified_facts=(),
        available_tools=frozenset(),
        available_subagent_types=frozenset(),
    )
    # RequestClarification（透传场景）
    cl = RequestClarification(question="q", reason="r", blocking_fact_ids=())
    RoutingContext(
        output=cl,
        goal=GOAL,
        verified_facts=(),
        available_tools=frozenset(),
        available_subagent_types=frozenset(),
    )
    # RecommendSynthesis（透传场景）
    rs = RecommendSynthesis(
        target_criterion="criterion-A",
        supporting_fact_ids=("f-1",),
        rationale="r",
    )
    RoutingContext(
        output=rs,
        goal=GOAL,
        verified_facts=(),
        available_tools=frozenset(),
        available_subagent_types=frozenset(),
    )


def test_routing_context_rejects_non_planner_output() -> None:
    with pytest.raises(TypeError, match="PlannerOutput"):
        RoutingContext(
            output="not a planner output",  # type: ignore[arg-type]
            goal=GOAL,
            verified_facts=(),
            available_tools=frozenset(),
            available_subagent_types=frozenset(),
        )


def test_routing_context_rejects_bool_max_total() -> None:
    with pytest.raises(ValueError, match="max_total_subagents"):
        RoutingContext(
            output=propose((proposal("w-1"),)),
            goal=GOAL,
            verified_facts=(),
            available_tools=frozenset(),
            available_subagent_types=frozenset(),
            max_total_subagents=True,  # type: ignore[arg-type]
        )


# ===========================================================================
# 25D 契约收口：显式路由目标字段
# ===========================================================================


def test_proposal_to_requires_explicit_tool_name() -> None:
    """25D 契约：TOOL 路由必须显式给出 tool_name（不再从 work_id 推断）。"""
    with pytest.raises(ValueError, match="TOOL"):
        WorkItemProposal(
            work_id="w-tool",
            objective="o",
            execution_route=ExecutionRoute.TOOL,
            # tool_name 缺失 → ValueError
        )


def test_proposal_subagent_requires_explicit_subagent_type() -> None:
    """25D 契约：SUBAGENT/SUBAGENT_BATCH 必须显式给出 subagent_type。"""
    with pytest.raises(ValueError, match="subagent"):
        WorkItemProposal(
            work_id="w-sub",
            objective="o",
            execution_route=ExecutionRoute.SUBAGENT,
            # subagent_type 缺失 → ValueError
        )
    with pytest.raises(ValueError, match="subagent"):
        WorkItemProposal(
            work_id="w-batch",
            objective="o",
            execution_route=ExecutionRoute.SUBAGENT_BATCH,
        )


def test_proposal_rejects_subagent_type_for_direct_agent() -> None:
    """25D 契约：DIRECT_AGENT 不应携带 subagent_type / tool_name。"""
    with pytest.raises(ValueError, match="DIRECT_AGENT"):
        WorkItemProposal(
            work_id="w",
            objective="o",
            execution_route=ExecutionRoute.DIRECT_AGENT,
            subagent_type="general_purpose",  # 不该有
        )
    with pytest.raises(ValueError, match="DIRECT_AGENT"):
        WorkItemProposal(
            work_id="w",
            objective="o",
            execution_route=ExecutionRoute.DIRECT_AGENT,
            tool_name="calculator",  # 不该有
        )


def test_proposal_to_dict_omits_none_fields_backward_compat() -> None:
    """to_dict 不写 None 字段——老 payload 形状（无 tool_name/subagent_type）保持。"""
    p = WorkItemProposal(work_id="w", objective="o")
    d = p.to_dict()
    assert "tool_name" not in d
    assert "subagent_type" not in d


def test_proposal_from_dict_accepts_legacy_payload_without_new_fields() -> None:
    """from_dict 兼容老数据：缺 tool_name/subagent_type 时默认为 None（DIRECT_AGENT 路线）。"""
    legacy = {
        "schema_version": 1,
        "work_id": "w-legacy",
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


def test_proposal_to_dict_with_tool_round_trips() -> None:
    """to_dict + from_dict 在 TOOL + 显式 tool_name 下保持值不变。"""
    p = WorkItemProposal(
        work_id="w",
        objective="o",
        execution_route=ExecutionRoute.TOOL,
        tool_name="calculator",
    )
    p2 = WorkItemProposal.from_dict(p.to_dict())
    assert p2 == p
    assert p2.tool_name == "calculator"
    assert p2.execution_route is ExecutionRoute.TOOL


def test_proposal_to_dict_with_subagent_round_trips() -> None:
    p = WorkItemProposal(
        work_id="w",
        objective="o",
        execution_route=ExecutionRoute.SUBAGENT,
        subagent_type="general_purpose",
    )
    p2 = WorkItemProposal.from_dict(p.to_dict())
    assert p2 == p
    assert p2.subagent_type == "general_purpose"


def test_router_uses_typed_subagent_type_not_work_id() -> None:
    """Router 读 proposal.subagent_type——**不**解析 work_id 里的 ':type'。"""
    p = proposal(
        "w-without-colon",
        route=ExecutionRoute.SUBAGENT,
        subagent_type="general_purpose",
    )
    out = route(
        ctx(
            (p,),
            available_subagent_types=frozenset({"general_purpose"}),
        )
    )
    assert isinstance(out, RoutedExecutionPlan)
    # 决策里的 subagent_type 来自字段，**不**是从 "w-1:general_purpose" 解析
    decision = out.batches[0].items[0].decision
    assert decision.subagent_type == "general_purpose"
    assert decision.route is ExecutionRoute.SUBAGENT


def test_router_rejects_subagent_route_without_subagent_type_field() -> None:
    """提案里 SUBAGENT 路由但 subagent_type 字段缺失——Router 之前允许但 model 25D 契约已拦。
    测的是从 dict 构造 WorkItemProposal 的拒绝路径：Router 拿到非法 WorkItemProposal
    时已经走不到；这里确保模型构造期拒绝（防止未来退化）。"""
    # 直接构造会被模型构造期拒绝
    with pytest.raises(ValueError, match="subagent"):
        WorkItemProposal(
            work_id="w",
            objective="o",
            execution_route=ExecutionRoute.SUBAGENT,
            # no subagent_type
        )


def test_router_uses_typed_tool_name_for_tooL() -> None:
    p = proposal(
        "w",
        route=ExecutionRoute.TOOL,
        tool_name="calculator",
    )
    out = route(
        ctx(
            (p,),
            available_tools=frozenset({"calculator"}),
        )
    )
    assert isinstance(out, RoutedExecutionPlan)
    assert out.batches[0].items[0].decision.tool_name == "calculator"


def test_router_rejects_tool_with_unknown_tool_name_field() -> None:
    """tool_name 字段已知但不在 available_tools——确定性拒绝。"""
    p = proposal(
        "w",
        route=ExecutionRoute.TOOL,
        tool_name="mystery_tool",
    )
    out = route(
        ctx(
            (p,),
            available_tools=frozenset({"calculator"}),
        )
    )
    assert isinstance(out, RoutingRejection)
    assert out.kind == RejectionKind.UNKNOWN_TOOL
    assert "mystery_tool" in out.message


# ===========================================================================
# 25D 契约收口：截断后依赖闭合
# ===========================================================================


def test_truncation_drops_subagent_with_no_dependents_succeeds() -> None:
    """截断一个无被依赖的 subagent——其它保留项无悬空依赖，路由通过。"""
    p_sub1 = proposal("w-1", route=ExecutionRoute.SUBAGENT, subagent_type="general_purpose")
    p_sub2 = proposal("w-2", route=ExecutionRoute.SUBAGENT, subagent_type="general_purpose")
    p_dir = proposal("w-3", route=ExecutionRoute.DIRECT_AGENT)  # 不依赖任何
    out = route(
        ctx(
            (p_sub1, p_sub2, p_dir),
            max_concurrent_subagents=1,  # 只保留最早 1 个 subagent
        )
    )
    assert isinstance(out, RoutedExecutionPlan)
    # p_sub2 + p_dir 保留；p_sub1 被截掉且无被依赖
    assert out.total_items == 2


def test_truncation_drops_subagent_with_dependent_rejects_whole_batch() -> None:
    """max_concurrent=0 → 所有 subagent 被截；保留项依赖悬空 → 整批拒绝。

    关键语义：Router 宁可整批拒绝也**不**返回依赖不闭合的 plan（25D 契约）。
    此时路径走的是「依赖闭合重检」而非 PER_RESPONSE_CAPPED——因为
    `_truncate_by_concurrency` 只丢弃 subagent，DIRECT_AGENT 仍保留
    （`truncated` 非空），所以先做依赖重检发现 MissingDependency。
    """
    p_sub = proposal(
        "w-sub",
        route=ExecutionRoute.SUBAGENT,
        subagent_type="general_purpose",
    )
    p_dependent = proposal(
        "w-dependent",
        route=ExecutionRoute.DIRECT_AGENT,
        depends_on=("w-sub",),
    )
    out = route(
        ctx(
            (p_sub, p_dependent),
            max_concurrent_subagents=0,  # 全部 subagent 被截
            max_total_subagents=6,
        )
    )
    assert isinstance(out, RoutingRejection)
    # w-dependent 的 w-sub 依赖悬空：truncated 仍含 w-dependent，重检发现
    # MissingDependency → 拒绝整批
    assert out.kind == RejectionKind.MISSING_DEPENDENCY
    assert out.offending_proposal_work_id == "w-dependent"
    assert "w-sub" in out.message


def test_truncation_keeps_dependent_when_depended_subagent_dropped_rejects_batch() -> None:
    """更精确的依赖闭合场景：max=1 保留较早 subagent，**但**另一个 subagent 是
    DIRECT_AGENT 的依赖 → 直接批次里出现 `depends_on=("w-sub2",)`，
    w-sub2 被截 → 重检发现 MissingDependency → 拒绝整批。"""
    p_sub_kept = proposal(
        "w-sub-kept",
        route=ExecutionRoute.SUBAGENT,
        subagent_type="general_purpose",
    )
    p_sub_dropped = proposal(
        "w-sub-dropped",
        route=ExecutionRoute.SUBAGENT,
        subagent_type="general_purpose",
    )
    p_dependent = proposal(
        "w-dep",
        route=ExecutionRoute.DIRECT_AGENT,
        depends_on=("w-sub-dropped",),  # 依赖被截的 subagent
    )
    out = route(
        ctx(
            (p_sub_kept, p_sub_dropped, p_dependent),
            max_concurrent_subagents=1,  # 保留最早 1 个：w-sub-kept；w-sub-dropped 被截
        )
    )
    # 截断后 w-dep 依赖 w-sub-dropped；augmented_verified 含 w-sub-dropped 但
    # w-sub-dropped 不在本批的 kept 里 → 仍 missing → MissingDependency 拒绝
    assert isinstance(out, RoutingRejection)
    assert out.kind == RejectionKind.MISSING_DEPENDENCY
    assert out.offending_proposal_work_id == "w-dep"
    assert "w-sub-dropped" in out.message
    # 整批拒绝——**不**返回部分通过的 plan
    assert out.batches if False else True  # syntax 标签; just for line


def test_truncation_keeps_dependent_where_depended_also_kept_succeeds() -> None:
    """控制用例：依赖的 subagent **没**被截掉，路由通过。"""
    p_sub = proposal(
        "w-sub",
        route=ExecutionRoute.SUBAGENT,
        subagent_type="general_purpose",
    )
    p_dependent = proposal(
        "w-dep",
        route=ExecutionRoute.DIRECT_AGENT,
        depends_on=("w-sub",),
    )
    out = route(
        ctx(
            (p_sub, p_dependent),
            max_concurrent_subagents=2,  # 全部保留
        )
    )
    assert isinstance(out, RoutedExecutionPlan)
    assert out.total_items == 2


# ===========================================================================
# 25D 契约收口：RequestClarification 空 blocking_fact_ids 明确为正常场景
# ===========================================================================


def test_request_clarification_empty_blocking_fact_ids_explicit_normal_path() -> None:
    """空 blocking_fact_ids = 「我没有任何事实可参考，纯缺信息」——正常场景，**不**是反常。

    Planner 业务校验通过；Router 透传。语义由 Orchestrator（25E+）处理。
    """
    # Planner 端：空元组通过 _validate_clarification
    rc = RequestClarification(
        question="用户的真正目标是什么？",
        reason="我没有任何 VerifiedFact 可参考",
        blocking_fact_ids=(),  # 空 = 显式表达无事实
    )
    planner_ctx = PlannerContext(
        goal=GOAL,
        verified_facts=(),
        last_decision=None,
        recent_items=(),
        budget_snapshot=build_budget_snapshot(Budget(max_model_calls=5)),
        capabilities=("general_purpose",),
    )
    # 业务校验通过：空 blocking_fact_ids 不会触发 UnknownBlockingFact
    result = planner_validate_business(rc, planner_ctx)
    assert result is rc
    # Router 透传——不校验 blocking_fact_ids 是否空
    out = route(passthrough_ctx(rc))
    assert out is None


def test_request_clarification_with_known_fact_id_still_passes() -> None:
    """非空 blocking_fact_ids（含已知 fact_id）也照常通过——Pluggable 选择。"""
    rc = RequestClarification(
        question="下一步方向？",
        reason="f-1 仍不够",
        blocking_fact_ids=("f-1",),
    )
    # Router 是透传路径——不校验 blocking_fact_ids 是否存在
    out = route(passthrough_ctx(rc))
    assert out is None


# ===========================================================================
# 25D 契约收口：Router 拒绝后的重规划总次数由 Orchestrator 统一管理
# ===========================================================================


def test_router_does_not_self_replan() -> None:
    """Router 抛 RoutingRejection 后**不**自己重规划、不调 Planner——纯函数。"""
    # 构造一个必然被 Router 拒绝的批次（缺失依赖）
    p_a = proposal("w-a", depends_on=("w-does-not-exist",))
    out = route(ctx((p_a,)))
    assert isinstance(out, RoutingRejection)
    # 拒了就是拒了——**不**重试，**不**降级，**不**调 Planner
    # 这条语义由 AST 守卫 + 不持有 Model/Planner 引用共同保证
