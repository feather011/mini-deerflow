"""Phase 25A Lead Agent 领域模型。

三条冻结设计在模型层的体现：

1. **滚动批次**：没有 `Plan`/`DAG` 对象，只有 `PlanningDecision`（一次决策）
   与 `WorkItemProposal`（本批提案）；依赖解析只看
   「已验证历史 ∪ 当前批次」，见 `validation.py`。
2. **只有 `VerifiedFact` 能进入后续规划**：`EvidenceReceipt` 与
   `VerificationResult` 是它的构成部件，但不能单独流转——规划输入的
   类型就是 `VerifiedFact`。
3. **ThreadState 仅在整个 Lead Run 成功后提交**：本模块不触碰
   `ThreadState` / `ThreadStore`，`LeadRunState` 只是运行态，没有
   `commit()` 之类的方法——提交时机由上层集成阶段决定。

序列化沿用仓库既有风格：`schema_version` 字段 + `to_dict`/`from_dict` +
`_req()` 缺字段报错 + `str` 枚举按 `.value` 落盘。

时间戳统一用 `time.time()`（墙钟）而非 `monotonic`：本模块的状态要
序列化持久化，monotonic 时钟跨进程不可比较。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any

from .errors import (
    EmptyGoalFieldError,
    RevisionLimitExceededError,
    UnknownResourceError,
)
from .state_machine import (
    ExecutionRoute,
    LeadRunStatus,
    ResourceAccessMode,
    WorkItemStatus,
    is_terminal,
    validate_transition,
)

__all__ = [
    "ExecutionAttempt",
    "EvidenceReceipt",
    "GoalAmendment",
    "GoalContract",
    "LeadRunState",
    "PlanningDecision",
    "ResourceClaim",
    "VerificationResult",
    "VerifiedFact",
    "WorkItem",
    "WorkItemProposal",
    "SCHEMA_VERSION",
]

SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# 序列化辅助（与 subagent_models.req 同风格）
# ---------------------------------------------------------------------------


def _req(data: dict[str, Any], key: str) -> Any:
    if key not in data:
        raise ValueError(f"缺少字段: {key}")
    return data[key]


def _check_version(data: dict[str, Any], entity: str) -> None:
    version = data.get("schema_version", SCHEMA_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError(f"{entity}.schema_version 必须是整数")
    if version != SCHEMA_VERSION:
        raise ValueError(f"未知 schema_version: {version}（当前 {SCHEMA_VERSION}）")


def _non_empty_str(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} 必须是字符串")
    if not value.strip():
        raise ValueError(f"{name} 不能为空")
    return value.strip()


def _str_tuple(value: object, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} 必须是数组")
    items: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise ValueError(f"{name} 元素必须是字符串")
        if entry.strip():
            items.append(entry.strip())
    return tuple(items)


def _optional_str_field(data: dict[str, object], key: str) -> str | None:
    """读取可选字符串字段；缺省即视为 None。

    25D 兼容老数据：旧 payload 不带 `tool_name` / `subagent_type` 键时
    返回 None，让 DIRECT_AGENT 路线的不变式自然成立。
    """
    if key not in data:
        return None
    value = data[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} 必须是字符串或 null，收到: {type(value).__name__}")
    return value


# ---------------------------------------------------------------------------
# 目标契约与修订
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GoalAmendment:
    """对目标契约的一次修订（滚动批次期间目标可能被澄清或收窄）。

    frozen：修订是**不可变的事实记录**，改目标=新增一条 amendment 并派生
    新的 `GoalContract`，而不是原地改写旧契约——否则无法回答
    「当时是按哪个版本的目标规划的」。
    """

    amendment_id: str
    objective: str
    acceptance_criteria: tuple[str, ...]
    reason: str
    created_at: float
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"未知 schema_version: {self.schema_version}")
        _non_empty_str(self.amendment_id, "amendment_id")
        # 与 GoalContract.objective 同语义：目标字段为空抛领域异常
        if not isinstance(self.objective, str) or not self.objective.strip():
            raise EmptyGoalFieldError("objective")
        if not self.acceptance_criteria:
            raise EmptyGoalFieldError("acceptance_criteria")
        for item in self.acceptance_criteria:
            _non_empty_str(item, "acceptance_criteria 元素")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "amendment_id": self.amendment_id,
            "objective": self.objective,
            "acceptance_criteria": list(self.acceptance_criteria),
            "reason": self.reason,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "GoalAmendment":
        if not isinstance(data, dict):
            raise ValueError("GoalAmendment 必须是字典")
        _check_version(data, "GoalAmendment")
        return cls(
            amendment_id=str(_req(data, "amendment_id")),
            objective=str(_req(data, "objective")),
            acceptance_criteria=_str_tuple(
                _req(data, "acceptance_criteria"), "acceptance_criteria"
            ),
            reason=str(data.get("reason", "")),
            created_at=float(_req(data, "created_at")),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )


@dataclass(frozen=True)
class GoalContract:
    """目标契约：Lead Run 的锚点。

    冻结设计要求 `objective` 与 `acceptance_criteria` **不得为空**——
    空目标无法判定 SUCCEEDED 还是 PARTIAL，滚动批次会永远规划下去。
    """

    goal_id: str
    objective: str
    acceptance_criteria: tuple[str, ...]
    created_at: float
    constraints: tuple[str, ...] = ()
    amendments: tuple[GoalAmendment, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"未知 schema_version: {self.schema_version}")
        _non_empty_str(self.goal_id, "goal_id")
        # 规格点名 objective 与 acceptance_criteria 不得为空——两个都抛
        # 领域异常 EmptyGoalFieldError（而非通用 ValueError），
        # 调用方才能按「目标不完整」这一语义单独处理。
        if not isinstance(self.objective, str) or not self.objective.strip():
            raise EmptyGoalFieldError("objective")
        if not self.acceptance_criteria:
            raise EmptyGoalFieldError("acceptance_criteria")
        for item in self.acceptance_criteria:
            _non_empty_str(item, "acceptance_criteria 元素")
        for item in self.constraints:
            _non_empty_str(item, "constraints 元素")
        for amd in self.amendments:
            if not isinstance(amd, GoalAmendment):
                raise ValueError("amendments 元素必须是 GoalAmendment")

    @classmethod
    def create(
        cls,
        *,
        goal_id: str,
        objective: str,
        acceptance_criteria: tuple[str, ...] | list[str],
        constraints: tuple[str, ...] | list[str] = (),
        now: float | None = None,
    ) -> "GoalContract":
        return cls(
            goal_id=goal_id,
            objective=objective,
            acceptance_criteria=tuple(acceptance_criteria),
            constraints=tuple(constraints),
            created_at=now if now is not None else time.time(),
        )

    def amend(self, amendment: GoalAmendment) -> "GoalContract":
        """应用一次修订，返回**新的**契约（原对象不变，frozen 不可改写）。"""
        return replace(self, amendments=self.amendments + (amendment,))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "goal_id": self.goal_id,
            "objective": self.objective,
            "acceptance_criteria": list(self.acceptance_criteria),
            "constraints": list(self.constraints),
            "amendments": [a.to_dict() for a in self.amendments],
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "GoalContract":
        if not isinstance(data, dict):
            raise ValueError("GoalContract 必须是字典")
        _check_version(data, "GoalContract")
        raw_amendments = data.get("amendments", [])
        if not isinstance(raw_amendments, list):
            raise ValueError("amendments 必须是数组")
        return cls(
            goal_id=str(_req(data, "goal_id")),
            objective=str(_req(data, "objective")),
            acceptance_criteria=_str_tuple(
                _req(data, "acceptance_criteria"), "acceptance_criteria"
            ),
            constraints=_str_tuple(data.get("constraints"), "constraints"),
            amendments=tuple(
                GoalAmendment.from_dict(item) for item in raw_amendments
            ),
            created_at=float(_req(data, "created_at")),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )


# ---------------------------------------------------------------------------
# 资源声明
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceClaim:
    """一条资源访问声明（(资源, 访问方式) 对）。

    `resource` 存**规范化后**的形态，规范化在构造期完成（见
    `normalize_resource` 的调用方），保证冲突检查比较的是同一坐标系。

    字段名是 `access` 而非 `route`：`route`（执行路线）与「读还是写」
    是两个正交概念，前者是 `ExecutionRoute`，后者是 `ResourceAccessMode`。
    早期把两者塞进同一个枚举，现在拆开且**不留别名**。
    """

    resource: str
    access: ResourceAccessMode

    def __post_init__(self) -> None:
        resource = _non_empty_str(self.resource, "resource")
        if resource != self.resource:
            object.__setattr__(self, "resource", resource)
        if not isinstance(self.access, ResourceAccessMode):
            raise ValueError(
                f"access 必须是 ResourceAccessMode，收到 {self.access!r}"
            )

    @classmethod
    def of(
        cls, resource: str, access: ResourceAccessMode | str
    ) -> "ResourceClaim":
        """按未规范化的原始字符串构造（内部负责规范化）。"""
        from .validation import normalize_resource

        normalized = normalize_resource(resource)
        if isinstance(access, str):
            try:
                access = ResourceAccessMode(access)
            except ValueError as exc:
                raise UnknownResourceError(f"未知访问方式: {access}") from exc
        return cls(resource=normalized, access=access)

    def to_dict(self) -> dict[str, object]:
        return {"resource": self.resource, "access": self.access.value}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "ResourceClaim":
        if not isinstance(data, dict):
            raise ValueError("ResourceClaim 必须是字典")
        try:
            access = ResourceAccessMode(str(_req(data, "access")))
        except ValueError as exc:
            raise ValueError(
                f"未知 ResourceAccessMode: {data.get('access')!r}"
            ) from exc
        return cls(resource=str(_req(data, "resource")), access=access)


# ---------------------------------------------------------------------------
# 规划与提案
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkItemProposal:
    """本批次的一个待校验提案。校验通过才变成 `WorkItem`。

    25D 起新增两个**显式**路由目标字段（25A 域；不影响 25A 行为契约）：

    - `tool_name`：当 `execution_route = TOOL` 时必填，**不是** work_id 的后缀
    - `subagent_type`：当 `execution_route = SUBAGENT` / `SUBAGENT_BATCH` 时必填

    ## 序列化向后兼容（明确范围）

    **`to_dict()` 只在字段非 None 时写出**——老 payload（无新键）形状不变。
    **`from_dict()` 缺键默认为 None**——这意味着：

    - **老 DIRECT_AGENT 路线** payload（含 `execution_route="direct_agent"`、无
      `tool_name` / `subagent_type`）→ 读取后 `tool_name=None, subagent_type=None`，
      满足 `DIRECT_AGENT` 不带这两个字段的不变式 → **正常加载**
    - **老 TOOL / SUBAGENT 路线** payload（无 `tool_name` / `subagent_type`）→
      读取后字段为 `None` → 触发 `__post_init__` 的非空校验 → **构造期 ValueError**

    后者是**有意的**。25D 之前**没有持久化的 TOOL / SUBAGENT 数据**
    （Orchestrator 25E 才接 ToolRegistry 注入 `tool_name`；本字段是 Planner
    25C 之后才会输出的新字段），所以"老 TOOL / SUBAGENT 数据"目前是空集。
    若未来 25E 之前的某个工作流确实产出了无 `tool_name` / `subagent_type` 的
    老持久化数据，**不要为了通过加载而猜测执行目标**——这种数据必须被
    重新规划或显式迁移。
    """

    work_id: str
    objective: str
    depends_on: tuple[str, ...] = ()
    claims: tuple[ResourceClaim, ...] = ()
    # 执行路线：交给谁跑（直接 Agent / 工具 / 子代理 / 子代理批次）。
    # 与 claims 里的 ResourceAccessMode（读还是写）是两个正交概念。
    execution_route: ExecutionRoute = ExecutionRoute.DIRECT_AGENT
    # 显式路由目标：Router 不再从 work_id 后缀解析
    tool_name: str | None = None
    subagent_type: str | None = None
    created_at: float = 0.0
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"未知 schema_version: {self.schema_version}")
        _non_empty_str(self.work_id, "work_id")
        _non_empty_str(self.objective, "objective")
        if not isinstance(self.execution_route, ExecutionRoute):
            raise ValueError(
                f"execution_route 必须是 ExecutionRoute，收到 {self.execution_route!r}"
            )
        if not isinstance(self.tool_name, (str, type(None))):
            raise TypeError(
                f"tool_name 必须是 str 或 None，收到 {type(self.tool_name).__name__}"
            )
        if not isinstance(self.subagent_type, (str, type(None))):
            raise TypeError(
                f"subagent_type 必须是 str 或 None，"
                f"收到 {type(self.subagent_type).__name__}"
            )
        # 路由域一致性（25D 契约收口）
        if self.execution_route is ExecutionRoute.DIRECT_AGENT:
            if self.tool_name is not None or self.subagent_type is not None:
                raise ValueError(
                    "DIRECT_AGENT 不应携带 tool_name/subagent_type"
                )
        elif self.execution_route is ExecutionRoute.TOOL:
            if not self.tool_name or not self.tool_name.strip():
                raise ValueError("TOOL 路由必须给出非空 tool_name")
            if self.subagent_type is not None:
                raise ValueError("TOOL 路由不应携带 subagent_type")
        elif self.execution_route in (
            ExecutionRoute.SUBAGENT,
            ExecutionRoute.SUBAGENT_BATCH,
        ):
            if not self.subagent_type or not self.subagent_type.strip():
                raise ValueError(
                    f"{self.execution_route.value} 路由必须给出非空 subagent_type"
                )
            if self.tool_name is not None:
                raise ValueError(
                    f"{self.execution_route.value} 路由不应携带 tool_name"
                )
        seen: set[str] = set()
        for dep in self.depends_on:
            if not isinstance(dep, str) or not dep.strip():
                raise ValueError("depends_on 元素必须是非空字符串")
            if dep in seen:
                raise ValueError(f"depends_on 重复: {dep}")
            seen.add(dep)
        for claim in self.claims:
            if not isinstance(claim, ResourceClaim):
                raise ValueError("claims 元素必须是 ResourceClaim")

    @property
    def resources(self) -> tuple[str, ...]:
        """本提案声明触及的规范化资源列表（去重、保序）。"""
        seen: dict[str, None] = {}
        for claim in self.claims:
            seen.setdefault(claim.resource, None)
        return tuple(seen)

    def to_dict(self) -> dict[str, object]:
        """序列化——`tool_name` / `subagent_type` 仅在非 None 时写出。

        旧 payload（无新键）能直接被 `from_dict` 读出（字段为 None，
        符合 DIRECT_AGENT 路线的不变式），**不会**让老数据失效。
        """
        result: dict[str, object] = {
            "schema_version": self.schema_version,
            "work_id": self.work_id,
            "objective": self.objective,
            "depends_on": list(self.depends_on),
            "claims": [c.to_dict() for c in self.claims],
            "execution_route": self.execution_route.value,
            "created_at": self.created_at,
        }
        if self.tool_name is not None:
            result["tool_name"] = self.tool_name
        if self.subagent_type is not None:
            result["subagent_type"] = self.subagent_type
        return result

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "WorkItemProposal":
        if not isinstance(data, dict):
            raise ValueError("WorkItemProposal 必须是字典")
        _check_version(data, "WorkItemProposal")
        raw_claims = data.get("claims", [])
        if not isinstance(raw_claims, list):
            raise ValueError("claims 必须是数组")
        route_raw = data.get("execution_route", ExecutionRoute.DIRECT_AGENT.value)
        try:
            execution_route = ExecutionRoute(str(route_raw))
        except ValueError as exc:
            raise ValueError(f"未知 ExecutionRoute: {route_raw!r}") from exc
        return cls(
            work_id=str(_req(data, "work_id")),
            objective=str(_req(data, "objective")),
            depends_on=_str_tuple(data.get("depends_on"), "depends_on"),
            claims=tuple(ResourceClaim.from_dict(c) for c in raw_claims),
            execution_route=execution_route,
            tool_name=_optional_str_field(data, "tool_name"),
            subagent_type=_optional_str_field(data, "subagent_type"),
            created_at=float(data.get("created_at", 0.0)),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )


@dataclass(frozen=True)
class PlanningDecision:
    """一次滚动批次的规划决策（不是一次性完整 DAG）。"""

    decision_id: str
    batch_index: int
    proposals: tuple[WorkItemProposal, ...]
    decided_at: float
    rationale: str = ""
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"未知 schema_version: {self.schema_version}")
        _non_empty_str(self.decision_id, "decision_id")
        if isinstance(self.batch_index, bool) or not isinstance(self.batch_index, int):
            raise ValueError("batch_index 必须是整数")
        if self.batch_index < 0:
            raise ValueError(f"batch_index 不能为负: {self.batch_index}")
        if not self.proposals:
            raise ValueError("proposals 不能为空——空批次不是一次有效决策")
        for item in self.proposals:
            if not isinstance(item, WorkItemProposal):
                raise ValueError("proposals 元素必须是 WorkItemProposal")

    @property
    def work_ids(self) -> tuple[str, ...]:
        return tuple(p.work_id for p in self.proposals)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "batch_index": self.batch_index,
            "proposals": [p.to_dict() for p in self.proposals],
            "decided_at": self.decided_at,
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "PlanningDecision":
        if not isinstance(data, dict):
            raise ValueError("PlanningDecision 必须是字典")
        _check_version(data, "PlanningDecision")
        raw = data.get("proposals", [])
        if not isinstance(raw, list):
            raise ValueError("proposals 必须是数组")
        return cls(
            decision_id=str(_req(data, "decision_id")),
            batch_index=int(_req(data, "batch_index")),
            proposals=tuple(WorkItemProposal.from_dict(p) for p in raw),
            decided_at=float(_req(data, "decided_at")),
            rationale=str(data.get("rationale", "")),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionAttempt:
    """一次执行尝试（首次执行或唯一一次修订，各记一条）。"""

    attempt_id: str
    work_id: str
    sequence: int
    started_at: float
    finished_at: float | None = None
    outcome: str = ""
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"未知 schema_version: {self.schema_version}")
        _non_empty_str(self.attempt_id, "attempt_id")
        _non_empty_str(self.work_id, "work_id")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise ValueError("sequence 必须是整数")
        if self.sequence < 1:
            raise ValueError(f"sequence 从 1 开始: {self.sequence}")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at 不能早于 started_at")

    @property
    def is_revision(self) -> bool:
        """是否是修订执行（sequence >= 2）。"""
        return self.sequence >= 2

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": self.schema_version,
            "attempt_id": self.attempt_id,
            "work_id": self.work_id,
            "sequence": self.sequence,
            "started_at": self.started_at,
            "outcome": self.outcome,
        }
        if self.finished_at is not None:
            result["finished_at"] = self.finished_at
        return result

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "ExecutionAttempt":
        if not isinstance(data, dict):
            raise ValueError("ExecutionAttempt 必须是字典")
        _check_version(data, "ExecutionAttempt")
        finished = data.get("finished_at")
        return cls(
            attempt_id=str(_req(data, "attempt_id")),
            work_id=str(_req(data, "work_id")),
            sequence=int(_req(data, "sequence")),
            started_at=float(_req(data, "started_at")),
            finished_at=float(finished) if finished is not None else None,
            outcome=str(data.get("outcome", "")),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )


@dataclass(frozen=True)
class EvidenceReceipt:
    """证据收据：一次可复核的观测记录。

    只是**证据**，不是事实——必须经过 `VerificationResult` 才能
    晋升为 `VerifiedFact`。
    """

    receipt_id: str
    source: str
    observed_at: float
    content: str = ""
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"未知 schema_version: {self.schema_version}")
        _non_empty_str(self.receipt_id, "receipt_id")
        _non_empty_str(self.source, "source")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "source": self.source,
            "observed_at": self.observed_at,
            "content": self.content,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "EvidenceReceipt":
        if not isinstance(data, dict):
            raise ValueError("EvidenceReceipt 必须是字典")
        _check_version(data, "EvidenceReceipt")
        return cls(
            receipt_id=str(_req(data, "receipt_id")),
            source=str(_req(data, "source")),
            observed_at=float(_req(data, "observed_at")),
            content=str(data.get("content", "")),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )


@dataclass(frozen=True)
class VerificationResult:
    """对某个 WorkItem 产出的验证结论（通过/不通过 + 证据）。"""

    verification_id: str
    work_id: str
    passed: bool
    receipts: tuple[EvidenceReceipt, ...]
    verified_at: float
    notes: str = ""
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"未知 schema_version: {self.schema_version}")
        _non_empty_str(self.verification_id, "verification_id")
        _non_empty_str(self.work_id, "work_id")
        if not isinstance(self.passed, bool):
            raise ValueError("passed 必须是布尔值")
        for receipt in self.receipts:
            if not isinstance(receipt, EvidenceReceipt):
                raise ValueError("receipts 元素必须是 EvidenceReceipt")
        if self.passed and not self.receipts:
            # 通过验证必须有证据支撑，否则「已验证」一文不值
            raise ValueError("验证通过必须至少携带一条 EvidenceReceipt")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "verification_id": self.verification_id,
            "work_id": self.work_id,
            "passed": self.passed,
            "receipts": [r.to_dict() for r in self.receipts],
            "verified_at": self.verified_at,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "VerificationResult":
        if not isinstance(data, dict):
            raise ValueError("VerificationResult 必须是字典")
        _check_version(data, "VerificationResult")
        raw = data.get("receipts", [])
        if not isinstance(raw, list):
            raise ValueError("receipts 必须是数组")
        return cls(
            verification_id=str(_req(data, "verification_id")),
            work_id=str(_req(data, "work_id")),
            passed=bool(_req(data, "passed")),
            receipts=tuple(EvidenceReceipt.from_dict(r) for r in raw),
            verified_at=float(_req(data, "verified_at")),
            notes=str(data.get("notes", "")),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )


@dataclass(frozen=True)
class VerifiedFact:
    """**唯一**能进入后续规划与综合的事实。

    构造期即强制「来自一次通过的验证」——把未验证的中间结论挡在规划输入
    之外，这是本阶段最重要的一道闸门。
    """

    fact_id: str
    statement: str
    work_id: str
    verification_id: str
    receipts: tuple[EvidenceReceipt, ...]
    established_at: float
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"未知 schema_version: {self.schema_version}")
        _non_empty_str(self.fact_id, "fact_id")
        _non_empty_str(self.statement, "statement")
        _non_empty_str(self.work_id, "work_id")
        _non_empty_str(self.verification_id, "verification_id")
        if not self.receipts:
            raise ValueError("VerifiedFact 必须携带至少一条 EvidenceReceipt")
        for receipt in self.receipts:
            if not isinstance(receipt, EvidenceReceipt):
                raise ValueError("receipts 元素必须是 EvidenceReceipt")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "fact_id": self.fact_id,
            "statement": self.statement,
            "work_id": self.work_id,
            "verification_id": self.verification_id,
            "receipts": [r.to_dict() for r in self.receipts],
            "established_at": self.established_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "VerifiedFact":
        if not isinstance(data, dict):
            raise ValueError("VerifiedFact 必须是字典")
        _check_version(data, "VerifiedFact")
        raw = data.get("receipts", [])
        if not isinstance(raw, list):
            raise ValueError("receipts 必须是数组")
        return cls(
            fact_id=str(_req(data, "fact_id")),
            statement=str(_req(data, "statement")),
            work_id=str(_req(data, "work_id")),
            verification_id=str(_req(data, "verification_id")),
            receipts=tuple(EvidenceReceipt.from_dict(r) for r in raw),
            established_at=float(_req(data, "established_at")),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )


# ---------------------------------------------------------------------------
# WorkItem（可变：状态转换）
# ---------------------------------------------------------------------------


@dataclass
class WorkItem:
    """批次内的可执行单元。

    与 `SubagentTask` 同风格：**状态可变（仅限合法转换），字段不可随意
    修改**（校验在 `__post_init__`，转换走状态机）。

    修订规则（冻结设计：最多首次执行 + 一次修订）由 `revised` 标志落实：
    第一次 `RUNNING -> BLOCKED` 后可 `BLOCKED -> READY` 修订一次，
    第二次 `RUNNING -> BLOCKED` 直接抛 `RevisionLimitExceededError`。
    """

    work_id: str
    objective: str
    status: WorkItemStatus
    depends_on: tuple[str, ...]
    claims: tuple[ResourceClaim, ...]
    created_at: float
    attempts: tuple[ExecutionAttempt, ...] = ()
    # 执行路线：由规划/路由阶段决定并随提案带下来（与 claims 的读写方式正交）
    execution_route: ExecutionRoute = ExecutionRoute.DIRECT_AGENT
    # 25D：与 WorkItemProposal 同步——显式路由目标
    tool_name: str | None = None
    subagent_type: str | None = None
    revised: bool = False
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"未知 schema_version: {self.schema_version}")
        _non_empty_str(self.work_id, "work_id")
        _non_empty_str(self.objective, "objective")
        if not isinstance(self.status, WorkItemStatus):
            raise ValueError(f"status 必须是 WorkItemStatus: {self.status!r}")
        if not isinstance(self.execution_route, ExecutionRoute):
            raise ValueError(
                f"execution_route 必须是 ExecutionRoute，收到 {self.execution_route!r}"
            )
        # 25D：与 WorkItemProposal 一致的路由域校验
        if self.execution_route is ExecutionRoute.DIRECT_AGENT:
            if self.tool_name is not None or self.subagent_type is not None:
                raise ValueError(
                    "DIRECT_AGENT 不应携带 tool_name/subagent_type"
                )
        elif self.execution_route is ExecutionRoute.TOOL:
            if not self.tool_name or not self.tool_name.strip():
                raise ValueError("TOOL 路由必须给出非空 tool_name")
            if self.subagent_type is not None:
                raise ValueError("TOOL 路由不应携带 subagent_type")
        elif self.execution_route in (
            ExecutionRoute.SUBAGENT,
            ExecutionRoute.SUBAGENT_BATCH,
        ):
            if not self.subagent_type or not self.subagent_type.strip():
                raise ValueError(
                    f"{self.execution_route.value} 路由必须给出非空 subagent_type"
                )
            if self.tool_name is not None:
                raise ValueError(
                    f"{self.execution_route.value} 路由不应携带 tool_name"
                )
        if not isinstance(self.depends_on, tuple):
            raise ValueError("depends_on 必须是 tuple")
        if not isinstance(self.claims, tuple):
            raise ValueError("claims 必须是 tuple")
        if not isinstance(self.attempts, tuple):
            raise ValueError("attempts 必须是 tuple")
        for attempt in self.attempts:
            if not isinstance(attempt, ExecutionAttempt):
                raise ValueError("attempts 元素必须是 ExecutionAttempt")

    @classmethod
    def from_proposal(
        cls, proposal: WorkItemProposal, *, status: WorkItemStatus | None = None
    ) -> "WorkItem":
        """由校验通过的提案构造执行单元（执行路线与显式目标一并继承）。"""
        return cls(
            work_id=proposal.work_id,
            objective=proposal.objective,
            status=status or WorkItemStatus.READY,
            depends_on=proposal.depends_on,
            claims=proposal.claims,
            created_at=proposal.created_at or time.time(),
            execution_route=proposal.execution_route,
            tool_name=proposal.tool_name,
            subagent_type=proposal.subagent_type,
        )

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self.status)

    @property
    def attempt_count(self) -> int:
        """已发起的执行次数（首次 + 修订）。"""
        return len(self.attempts)

    @property
    def resources(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for claim in self.claims:
            seen.setdefault(claim.resource, None)
        return tuple(seen)

    def transition_to(
        self,
        new_status: WorkItemStatus,
        *,
        now: float | None = None,
        attempt_id: str | None = None,
        outcome: str = "",
    ) -> None:
        """在合法时转换状态；非法抛状态机异常，违反修订上限抛专用异常。

        Args:
            new_status: 目标状态。
            now: 时间戳（默认墙钟），用于记录 attempt 起止。
            attempt_id: 进入 `RUNNING` 时必填，标识这次尝试。
            outcome: 离开 `RUNNING` 时记录到最近一次 attempt 的结果。
        """
        if (
            new_status is WorkItemStatus.BLOCKED
            and self.revised
            and self.status is WorkItemStatus.RUNNING
        ):
            # 第二次执行仍失败：不允许再修一次（否则坏任务永不收敛）
            raise RevisionLimitExceededError(self.work_id, self.attempt_count)

        validate_transition(self.work_id, self.status, new_status)
        stamp = now if now is not None else time.time()

        if new_status is WorkItemStatus.RUNNING:
            if attempt_id is None:
                raise ValueError("进入 RUNNING 必须提供 attempt_id")
            if self.revised and self.attempt_count >= 2:
                raise RevisionLimitExceededError(self.work_id, self.attempt_count)
            self.attempts = self.attempts + (
                ExecutionAttempt(
                    attempt_id=attempt_id,
                    work_id=self.work_id,
                    sequence=self.attempt_count + 1,
                    started_at=stamp,
                ),
            )
        elif self.status is WorkItemStatus.RUNNING and self.attempts:
            # 离开 RUNNING：补记结束时间与结果
            latest = self.attempts[-1]
            self.attempts = self.attempts[:-1] + (
                replace(latest, finished_at=stamp, outcome=outcome),
            )

        # 从 BLOCKED 发起修订（BLOCKED -> READY）即标记「已修订过」，
        # 此后第二次 RUNNING -> BLOCKED 会在入口处抛 RevisionLimitExceededError
        if self.status is WorkItemStatus.BLOCKED and new_status is WorkItemStatus.READY:
            self.revised = True

        self.status = new_status

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": self.schema_version,
            "work_id": self.work_id,
            "objective": self.objective,
            "status": self.status.value,
            "depends_on": list(self.depends_on),
            "claims": [c.to_dict() for c in self.claims],
            "attempts": [a.to_dict() for a in self.attempts],
            "execution_route": self.execution_route.value,
            "revised": self.revised,
            "created_at": self.created_at,
        }
        if self.tool_name is not None:
            result["tool_name"] = self.tool_name
        if self.subagent_type is not None:
            result["subagent_type"] = self.subagent_type
        return result

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "WorkItem":
        if not isinstance(data, dict):
            raise ValueError("WorkItem 必须是字典")
        _check_version(data, "WorkItem")
        try:
            status = WorkItemStatus(str(_req(data, "status")))
        except ValueError as exc:
            raise ValueError(f"未知 WorkItemStatus: {data.get('status')!r}") from exc
        raw_claims = data.get("claims", [])
        raw_attempts = data.get("attempts", [])
        if not isinstance(raw_claims, list):
            raise ValueError("claims 必须是数组")
        if not isinstance(raw_attempts, list):
            raise ValueError("attempts 必须是数组")
        route_raw = data.get("execution_route", ExecutionRoute.DIRECT_AGENT.value)
        try:
            execution_route = ExecutionRoute(str(route_raw))
        except ValueError as exc:
            raise ValueError(f"未知 ExecutionRoute: {route_raw!r}") from exc
        return cls(
            work_id=str(_req(data, "work_id")),
            objective=str(_req(data, "objective")),
            status=status,
            depends_on=_str_tuple(data.get("depends_on"), "depends_on"),
            claims=tuple(ResourceClaim.from_dict(c) for c in raw_claims),
            attempts=tuple(ExecutionAttempt.from_dict(a) for a in raw_attempts),
            execution_route=execution_route,
            tool_name=_optional_str_field(data, "tool_name"),
            subagent_type=_optional_str_field(data, "subagent_type"),
            revised=bool(data.get("revised", False)),
            created_at=float(_req(data, "created_at")),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )


# ---------------------------------------------------------------------------
# LeadRunState（可变：Run 聚合状态）
# ---------------------------------------------------------------------------


@dataclass
class LeadRunState:
    """一次 Lead Run 的完整运行态（可序列化的快照）。

    本对象**不提供任何写 ThreadState 的方法**：冻结设计要求
    「ThreadState 仅在整个 Lead Run 成功后提交」，提交时机属集成阶段，
    领域模型不拥有这个动作。
    """

    lead_run_id: str
    goal: GoalContract
    status: LeadRunStatus
    # 25B：Run 与其作用的线程的关联。**必填非空**——
    # `LeadRunStore.list_by_thread()` 靠它过滤，而规格的 `save()` 签名里
    # 没有 thread_id，说明关联必须落在状态本身。留空会让「按线程找 Run」
    # 悄悄失效，因此不提供默认值。
    thread_id: str
    created_at: float
    batch_index: int = 0
    decisions: tuple[PlanningDecision, ...] = ()
    items: tuple[WorkItem, ...] = ()
    verified_facts: tuple[VerifiedFact, ...] = ()
    updated_at: float = 0.0
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"未知 schema_version: {self.schema_version}")
        _non_empty_str(self.lead_run_id, "lead_run_id")
        _non_empty_str(self.thread_id, "thread_id")
        if not isinstance(self.goal, GoalContract):
            raise ValueError("goal 必须是 GoalContract")
        if not isinstance(self.status, LeadRunStatus):
            raise ValueError(f"status 必须是 LeadRunStatus: {self.status!r}")
        if isinstance(self.batch_index, bool) or not isinstance(self.batch_index, int):
            raise ValueError("batch_index 必须是整数")
        if self.batch_index < 0:
            raise ValueError(f"batch_index 不能为负: {self.batch_index}")
        seen: set[str] = set()
        for item in self.items:
            if not isinstance(item, WorkItem):
                raise ValueError("items 元素必须是 WorkItem")
            if item.work_id in seen:
                raise ValueError(f"items 中 work_id 重复: {item.work_id}")
            seen.add(item.work_id)
        for decision in self.decisions:
            if not isinstance(decision, PlanningDecision):
                raise ValueError("decisions 元素必须是 PlanningDecision")
        for fact in self.verified_facts:
            if not isinstance(fact, VerifiedFact):
                raise ValueError("verified_facts 元素必须是 VerifiedFact")

    @classmethod
    def start(
        cls,
        *,
        lead_run_id: str,
        goal: GoalContract,
        thread_id: str,
        now: float | None = None,
    ) -> "LeadRunState":
        """创建一个处于 CREATED 的新 Run。"""
        stamp = now if now is not None else time.time()
        return cls(
            lead_run_id=lead_run_id,
            goal=goal,
            status=LeadRunStatus.CREATED,
            created_at=stamp,
            thread_id=thread_id,
            updated_at=stamp,
        )

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self.status)

    # --- 状态转换 ---

    def transition_to(
        self, new_status: LeadRunStatus, *, now: float | None = None
    ) -> None:
        """在合法时转换状态；非法抛状态机异常，终态抛 TerminalStateError。"""
        validate_transition(self.lead_run_id, self.status, new_status)
        self.status = new_status
        self.updated_at = now if now is not None else time.time()

    # --- 批次与事实 ---

    @property
    def verified_work_ids(self) -> frozenset[str]:
        """已验证历史的 work_id 集合——滚动批次依赖解析的唯一合法来源之一。"""
        return frozenset(fact.work_id for fact in self.verified_facts)

    def add_decision(self, decision: PlanningDecision) -> None:
        if any(d.decision_id == decision.decision_id for d in self.decisions):
            raise ValueError(f"decision_id 重复: {decision.decision_id}")
        self.decisions = self.decisions + (decision,)

    def add_verified_fact(self, fact: VerifiedFact) -> None:
        """登记一条已验证事实；重复 fact_id 拒绝。"""
        if any(f.fact_id == fact.fact_id for f in self.verified_facts):
            raise ValueError(f"fact_id 重复: {fact.fact_id}")
        self.verified_facts = self.verified_facts + (fact,)

    def add_item(self, item: WorkItem) -> None:
        if any(w.work_id == item.work_id for w in self.items):
            raise ValueError(f"work_id 重复: {item.work_id}")
        self.items = self.items + (item,)

    def item(self, work_id: str) -> WorkItem | None:
        for candidate in self.items:
            if candidate.work_id == work_id:
                return candidate
        return None

    def replace_item(self, item: WorkItem) -> None:
        """按 work_id 原位替换（状态转换后回写）。"""
        index = {w.work_id: i for i, w in enumerate(self.items)}
        if item.work_id not in index:
            raise KeyError(f"work_id 不存在: {item.work_id}")
        mutable = list(self.items)
        mutable[index[item.work_id]] = item
        self.items = tuple(mutable)

    # --- 序列化 ---

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": self.schema_version,
            "lead_run_id": self.lead_run_id,
            "goal": self.goal.to_dict(),
            "status": self.status.value,
            "thread_id": self.thread_id,
            "batch_index": self.batch_index,
            "decisions": [d.to_dict() for d in self.decisions],
            "items": [i.to_dict() for i in self.items],
            "verified_facts": [f.to_dict() for f in self.verified_facts],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        return result

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "LeadRunState":
        if not isinstance(data, dict):
            raise ValueError("LeadRunState 必须是字典")
        _check_version(data, "LeadRunState")
        try:
            status = LeadRunStatus(str(_req(data, "status")))
        except ValueError as exc:
            raise ValueError(f"未知 LeadRunStatus: {data.get('status')!r}") from exc

        def _list(key: str) -> list:
            raw = data.get(key, [])
            if not isinstance(raw, list):
                raise ValueError(f"{key} 必须是数组")
            return raw

        return cls(
            lead_run_id=str(_req(data, "lead_run_id")),
            goal=GoalContract.from_dict(_req(data, "goal")),  # type: ignore[arg-type]
            status=status,
            thread_id=_non_empty_str(_req(data, "thread_id"), "thread_id"),
            batch_index=int(data.get("batch_index", 0)),
            decisions=tuple(
                PlanningDecision.from_dict(d) for d in _list("decisions")
            ),
            items=tuple(WorkItem.from_dict(i) for i in _list("items")),
            verified_facts=tuple(
                VerifiedFact.from_dict(f) for f in _list("verified_facts")
            ),
            created_at=float(_req(data, "created_at")),
            updated_at=float(data.get("updated_at", 0.0)),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )
