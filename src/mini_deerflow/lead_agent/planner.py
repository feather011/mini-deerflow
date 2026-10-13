"""Phase 25C LeadPlanner：单步下一步规划服务。

## 边界（25C 范围）

- **只规划下一步**：不循环、不调度、不开 Lease、不写 Store、不改 LeadRunState
- 输入是 `PlannerContext`（frozen 快照），输出是 `PlannerOutput`（3 选 1 互斥）
- 复用 25A 的 `WorkItemProposal` / `ResourceClaim` / `ExecutionRoute` 与 25A 的
  `validate_batch` 做确定性校验——**不**另写冲突规则
- 复用现有 `Model.decide()` 协议，**不**引入 LangGraph / ToolCall
- 预算：每次模型调用前 `Budget.consume_model_call()`；耗尽则抛
  `LeadPlannerBudgetError`，**不**调模型

## 与 25D 的接缝

Planner **只**输出 `PlannerOutput`；由 25D 的 Orchestrator 把它映射到
`WorkItem.from_proposal(...)`（已存在于 25A）/ 写 `LeadRunState` / 开 Lease。
25C 阶段不存在 Orchestrator，本模块**不会**被它驱动——通过测试覆盖。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, Callable, ClassVar, Mapping, Protocol, runtime_checkable

from ..middleware import Budget
from ..models import Message
from .errors import (
    LeadPlannerBudgetError,
    LeadPlannerDeterministicError,
    LeadPlannerError,
    LeadPlannerMaxAttemptsError,
    LeadPlannerSchemaError,
)
from .models import (
    ExecutionAttempt,
    GoalContract,
    PlanningDecision,
    ResourceClaim,
    VerifiedFact,
    WorkItem,
    WorkItemProposal,
)
from .validation import validate_batch

__all__ = [
    "BudgetSnapshot",
    "DEFAULT_MAX_PLANNING_ATTEMPTS",
    "DefaultLeadPlanner",
    "LeadPlanner",
    "PlannerContext",
    "PlannerOutput",
    "ProposeBatch",
    "RecommendSynthesis",
    "RequestClarification",
    "build_budget_snapshot",
    "default_system_prompt",
    "summarize_recent_items",
]


# ---------------------------------------------------------------------------
# 预算只读快照（不让 Planner 持 Budget 引用）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetSnapshot:
    """Budget 的只读快照——传给 Planner 后即便 Budget 被并发修改也不影响。

    Planner 拿到的是**值**而不是引用，因此**不可能**误调 `consume_*()`。
    预算扣减仍由 `DefaultLeadPlanner` 通过它自己持有的 `Budget` 引用完成——
    Planner 内部接口封装，不让上下文对象把可写 Budget 带进来。
    """

    max_steps: int | None
    max_model_calls: int | None
    max_tool_calls: int | None
    max_subagent_tasks: int | None
    max_subagent_depth: int | None
    max_summary_calls: int | None
    steps_used: int
    model_calls_used: int
    tool_calls_used: int
    subagent_tasks_used: int
    summary_calls_used: int


def build_budget_snapshot(budget: Budget) -> BudgetSnapshot:
    """在持有 Budget 锁的前提下取一个**值**快照；调用方拿到后 Budget 可变。"""
    # Budget 的 consume_* 在自己的锁内完成；这里只读所以加锁取一致视图。
    with budget._lock:  # noqa: SLF001 — 受控访问：只读、不修改
        return BudgetSnapshot(
            max_steps=budget.max_steps,
            max_model_calls=budget.max_model_calls,
            max_tool_calls=budget.max_tool_calls,
            max_subagent_tasks=budget.max_subagent_tasks,
            max_subagent_depth=budget.max_subagent_depth,
            max_summary_calls=budget.max_summary_calls,
            steps_used=budget._steps,  # noqa: SLF001
            model_calls_used=budget._model_calls,  # noqa: SLF001
            tool_calls_used=budget._tool_calls,  # noqa: SLF001
            subagent_tasks_used=budget._subagent_tasks,  # noqa: SLF001
            summary_calls_used=budget._summary_calls,  # noqa: SLF001
        )


# ---------------------------------------------------------------------------
# 任务摘要（只取 Planner 真正需要的字段，**不**倒 LeadRunState 全部历史）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkItemSummary:
    """WorkItem 的 Planner 摘要——不含 attempts / status 变更细节。

    状态字段保留 `status`（字符串值），便于模型判断「已完成/失败/运行中」。
    """

    work_id: str
    objective: str
    status: str
    execution_route: str
    claims: tuple[tuple[str, str], ...]  # (resource, access.value)


def summarize_recent_items(
    items: tuple[WorkItem, ...], *, keep: int = 5
) -> tuple[WorkItemSummary, ...]:
    """取最近 `keep` 个 WorkItem 的摘要；空输入返回空 tuple。

    按 `created_at` 升序取尾（同时间戳时按 `work_id` 升序破平）。
    `keep <= 0` 时返回空 tuple（用于零窗口）。
    """
    if keep <= 0 or not items:
        return ()
    ordered = sorted(items, key=lambda w: (w.created_at, w.work_id))
    recent = ordered[-keep:]
    return tuple(
        WorkItemSummary(
            work_id=w.work_id,
            objective=w.objective,
            status=w.status.value,
            execution_route=w.execution_route.value,
            claims=tuple((c.resource, c.access.value) for c in w.claims),
        )
        for w in recent
    )


# ---------------------------------------------------------------------------
# Planner 输入
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannerContext:
    """单次 `plan()` 调用的全部输入。

    frozen + 全部值为只读对象（`BudgetSnapshot`、原 `VerifiedFact` / `WorkItem`
    的不可变视图）。**不**含 `LeadRunState` / `Store` / `Budget` 引用——
    调用方应在 Orchestrator（25D）侧把可变状态投影成本上下文。
    """

    goal: GoalContract
    verified_facts: tuple[VerifiedFact, ...]
    last_decision: PlanningDecision | None
    recent_items: tuple[WorkItemSummary, ...]
    budget_snapshot: BudgetSnapshot
    capabilities: tuple[str, ...] = ()
    # 上一轮 `validate_batch` 失败的反馈；首轮为 None
    last_error: LeadPlannerDeterministicError | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.goal, GoalContract):
            raise TypeError("goal 必须是 GoalContract")
        for fact in self.verified_facts:
            if not isinstance(fact, VerifiedFact):
                raise TypeError("verified_facts 元素必须是 VerifiedFact")
        if self.last_decision is not None and not isinstance(
            self.last_decision, PlanningDecision
        ):
            raise TypeError("last_decision 必须是 PlanningDecision 或 None")
        for item in self.recent_items:
            if not isinstance(item, WorkItemSummary):
                raise TypeError("recent_items 元素必须是 WorkItemSummary")
        if not isinstance(self.budget_snapshot, BudgetSnapshot):
            raise TypeError("budget_snapshot 必须是 BudgetSnapshot")
        for name in self.capabilities:
            if not isinstance(name, str) or not name.strip():
                raise ValueError("capabilities 元素必须是非空字符串")
        if self.last_error is not None and not isinstance(
            self.last_error, LeadPlannerDeterministicError
        ):
            raise TypeError("last_error 必须是 LeadPlannerDeterministicError 或 None")


# ---------------------------------------------------------------------------
# Planner 输出（3 选 1 互斥）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannerOutput:
    """基类：仅携带 `kind` 标签，外部用 `isinstance` 判分支。

    `kind` 作为 `ClassVar` 给出默认值；三个子类在 `__post_init__` 里覆盖。
    用 `ClassVar` 是因为 `@dataclass(frozen=True)` 会把任何普通字段都塞进
    `__init__` 形参列表，而子类已经声明了非默认字段（proposals 等）——
    把 `kind` 当字段会触发「默认字段后跟非默认字段」错误。
    """

    kind: ClassVar[str] = ""


@dataclass(frozen=True)
class ProposeBatch(PlannerOutput):
    """标签：提出下一批 `WorkItemProposal`（可能为空元组——空批不合法，校验拦）。"""

    proposals: tuple[WorkItemProposal, ...]
    rationale: str

    def __post_init__(self) -> None:
        if not isinstance(self.proposals, tuple):
            raise TypeError("proposals 必须是 tuple")
        for p in self.proposals:
            if not isinstance(p, WorkItemProposal):
                raise TypeError("proposals 元素必须是 WorkItemProposal")
        if not self.rationale.strip():
            raise ValueError("rationale 不能为空")
        object.__setattr__(self, "kind", "propose_batch")


@dataclass(frozen=True)
class RequestClarification(PlannerOutput):
    """标签：请求用户澄清。

    `blocking_fact_ids` 引用 `VerifiedFact.fact_id`（**不是** `work_id`）：
    fact 是构造期被强制带证据的最小事实单元，事实之间通过 `fact_id` 唯一定位。
    用 `work_id` 反而绕了一层事实归集。
    """

    question: str
    reason: str
    blocking_fact_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.question, str) or not self.question.strip():
            raise ValueError("question 不能为空")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason 不能为空")
        if not isinstance(self.blocking_fact_ids, tuple):
            raise TypeError("blocking_fact_ids 必须是 tuple")
        for fid in self.blocking_fact_ids:
            if not isinstance(fid, str) or not fid.strip():
                raise ValueError("blocking_fact_ids 元素必须是非空字符串")
        object.__setattr__(self, "kind", "request_clarification")


@dataclass(frozen=True)
class RecommendSynthesis(PlannerOutput):
    """标签：建议进入综合。**不是**终态——需要 25D 的 Synthesizer 真正跑。

    `target_criterion` 必须是 `goal.acceptance_criteria` 之一；
    `supporting_fact_ids` 必须是 `verified_facts.fact_id` 的子集。
    """

    target_criterion: str
    supporting_fact_ids: tuple[str, ...]
    rationale: str

    def __post_init__(self) -> None:
        if not isinstance(self.target_criterion, str) or not self.target_criterion.strip():
            raise ValueError("target_criterion 不能为空")
        if not isinstance(self.supporting_fact_ids, tuple):
            raise TypeError("supporting_fact_ids 必须是 tuple")
        for fid in self.supporting_fact_ids:
            if not isinstance(fid, str) or not fid.strip():
                raise ValueError("supporting_fact_ids 元素必须是非空字符串")
        if not self.rationale.strip():
            raise ValueError("rationale 不能为空")
        object.__setattr__(self, "kind", "recommend_synthesis")


# ---------------------------------------------------------------------------
# Planner 接口与默认实现
# ---------------------------------------------------------------------------


class LeadPlanner(Protocol):
    """LeadPlanner 协议——Orchestrator（25D 引入）按此协议注入。"""

    def plan(self, ctx: PlannerContext) -> PlannerOutput: ...


# `max_planning_attempts` 与 WorkItem 的 `max_revisions_per_item=1` **独立**计数。
# 这是用户硬性约束：每轮规划至多 2 次模型尝试，连续 2 次失败 → FAILED。
DEFAULT_MAX_PLANNING_ATTEMPTS = 2


# JSON schema 提示：让模型知道该输出什么形状（自描述，无需读源码）。
# 严格意义上的"schema"由解析器+校验器落实，不是让模型做自由生成。
DEFAULT_SYSTEM_PROMPT = """\
你是 Lead Agent 的下一步规划器。**只决定下一步**，不执行、不调度、不调用工具。

