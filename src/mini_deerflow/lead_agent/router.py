"""Phase 25D LeadRouter：策略建议与执行权限分离。

## 设计目标

`Router` 是 Planner 与 Executor（25E）之间的**纯函数**：
- 接 `ProposeBatch`，产 `RoutedExecutionPlan` 或 `RoutingRejection`
- **不**调模型、**不**调 `ToolRegistry.execute`、**不**调 `SubagentManager.submit`、
  **不**开 `BudgetLease`、**不**改 `LeadRunState`
- **复用** 25A `validate_batch` + `validate_parallel_safe`，**不**重写
- `RequestClarification` / `RecommendSynthesis` **透传**（不归 Router 决定
  "要不要追问" / "要不要综合"——这是 25F / Orchestrator 的职责）

## 拒绝的处理

`RoutingRejection` 由 Orchestrator（25D 后续 / 25E 入口）接收。
Router **不**擅自改成 `RequestClarification`、**不**重规划、
**不**改 `LeadRunState`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, FrozenSet

from .errors import (
    BatchValidationError,
    CyclicDependencyError,
    DuplicateWorkIdError,
    EmptyGoalFieldError,
    MissingDependencyError,
    ResourceConflictError,
    SelfDependencyError,
)
from .models import (
    GoalContract,
    VerifiedFact,
    WorkItemProposal,
)
from .planner import (
    ProposeBatch,
    RecommendSynthesis,
    RequestClarification,
)
from .state_machine import ExecutionRoute
from .validation import validate_batch, validate_parallel_safe

__all__ = [
    "DEFAULT_FORBIDDEN_SUBAGENT_TYPES",
    "ExecutionDecision",
    "RoutedExecutionPlan",
    "RoutingContext",
    "RoutingRejection",
    "ValidatedBatch",
    "ValidatedWorkItem",
    "route",
]


# ---------------------------------------------------------------------------
# 拒绝类型
# ---------------------------------------------------------------------------


# Router 拒绝的 kind 取值（确定性可枚举；用于 Orchestrator 决策）
class RejectionKind:
    """Router 拒绝原因分类——`kind` 字段取这些字符串之一。

    Orchestrator 拿到 `RoutingRejection` 后按 `kind` 决定下一步：
    - 结构/语义错（`Missing/Cyclic/Duplicate/Self/EmptyGoal`） → 反馈 Planner 重规划
    - 域/配置错（`UnknownTool/UnknownSubagentType/ForbiddenSubagentType`） →
      改 Orchestrator 配置，**不**走 Planner
    - 资源/容量错（`ResourceConflict/TotalCapExceeded/PerResponseCapped`） →
      降级 / 拆分 / 降并发，**不**走 Planner
    """

    MISSING_DEPENDENCY: ClassVar[str] = "MissingDependency"
    CYCLIC_DEPENDENCY: ClassVar[str] = "CyclicDependency"
    DUPLICATE_WORK_ID: ClassVar[str] = "DuplicateWorkId"
    SELF_DEPENDENCY: ClassVar[str] = "SelfDependency"
    EMPTY_GOAL: ClassVar[str] = "EmptyGoalField"

    UNKNOWN_TOOL: ClassVar[str] = "UnknownTool"
    UNKNOWN_SUBAGENT_TYPE: ClassVar[str] = "UnknownSubagentType"
    FORBIDDEN_SUBAGENT_TYPE: ClassVar[str] = "ForbiddenSubagentType"

    RESOURCE_CONFLICT: ClassVar[str] = "ResourceConflict"
    TOTAL_CAP_EXCEEDED: ClassVar[str] = "TotalCapExceeded"
    PER_RESPONSE_CAPPED: ClassVar[str] = "PerResponseCapped"

    # 路由域不一致（例如 `direct_agent` 上却给了 tool_name）
    INVALID_ROUTE_DOMAIN: ClassVar[str] = "InvalidRouteDomain"


# 默认禁用的子代理类型——防递归委派（官方也有类似禁 `task` 等）
# "lead_agent" 必须禁：否则 SUBAGENT → SUBAGENT → ... 会把 Run 倒回 Planner
DEFAULT_FORBIDDEN_SUBAGENT_TYPES: FrozenSet[str] = frozenset({"lead_agent"})


# ---------------------------------------------------------------------------
# 上下文
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoutingContext:
    """单次 `route()` 调用的全部输入（frozen）。

    `output` 可以是任意 `PlannerOutput` 子类——`ProposeBatch` 走完整路由链；
    `RequestClarification` / `RecommendSynthesis` 由 Router **透传**（不归
    Router 校验"是否追问" / "是否综合"）。
    """

    output: ProposeBatch | RequestClarification | RecommendSynthesis
    goal: GoalContract
    verified_facts: tuple[VerifiedFact, ...]
    available_tools: frozenset[str]
    available_subagent_types: frozenset[str]
    forbidden_subagent_types: frozenset[str] = DEFAULT_FORBIDDEN_SUBAGENT_TYPES
    max_concurrent_subagents: int = 3
    max_total_subagents: int = 6
    already_routed_in_response: int = 0
    already_routed_in_run: int = 0
    capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(
            self.output, (ProposeBatch, RequestClarification, RecommendSynthesis)
        ):
            raise TypeError(
                f"output 必须是 PlannerOutput 子类之一，"
                f"收到 {type(self.output).__name__}"
            )
        if not isinstance(self.goal, GoalContract):
            raise TypeError("goal 必须是 GoalContract")
        for f in self.verified_facts:
            if not isinstance(f, VerifiedFact):
                raise TypeError("verified_facts 元素必须是 VerifiedFact")
        if not isinstance(self.available_tools, frozenset):
            raise TypeError("available_tools 必须是 frozenset[str]")
        if not isinstance(self.available_subagent_types, frozenset):
            raise TypeError(
                "available_subagent_types 必须是 frozenset[str]"
            )
        if not isinstance(self.forbidden_subagent_types, frozenset):
            raise TypeError(
                "forbidden_subagent_types 必须是 frozenset[str]"
            )
        if (
            isinstance(self.max_concurrent_subagents, bool)
            or not isinstance(self.max_concurrent_subagents, int)
            or self.max_concurrent_subagents < 0
        ):
            raise ValueError(
                f"max_concurrent_subagents 必须为非负整数，收到 {self.max_concurrent_subagents!r}"
            )
        if (
            isinstance(self.max_total_subagents, bool)
            or not isinstance(self.max_total_subagents, int)
            or self.max_total_subagents < 0
        ):
            raise ValueError(
                f"max_total_subagents 必须为非负整数，收到 {self.max_total_subagents!r}"
            )
        if (
            isinstance(self.already_routed_in_response, bool)
            or not isinstance(self.already_routed_in_response, int)
            or self.already_routed_in_response < 0
        ):
            raise ValueError("already_routed_in_response 必须为非负整数")
        if (
            isinstance(self.already_routed_in_run, bool)
            or not isinstance(self.already_routed_in_run, int)
            or self.already_routed_in_run < 0
        ):
            raise ValueError("already_routed_in_run 必须为非负整数")


# ---------------------------------------------------------------------------
# 决策 / 输出
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionDecision:
    """单条 WorkItem 的路由决策。"""

    route: ExecutionRoute
    # `DIRECT_AGENT` 二者皆 None；`TOOL` 必须有 tool_name；
    # `SUBAGENT` / `SUBAGENT_BATCH` 必须有 subagent_type
    tool_name: str | None = None
    subagent_type: str | None = None
    # 同组 = 同一批并行（25E 据此决定并发槽）。每个 proposal 单独一个组。
    parallel_group_id: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.route, ExecutionRoute):
            raise TypeError(
                f"route 必须是 ExecutionRoute，收到 {type(self.route).__name__}"
            )
        if self.route is ExecutionRoute.DIRECT_AGENT:
            if self.tool_name is not None or self.subagent_type is not None:
                raise ValueError(
                    "DIRECT_AGENT 不应携带 tool_name/subagent_type"
                )
        elif self.route is ExecutionRoute.TOOL:
            # 25D 阶段 WorkItemProposal 不携带 tool_name（25A 字段无），
            # 实际 tool 绑定由 25E Orchestrator 在 WorkItem 转 SubagentTask
            # 时注入。这里 tool_name 可空，**不**做非空断言。
            if self.subagent_type is not None:
                raise ValueError("TOOL 路由不应携带 subagent_type")
        elif self.route in (
            ExecutionRoute.SUBAGENT,
            ExecutionRoute.SUBAGENT_BATCH,
        ):
            if not self.subagent_type:
                raise ValueError(
                    f"{self.route.value} 路由必须给出 subagent_type"
                )
            if self.tool_name is not None:
                raise ValueError(
                    f"{self.route.value} 路由不应携带 tool_name"
                )
        if isinstance(self.parallel_group_id, bool) or not isinstance(
            self.parallel_group_id, int
        ) or self.parallel_group_id < 0:
            raise ValueError("parallel_group_id 必须为非负整数")


@dataclass(frozen=True)
class ValidatedWorkItem:
    """单条已校验 WorkItem = 原提案 + 路由决策。"""

    proposal: WorkItemProposal
    decision: ExecutionDecision


@dataclass(frozen=True)
class ValidatedBatch:
    """通过路由校验的一批；每个 WorkItem 已绑定 ExecutionDecision。"""

    items: tuple[ValidatedWorkItem, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple) or not self.items:
            raise ValueError("items 必须是非空 tuple")
        for it in self.items:
            if not isinstance(it, ValidatedWorkItem):
                raise TypeError("items 元素必须是 ValidatedWorkItem")


@dataclass(frozen=True)
class RoutedExecutionPlan:
    """Router 通过校验的产物——下游 Executor（25E）按 `batches` 派发。"""

    batches: tuple[ValidatedBatch, ...]
    rationale: str

    def __post_init__(self) -> None:
        if not isinstance(self.batches, tuple) or not self.batches:
            raise ValueError("batches 必须是非空 tuple")
        for b in self.batches:
            if not isinstance(b, ValidatedBatch):
                raise TypeError("batches 元素必须是 ValidatedBatch")
        if not self.rationale.strip():
            raise ValueError("rationale 不能为空")

    @property
    def total_items(self) -> int:
        return sum(len(b.items) for b in self.batches)

    @property
    def subagent_count(self) -> int:
        """只统计 SUBAGENT / SUBAGENT_BATCH——供 Orchestrator 计入 max_total。"""
        n = 0
        for b in self.batches:
            for it in b.items:
                if it.decision.route in (
                    ExecutionRoute.SUBAGENT,
                    ExecutionRoute.SUBAGENT_BATCH,
                ):
                    n += 1
        return n

    def all_proposals(self) -> tuple[WorkItemProposal, ...]:
        out: list[WorkItemProposal] = []
        for b in self.batches:
            for it in b.items:
                out.append(it.proposal)
        return tuple(out)


@dataclass(frozen=True)
class RoutingRejection:
    """确定性拒绝。`kind` 字段取 `RejectionKind.*` 之一。"""

    kind: str
    message: str
    # 出错的提案 work_id（如可定位）；None 表示整批失败
    offending_proposal_work_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("kind 必须是非空字符串")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("message 必须是非空字符串")
        if self.offending_proposal_work_id is not None and not isinstance(
            self.offending_proposal_work_id, str
        ):
            raise TypeError("offending_proposal_work_id 必须是 str 或 None")


# ---------------------------------------------------------------------------
# 核心：route()
# ---------------------------------------------------------------------------


def route(
    ctx: RoutingContext,
) -> RoutedExecutionPlan | RoutingRejection | None:
    """对 Planner 的 `PlannerOutput` 做路由校验。

    - `ProposeBatch` → 走完整路由链；返回 `RoutedExecutionPlan` 或 `RoutingRejection`
    - `RequestClarification` / `RecommendSynthesis` → 直接返回 `None`（**透传**）

    Router **不**调模型、**不**执行工具、**不**调 SubagentManager、
    **不**开 BudgetLease、**不**改 LeadRunState。
    """
    # 透传：只有 ProposeBatch 是可路由的
    if isinstance(ctx.output, (RequestClarification, RecommendSynthesis)):
        return None
    if not isinstance(ctx.output, ProposeBatch):
        # 未来若增加 PlannerOutput 子类且未在此处显式处理
        return RoutingRejection(
            kind="InvalidRouteDomain",
            message=f"Router 不接受 {type(ctx.output).__name__}",
        )

    # 1) 25A 批次结构校验
    verified_work_ids = frozenset(f.work_id for f in ctx.verified_facts)
    try:
        validate_batch(
            ctx.output.proposals,
            verified_work_ids=verified_work_ids,
            goal=ctx.goal,
        )
    except EmptyGoalFieldError as exc:
        return RoutingRejection(
            kind=RejectionKind.EMPTY_GOAL, message=str(exc)
        )
    except DuplicateWorkIdError as exc:
        return RoutingRejection(
            kind=RejectionKind.DUPLICATE_WORK_ID,
            message=str(exc),
            offending_proposal_work_id=exc.work_id,
        )
    except SelfDependencyError as exc:
        return RoutingRejection(
            kind=RejectionKind.SELF_DEPENDENCY, message=str(exc)
        )
    except MissingDependencyError as exc:
        return RoutingRejection(
            kind=RejectionKind.MISSING_DEPENDENCY,
            message=str(exc),
            offending_proposal_work_id=exc.work_id,
        )
    except CyclicDependencyError as exc:
        return RoutingRejection(
            kind=RejectionKind.CYCLIC_DEPENDENCY, message=str(exc)
        )
    except BatchValidationError as exc:
        # 兜底：未来 25A 新增错误子类时不让 Router 静默吞掉
        return RoutingRejection(
            kind=type(exc).__name__, message=str(exc)
        )

    # 2) ExecutionRoute × 工具/子代理域一致
    for proposal in ctx.output.proposals:
        rejection = _check_route_domain(
            proposal, ctx.available_tools, ctx.available_subagent_types,
            ctx.forbidden_subagent_types,
        )
        if rejection is not None:
            return rejection

    # 3) per-response 并发截断（仅 SUBAGENT / SUBAGENT_BATCH 计入；仿官方）
    truncated, _dropped = _truncate_by_concurrency(
        ctx.output.proposals,
        already_routed_in_response=ctx.already_routed_in_response,
        max_concurrent=ctx.max_concurrent_subagents,
    )
    if not truncated:
        return RoutingRejection(
            kind=RejectionKind.PER_RESPONSE_CAPPED,
            message=(
                f"本响应内已路由 {ctx.already_routed_in_response} 个 Subagent"
                f"，max_concurrent={ctx.max_concurrent_subagents} 全部占满，"
                f"待入批 {len(ctx.output.proposals)} 条全部超出"
            ),
        )

    # 3.5) 截断后依赖闭合：保留任务里出现的 `depends_on` 仍必须指向
    #     `verified_facts ∪ 截断保留的批次`——截断掉的 subagent **不**
    #     视为已验证事实（它们没真跑过）；依赖它们的保留项必须被识别为
    #     `MissingDependency`。25D 契约：**不**返回已不满足
    #     `validate_batch` 的 `RoutedExecutionPlan`；要么重检通过（依赖
    #     闭合），要么**整批拒绝**。
    try:
        validate_batch(
            truncated,
            verified_work_ids=verified_work_ids,  # 用**原始** verified（不含被截的）
            goal=ctx.goal,
        )
    except EmptyGoalFieldError as exc:
        return RoutingRejection(
            kind=RejectionKind.EMPTY_GOAL, message=str(exc)
        )
    except DuplicateWorkIdError as exc:
        return RoutingRejection(
            kind=RejectionKind.DUPLICATE_WORK_ID,
            message=str(exc),
            offending_proposal_work_id=exc.work_id,
        )
    except SelfDependencyError as exc:
        return RoutingRejection(
            kind=RejectionKind.SELF_DEPENDENCY, message=str(exc)
        )
    except MissingDependencyError as exc:
        # 截断导致依赖悬空：明确拒绝整批（**不**返回部分成功的 plan）
        return RoutingRejection(
            kind=RejectionKind.MISSING_DEPENDENCY,
            message=(
                f"per-response 截断后保留任务 {exc.work_id!r} 依赖的 "
                f"{list(exc.missing)!r} 被截掉；整批拒绝——"
                f"Orchestrator 应按 lowering max_concurrent_subagents "
                f"或拆批重试"
            ),
            offending_proposal_work_id=exc.work_id,
        )
    except CyclicDependencyError as exc:
        return RoutingRejection(
            kind=RejectionKind.CYCLIC_DEPENDENCY, message=str(exc)
        )
    except BatchValidationError as exc:
        return RoutingRejection(
            kind=type(exc).__name__, message=str(exc)
        )

    # 4) per-run 总量截断（仿官方 max_total）
    subagent_in_kept = sum(
        1
        for p in truncated
        if p.execution_route
        in (ExecutionRoute.SUBAGENT, ExecutionRoute.SUBAGENT_BATCH)
    )
    if (
        ctx.already_routed_in_run + subagent_in_kept
        > ctx.max_total_subagents
    ):
        return RoutingRejection(
            kind=RejectionKind.TOTAL_CAP_EXCEEDED,
            message=(
                f"本 Run 已路由 {ctx.already_routed_in_run} 个 Subagent，"
                f"max_total={ctx.max_total_subagents} 不足以容纳本批 "
                f"{subagent_in_kept} 个"
            ),
        )

    # 5) 资源并行冲突（25A 二次确认）
    try:
        validate_parallel_safe(tuple(p.claims for p in truncated))
    except ResourceConflictError as exc:
        return RoutingRejection(
            kind=RejectionKind.RESOURCE_CONFLICT, message=str(exc)
        )

    # 6) 构造 RoutedExecutionPlan（**不**允许半截通过——上面已重检闭合）
    return _build_routed_plan(truncated, ctx)


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _check_route_domain(
    proposal: WorkItemProposal,
    available_tools: FrozenSet[str],
    available_subagent_types: FrozenSet[str],
    forbidden_subagent_types: FrozenSet[str],
) -> RoutingRejection | None:
    """校验单条提案的 ExecutionRoute × 工具/子代理域是否一致。

    域规则（25D 契约收口——使用模型字段，不再从 work_id 后缀解析）：
    - `DIRECT_AGENT` → `WorkItemProposal` 构造期已拦：tool_name / subagent_type 都 None
    - `TOOL` → `tool_name ∈ available_tools`（非空，**不**靠 work_id 推断）
    - `SUBAGENT` / `SUBAGENT_BATCH` → `subagent_type ∈ available_subagent_types`
      且 ∉ forbidden_subagent_types
    """
    route = proposal.execution_route
    work_id = proposal.work_id

    if route is ExecutionRoute.TOOL:
        if not available_tools:
            return RoutingRejection(
                kind=RejectionKind.UNKNOWN_TOOL,
                message=(
                    f"提案 {work_id!r} 选了 TOOL + tool_name={proposal.tool_name!r}，"
                    f"但 available_tools 为空（未注册任何工具）"
                ),
                offending_proposal_work_id=work_id,
            )
        if proposal.tool_name is None or proposal.tool_name not in available_tools:
            return RoutingRejection(
                kind=RejectionKind.UNKNOWN_TOOL,
                message=(
                    f"提案 {work_id!r} 的 tool_name={proposal.tool_name!r} "
                    f"不在 available_tools={sorted(available_tools)} 中"
                ),
                offending_proposal_work_id=work_id,
            )
        return None
    if route in (ExecutionRoute.SUBAGENT, ExecutionRoute.SUBAGENT_BATCH):
        # 构造期已校验 subagent_type 非空；这里只判配置
        if proposal.subagent_type is None:
            # 防御：构造期应该已拦；不放过
            return RoutingRejection(
                kind=RejectionKind.INVALID_ROUTE_DOMAIN,
                message=(
                    f"提案 {work_id!r} 选了 {route.value} 但 subagent_type 为空"
                ),
                offending_proposal_work_id=work_id,
            )
        if not available_subagent_types:
            return RoutingRejection(
                kind=RejectionKind.UNKNOWN_SUBAGENT_TYPE,
                message=(
                    f"提案 {work_id!r} 选了 {route.value}，但 "
                    f"available_subagent_types 为空（未注册任何子代理类型）"
                ),
                offending_proposal_work_id=work_id,
            )
        if proposal.subagent_type in forbidden_subagent_types:
            return RoutingRejection(
                kind=RejectionKind.FORBIDDEN_SUBAGENT_TYPE,
                message=(
                    f"提案 {work_id!r} 的 subagent_type={proposal.subagent_type!r} "
                    f"在禁用列表中（防递归委派）"
                ),
                offending_proposal_work_id=work_id,
            )
        if proposal.subagent_type not in available_subagent_types:
            return RoutingRejection(
                kind=RejectionKind.UNKNOWN_SUBAGENT_TYPE,
                message=(
                    f"提案 {work_id!r} 的 subagent_type={proposal.subagent_type!r} "
                    f"不在 available_subagent_types="
                    f"{sorted(available_subagent_types)} 中"
                ),
                offending_proposal_work_id=work_id,
            )
    return None


def _truncate_by_concurrency(
    proposals: tuple[WorkItemProposal, ...],
    *,
    already_routed_in_response: int,
    max_concurrent: int,
) -> tuple[tuple[WorkItemProposal, ...], int]:
    """per-response 并发截断：仅 SUBAGENT/SUBAGENT_BATCH 计入并发槽。

    仿官方 `subagent_limit_middleware._truncate_task_calls`：
    `allowed = min(max_concurrent, remaining_total - already_routed_in_response)`。
    越限额丢弃**最早**的（保留尾部的最新决定）。

    Returns:
        (kept_proposals, dropped_subagent_count)
    """
    # 标记 subagent 与否
    is_subagent = [
        p.execution_route in (ExecutionRoute.SUBAGENT, ExecutionRoute.SUBAGENT_BATCH)
        for p in proposals
    ]
    n_sub = sum(is_subagent)
    remaining = max(0, max_concurrent - already_routed_in_response)
    allowed_sub = min(remaining, n_sub)
    if allowed_sub >= n_sub:
        return proposals, 0

    # 截断：保留**最早**的 allowed_sub 条 subagent；其他丢弃
    keep_mask: list[bool] = []
    kept = 0
    for sub in is_subagent:
        if not sub:
            keep_mask.append(True)
        else:
            keep_mask.append(kept < allowed_sub)
            if keep_mask[-1]:
                kept += 1
    return tuple(p for p, k in zip(proposals, keep_mask) if k), n_sub - kept


def _build_routed_plan(
    kept: tuple[WorkItemProposal, ...],
    ctx: RoutingContext,
) -> RoutedExecutionPlan:
    """把通过校验的 proposals 绑成 ValidatedWorkItem + 构造 RoutedExecutionPlan。

    执行目标（tool_name / subagent_type）直接从模型字段读，**不**从
    work_id 解析。
    """
    items: list[ValidatedWorkItem] = []
    for index, proposal in enumerate(kept):
        decision = ExecutionDecision(
            route=proposal.execution_route,
            tool_name=proposal.tool_name,
            subagent_type=proposal.subagent_type,
            parallel_group_id=index,
        )
        items.append(ValidatedWorkItem(proposal=proposal, decision=decision))
    batch = ValidatedBatch(items=tuple(items))
    return RoutedExecutionPlan(
        batches=(batch,),
        rationale=(
            f"路由 {len(items)} 条 WorkItem（其中 "
            f"{sum(1 for it in items if it.decision.route in (ExecutionRoute.SUBAGENT, ExecutionRoute.SUBAGENT_BATCH))} "
            f"个 Subagent 计入 max_total）"
        ),
    )