输入包含：
- `goal`：当前目标契约（objective + acceptance_criteria）
- `verified_facts`：已经过验证的事实（构造期已强制非空 + 带证据）
- `last_decision`：上一批提案（如有）
- `recent_items`：最近 5 个 WorkItem 摘要（含 status）
- `budget_snapshot`：当前 Budget 各资源上限与已用量
- `capabilities`：可用的子代理类型名（如 general_purpose / bash）
- `last_error`：上一次 `validate_batch` 失败的反馈（首轮为 null）

你的输出必须是**严格 JSON**，且**仅三种形态之一**：

1) 提出下一批：
   {
     "kind": "propose_batch",
     "proposals": [
       {
         "work_id": "stable-uuid-or-name",
         "objective": "本任务的 objective",
         "depends_on": ["可选，已验证 work_id 或本批内 work_id"],
         "claims": [{"resource": "file:src/foo", "access": "read|write|unknown"}],
         "execution_route": "direct_agent|tool|subagent|subagent_batch",
         "tool_name": "执行路线选 tool 时必填（如 calculator）；direct_agent / subagent* 时不要给",
         "subagent_type": "执行路线选 subagent / subagent_batch 时必填（如 general_purpose / bash）；direct_agent / tool 时不要给"
       }
     ],
     "rationale": "为什么这批是下一步"
   }

2) 请求用户澄清（**信息不足**才用；能用工具查的不要问用户）：
   {
     "kind": "request_clarification",
     "question": "给用户的一句话问题",
     "reason": "为什么这个回答会改变计划",
     "blocking_fact_ids": ["verified_facts 中的 fact_id（不是 work_id）；可以为空元组——表示'我没有任何事实可参考'"]
   }

3) 建议进入综合（**不是**终态——只是建议；综合是否进行由后续阶段决定）：
   {
     "kind": "recommend_synthesis",
     "target_criterion": "goal.acceptance_criteria 中的一条原文",
     "supporting_fact_ids": ["verified_facts 中的 fact_id，必须存在"],
     "rationale": "为什么这批事实足以支撑该 criterion"
   }

规则（**违反任何一条都视为失败**）：
- 优先 `direct_agent`；委派需要给出明确价值（并行 / 专业能力 / 上下文隔离）
- `propose_batch.proposals` **不能为空**（空批不是有效规划）
- `depends_on` 中的 work_id 必须 ∈ verified_facts.work_id ∪ 本批 proposals.work_id
- `claims[].resource` 用 `normalize_resource` 的规范形式；逻辑资源保持精确
- 不要输出 JSON 以外的任何内容
- **`execution_route` 选 `tool` 时必须同时给出非空 `tool_name`**；
  选 `subagent` / `subagent_batch` 时必须同时给出非空 `subagent_type`；
  选 `direct_agent` 时两者**都**留空——构造期会直接拒绝
- `recommend_synthesis` 的 `target_criterion` 必须是 `goal.acceptance_criteria` 之一
- `request_clarification.blocking_fact_ids` 与 `recommend_synthesis.supporting_fact_ids`
  都引用 **fact_id**，**不是** work_id
- 当 `last_error` 非空时，**修复**它指出的问题，不要重复犯
"""


def default_system_prompt() -> str:
    """返回默认系统提示词；25D 之前无外部配置入口。"""
    return DEFAULT_SYSTEM_PROMPT


# 错误反馈前缀（提示模型：上次为什么失败）
_REPLAN_PREFIX = (
    "上一次你的输出被确定性校验拒绝：[{kind}] {message}\n"
    "请重新生成 JSON，**只**修复上述问题，不要新增未要求的字段。\n"
    "---上一次输出：---\n{raw}\n---"
)


@runtime_checkable
class LeadPlanner(Protocol):
    """Planner 协议——内部服务接口，供 25D Orchestrator 调用。"""

    def plan(self, ctx: PlannerContext) -> PlannerOutput: ...


# ---------------------------------------------------------------------------
# JSON 解析与字段校验
# ---------------------------------------------------------------------------


_KNOWN_KINDS = frozenset({"propose_batch", "request_clarification", "recommend_synthesis"})


def _parse_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if not text:
        raise LeadPlannerSchemaError("模型返回空内容", raw_text=raw)
    # 允许模型把 JSON 嵌在 ```json ... ``` 里——剥掉常见代码块围栏
    if text.startswith("```"):
        # 去掉首尾的 ``` 与可能存在的 ```json
        first_newline = text.find("\n")
        if first_newline != -1:
            inner = text[first_newline + 1 :]
        else:
            inner = text
        if inner.rstrip().endswith("```"):
            inner = inner.rstrip()[:-3]
        text = inner.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LeadPlannerSchemaError(
            f"JSON 解析失败: {exc.msg}", raw_text=raw
        ) from exc
    if not isinstance(data, dict):
        raise LeadPlannerSchemaError(
            f"顶层必须是 JSON 对象，收到 {type(data).__name__}", raw_text=raw
        )
    return data


def _build_proposal(raw: dict[str, Any]) -> WorkItemProposal:
    """把模型输出的 proposal 字典转成 `WorkItemProposal`（frozen）。

    字段缺失 / 类型错都抛 `LeadPlannerSchemaError`——解析层负责 schema，
    `validate_batch` 负责依赖闭合/无环/不重名。
    """
    if not isinstance(raw, dict):
        raise LeadPlannerSchemaError(f"proposal 必须是对象，收到 {type(raw).__name__}")

    work_id = raw.get("work_id")
    if not isinstance(work_id, str) or not work_id.strip():
        raise LeadPlannerSchemaError("proposal.work_id 必须是字符串")

    objective = raw.get("objective")
    if not isinstance(objective, str) or not objective.strip():
        raise LeadPlannerSchemaError(f"proposal({work_id}).objective 必须是非空字符串")

    depends_on_raw = raw.get("depends_on", ())
    if not isinstance(depends_on_raw, (list, tuple)):
        raise LeadPlannerSchemaError(
            f"proposal({work_id}).depends_on 必须是数组，收到 {type(depends_on_raw).__name__}"
        )
    depends_on = tuple(
        d for d in depends_on_raw if isinstance(d, str) and d.strip()
    )

    claims_raw = raw.get("claims", ())
    if not isinstance(claims_raw, (list, tuple)):
        raise LeadPlannerSchemaError(
            f"proposal({work_id}).claims 必须是数组，收到 {type(claims_raw).__name__}"
        )
    claims: list[ResourceClaim] = []
    for claim in claims_raw:
        if not isinstance(claim, dict):
            raise LeadPlannerSchemaError(
                f"proposal({work_id}).claims 元素必须是对象"
            )
        try:
            claims.append(
                ResourceClaim.of(
                    str(claim.get("resource", "")),
                    claim.get("access", "read"),
                )
            )
        except (TypeError, ValueError) as exc:
            raise LeadPlannerSchemaError(
                f"proposal({work_id}).claims 非法: {exc}"
            ) from exc

    route_raw = raw.get("execution_route", "direct_agent")
    if not isinstance(route_raw, str):
        raise LeadPlannerSchemaError(
            f"proposal({work_id}).execution_route 必须是字符串"
        )

    # 25D：显式路由目标——从模型输出读 tool_name / subagent_type；
    # 缺失/类型错在 WorkItemProposal 构造期自己拦（与 ExecutionRoute 一致）
    tool_name_raw = raw.get("tool_name")
    subagent_type_raw = raw.get("subagent_type")
    tool_name = tool_name_raw if isinstance(tool_name_raw, str) else None
    subagent_type = subagent_type_raw if isinstance(subagent_type_raw, str) else None

    # WorkItemProposal 构造期会自己校验 route / claims 形状
    try:
        return WorkItemProposal(
            work_id=work_id.strip(),
            objective=objective.strip(),
            depends_on=depends_on,
            claims=tuple(claims),
            execution_route=_parse_execution_route(work_id, route_raw),
            tool_name=tool_name,
            subagent_type=subagent_type,
        )
    except (ValueError, TypeError) as exc:
        raise LeadPlannerSchemaError(
            f"proposal({work_id}) 构造失败: {exc}"
        ) from exc


def _parse_execution_route(work_id: str, raw: Any) -> Any:
    """把字符串解析成 ExecutionRoute 枚举；失败抛 schema 错误。"""
    from .state_machine import ExecutionRoute  # 局部 import 避免循环

    if not isinstance(raw, str):
        raise LeadPlannerSchemaError(
            f"proposal({work_id}).execution_route 必须是字符串"
        )
    try:
        return ExecutionRoute(raw)
    except ValueError as exc:
        raise LeadPlannerSchemaError(
            f"proposal({work_id}).execution_route 未知: {raw!r}"
        ) from exc


def _parse_propose_batch(data: dict[str, Any]) -> ProposeBatch:
    proposals_raw = data.get("proposals")
    if not isinstance(proposals_raw, (list, tuple)):
        raise LeadPlannerSchemaError("propose_batch.proposals 必须是数组")
    if not proposals_raw:
        raise LeadPlannerSchemaError("propose_batch.proposals 不能为空")
    proposals = tuple(_build_proposal(p) for p in proposals_raw)
    rationale = data.get("rationale", "")
    if not isinstance(rationale, str) or not rationale.strip():
        raise LeadPlannerSchemaError("propose_batch.rationale 必须是非空字符串")
    return ProposeBatch(proposals=proposals, rationale=rationale.strip())


def _parse_clarification(data: dict[str, Any]) -> RequestClarification:
    question = data.get("question", "")
    reason = data.get("reason", "")
    if not isinstance(question, str) or not question.strip():
        raise LeadPlannerSchemaError("request_clarification.question 必须是非空字符串")
    if not isinstance(reason, str) or not reason.strip():
        raise LeadPlannerSchemaError("request_clarification.reason 必须是非空字符串")
    facts_raw = data.get("blocking_fact_ids", ())
    if not isinstance(facts_raw, (list, tuple)):
        raise LeadPlannerSchemaError(
            "blocking_fact_ids 必须是数组（这是 fact_id，不是 work_id）"
        )
    blocking = tuple(f for f in facts_raw if isinstance(f, str) and f.strip())
    return RequestClarification(
        question=question.strip(), reason=reason.strip(), blocking_fact_ids=blocking
    )


def _parse_recommend_synthesis(data: dict[str, Any]) -> RecommendSynthesis:
    target = data.get("target_criterion", "")
    if not isinstance(target, str) or not target.strip():
        raise LeadPlannerSchemaError(
            "recommend_synthesis.target_criterion 必须是非空字符串"
        )
    facts_raw = data.get("supporting_fact_ids", ())
    if not isinstance(facts_raw, (list, tuple)):
        raise LeadPlannerSchemaError("supporting_fact_ids 必须是数组")
    fact_ids = tuple(f for f in facts_raw if isinstance(f, str) and f.strip())
    rationale = data.get("rationale", "")
    if not isinstance(rationale, str) or not rationale.strip():
        raise LeadPlannerSchemaError("recommend_synthesis.rationale 必须是非空字符串")
    return RecommendSynthesis(
        target_criterion=target.strip(),
        supporting_fact_ids=fact_ids,
        rationale=rationale.strip(),
    )


def parse_planner_output(raw: str) -> PlannerOutput:
    """把模型文本解析成 `PlannerOutput`（3 选 1）；schema 错误抛 `LeadPlannerSchemaError`。

    解析只关心**结构**：JSON 合不合法、字段齐不齐、类型对不对。
    业务校验（`validate_batch`、target_criterion 必须在 acceptance_criteria 里）
    由 `DefaultLeadPlanner` 在解析后跑——失败抛 `LeadPlannerDeterministicError`。
    """
    data = _parse_json(raw)
    kind = data.get("kind")
    if not isinstance(kind, str) or kind not in _KNOWN_KINDS:
        raise LeadPlannerSchemaError(
            f"kind 必须是 {_KNOWN_KINDS} 之一，收到 {kind!r}"
        )
    if kind == "propose_batch":
        return _parse_propose_batch(data)
    if kind == "request_clarification":
        return _parse_clarification(data)
    return _parse_recommend_synthesis(data)


# ---------------------------------------------------------------------------
# 业务校验（确定性，独立于模型）
# ---------------------------------------------------------------------------


def _validate_propose_batch(
    output: ProposeBatch, ctx: PlannerContext
) -> ProposeBatch:
    """对 `ProposeBatch` 跑 25A 的 `validate_batch`，再补资源冲突检查。"""
    verified_ids = frozenset(f.work_id for f in ctx.verified_facts)
    try:
        # validate_batch 通过 → 返回不可变 WorkItem tuple；本阶段 Planner 不需要
        # 完整 WorkItem（25D 才会用 from_proposal），只用它的"是否通过"信号
        validate_batch(output.proposals, verified_work_ids=verified_ids, goal=ctx.goal)
    except Exception as exc:  # noqa: BLE001 — 6 种错误均映射为 deterministic
        kind = type(exc).__name__
        message = str(exc)
        raise LeadPlannerDeterministicError(kind=kind, message=message) from exc

    # 资源并行冲突（read-write / write-write / 任一 UNKNOWN 强制冲突）
    # 把每个 proposal 单独建组（按组之间比较），避免无意义的 N×N 重复
    try:
        from .validation import validate_parallel_safe  # 局部 import

        # 同一批内并行校验：每个 WorkItem 单独一组
        groups = tuple(p.claims for p in output.proposals)
        validate_parallel_safe(groups)
    except Exception as exc:  # noqa: BLE001
        kind = type(exc).__name__
        raise LeadPlannerDeterministicError(kind=kind, message=str(exc)) from exc
    return output


def _validate_clarification(
    output: RequestClarification, ctx: PlannerContext
) -> RequestClarification:
    """`blocking_fact_ids` 引用 `VerifiedFact.fact_id`——事实归集通过 fact_id。

    注意区分：
    - `VerifiedFact.fact_id`：事实唯一键
    - `VerifiedFact.work_id`：源 WorkItem 的 id（同一 work_id 上可挂多条事实）

    命名 `blocking_fact_ids` 是因为该字段语义是"被这些事实的结论卡住了"，
    **不是** "卡在这些任务上"——后者归 `WorkItem` / `depends_on` 管。
    """
    known = frozenset(f.fact_id for f in ctx.verified_facts)
    missing = tuple(fid for fid in output.blocking_fact_ids if fid not in known)
    if missing:
        raise LeadPlannerDeterministicError(
            kind="UnknownBlockingFact",
            message=(
                f"blocking_fact_ids 引用了不存在的 fact_id: "
                f"{', '.join(missing)}（这是 fact_id，不是 work_id）"
            ),
        )
    return output


def _validate_recommend_synthesis(
    output: RecommendSynthesis, ctx: PlannerContext
) -> RecommendSynthesis:
    """`target_criterion` 必须是 `goal.acceptance_criteria` 之一；supporting fact_id 必须存在。"""
    if output.target_criterion not in ctx.goal.acceptance_criteria:
        raise LeadPlannerDeterministicError(
            kind="UnknownTargetCriterion",
            message=(
                f"target_criterion 必须 ∈ goal.acceptance_criteria；"
                f"收到 {output.target_criterion!r}"
            ),
        )
    known = frozenset(f.fact_id for f in ctx.verified_facts)
    missing = tuple(fid for fid in output.supporting_fact_ids if fid not in known)
    if missing:
        raise LeadPlannerDeterministicError(
            kind="UnknownSupportingFact",
            message=f"supporting_fact_ids 引用了不存在的 fact_id: {', '.join(missing)}",
        )
    if not output.supporting_fact_ids:
        raise LeadPlannerDeterministicError(
            kind="EmptySupportingFacts",
            message="recommend_synthesis 必须至少给出一条 supporting_fact_ids",
        )
    return output


def _validate_business(
    output: PlannerOutput, ctx: PlannerContext
) -> PlannerOutput:
    """把结构化输出按类型跑业务校验；返回校验通过的同一个对象。"""
    if isinstance(output, ProposeBatch):
        return _validate_propose_batch(output, ctx)
    if isinstance(output, RequestClarification):
        return _validate_clarification(output, ctx)
    if isinstance(output, RecommendSynthesis):
        return _validate_recommend_synthesis(output, ctx)
    raise LeadPlannerSchemaError(
        f"未知 PlannerOutput 子类: {type(output).__name__}"
    )


# ---------------------------------------------------------------------------
# 默认实现
# ---------------------------------------------------------------------------


class DefaultLeadPlanner:
    """LeadPlanner 的默认实现：

    - 每次模型调用前通过 `root_budget.consume_model_call()` 扣费（耗尽则抛
      `LeadPlannerBudgetError`，模型不被调用）
    - 最多 `max_attempts` 次尝试（默认 2）；首次失败携带具体错误重试
    - 解析层负责 JSON schema，业务校验复用 25A `validate_batch`
    - **不**触碰 LeadRunState / Store / Lease / LangGraph

    Root Budget 通过 `RootBudget` Protocol 接入：25C 测试期可走 `Budget`
    内存适配器（`planner_budget.BudgetRootBudget`），25D 上线时切到
    `planner_budget.LeadLedgerRootBudget`（持久化路径）。
    """

    def __init__(
        self,
        model: Any,
        root_budget: Any,
        *,
        system_prompt: str | None = None,
        max_attempts: int = DEFAULT_MAX_PLANNING_ATTEMPTS,
    ) -> None:
        if not callable(getattr(model, "decide", None)):
            raise TypeError("model 必须实现 decide(messages, tools)")
        if not callable(getattr(root_budget, "consume_model_call", None)):
            raise TypeError(
                "root_budget 必须实现 consume_model_call()（RootBudget 协议）"
            )
        if not callable(getattr(root_budget, "snapshot", None)):
            raise TypeError(
                "root_budget 必须实现 snapshot()（RootBudget 协议）"
            )
        if not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError(f"max_attempts 必须为正整数，收到 {max_attempts!r}")
        self._model = model
        self._root_budget = root_budget
        self._system_prompt = (
            system_prompt if system_prompt is not None else default_system_prompt()
        )
        self._max_attempts = max_attempts

    def plan(self, ctx: PlannerContext) -> PlannerOutput:
        if not isinstance(ctx, PlannerContext):
            raise TypeError("ctx 必须是 PlannerContext")

        last_error: LeadPlannerError | None = None
        last_raw = ""

        for attempt in range(1, self._max_attempts + 1):
            # 1) 预算闸门：通过 RootBudget 协议扣费
            #    （`LeadLedgerRootBudget` 适配器会在内部走
            #    `LeadBudgetLedger.consume_direct` copy-on-write 路径；
            #    `BudgetRootBudget` 直接走 `Budget.consume_model_call` 内存路径）
            try:
                self._root_budget.consume_model_call()
            except Exception as exc:  # BudgetExceededError / 持久化钩子异常都覆盖
                # 不论 BudgetExceeded 还是持久化失败，**不**调模型
                raise LeadPlannerBudgetError(
                    reason=str(exc),
                    resource=(
                        getattr(exc, "resource", None)
                        if hasattr(exc, "resource")
                        else None
                    ),
                ) from exc

            # 2) 拼 messages：system + （错误反馈 + ）context
            messages = self._build_messages(ctx, last_error, last_raw, attempt)

            # 3) 调模型（**必须**在 root_budget.consume_model_call 成功之后）
            response = self._model.decide(messages)
            content = getattr(response, "content", "")
            if not isinstance(content, str):
                raise LeadPlannerSchemaError(
                    f"模型返回不是 FinalAnswer，收到 {type(response).__name__}"
                )
            last_raw = content

            # 4) 解析
            try:
                output = parse_planner_output(content)
            except LeadPlannerSchemaError as exc:
                last_error = exc
                if attempt < self._max_attempts:
                    continue
                raise LeadPlannerMaxAttemptsError(self._max_attempts, exc) from exc

            # 5) 业务校验
            try:
                _validate_business(output, ctx)
            except LeadPlannerDeterministicError as exc:
                last_error = exc
                if attempt < self._max_attempts:
                    continue
                raise LeadPlannerMaxAttemptsError(self._max_attempts, exc) from exc

            return output

        # 理论上到不了；保险
        raise LeadPlannerMaxAttemptsError(
            self._max_attempts,
            last_error or LeadPlannerError("未记录的错误"),
        )

    # ------------------------------------------------------------------

    def _build_messages(
        self,
        ctx: PlannerContext,
        last_error: LeadPlannerError | None,
        last_raw: str,
        attempt: int,
    ) -> list[Message]:
        user_payload = self._serialize_context(ctx)
        user_text = json.dumps(user_payload, ensure_ascii=False)
        if last_error is not None and attempt > 1:
            user_text = (
                _REPLAN_PREFIX.format(
                    kind=type(last_error).__name__,
                    message=str(last_error),
                    raw=(last_raw[:1000] if last_raw else "<empty>"),
                )
                + "\n"
                + user_text
            )
        return [
            Message(role="system", content=self._system_prompt),
            Message(role="user", content=user_text),
        ]

    @staticmethod
    def _serialize_context(ctx: PlannerContext) -> dict[str, Any]:
        """把 PlannerContext 序列化为模型可读的 JSON dict。"""
        return {
            "goal": {
                "goal_id": ctx.goal.goal_id,
                "objective": ctx.goal.objective,
                "acceptance_criteria": list(ctx.goal.acceptance_criteria),
                "constraints": list(ctx.goal.constraints),
            },
            "verified_facts": [
                {
                    "fact_id": f.fact_id,
                    "work_id": f.work_id,
                    "statement": f.statement,
                    "established_at": f.established_at,
                }
                for f in ctx.verified_facts
            ],
            "last_decision": (
                {
                    "decision_id": ctx.last_decision.decision_id,
                    "batch_index": ctx.last_decision.batch_index,
                    "proposals": [
                        {
                            "work_id": p.work_id,
                            "objective": p.objective,
                            "depends_on": list(p.depends_on),
                            "claims": [
                                {"resource": c.resource, "access": c.access.value}
                                for c in p.claims
                            ],
                            "execution_route": p.execution_route.value,
                        }
                        for p in ctx.last_decision.proposals
                    ],
                    "rationale": ctx.last_decision.rationale,
                }
                if ctx.last_decision is not None
                else None
            ),
            "recent_items": [
                {
                    "work_id": it.work_id,
                    "objective": it.objective,
                    "status": it.status,
                    "execution_route": it.execution_route,
                    "claims": [
                        {"resource": r, "access": a} for r, a in it.claims
                    ],
                }
                for it in ctx.recent_items
            ],
            "budget_snapshot": {
                "max_model_calls": ctx.budget_snapshot.max_model_calls,
                "model_calls_used": ctx.budget_snapshot.model_calls_used,
                "max_tool_calls": ctx.budget_snapshot.max_tool_calls,
                "tool_calls_used": ctx.budget_snapshot.tool_calls_used,
                "max_subagent_tasks": ctx.budget_snapshot.max_subagent_tasks,
                "subagent_tasks_used": ctx.budget_snapshot.subagent_tasks_used,
                "max_summary_calls": ctx.budget_snapshot.max_summary_calls,
                "summary_calls_used": ctx.budget_snapshot.summary_calls_used,
                "max_steps": ctx.budget_snapshot.max_steps,
                "steps_used": ctx.budget_snapshot.steps_used,
                "max_subagent_depth": ctx.budget_snapshot.max_subagent_depth,
            },
            "capabilities": list(ctx.capabilities),
            "last_error": (
                {"kind": ctx.last_error.kind, "message": ctx.last_error.message}
                if ctx.last_error is not None
                else None
            ),
        }
