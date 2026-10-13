"""Phase 25A Lead Agent 领域层。

位于现有 Agent/Subagent **之上**的编排层领域模型：目标契约、运行状态机、
滚动批次校验、资源冲突判定。本包是**纯领域代码**：

- 无 I/O：不碰数据库、网络、文件系统
- 无副作用：`import mini_deerflow.lead_agent` 不创建线程、连接或 Runtime
- 不执行 Planner 模型调用、Router 调度、Executor/Reviewer/Synthesizer
  （那些属于后续阶段）

设计文档见 `docs/lead-agent-architecture.md`。
"""

from .errors import (
    BatchValidationError,
    CyclicDependencyError,
    DuplicateWorkIdError,
    EmptyGoalFieldError,
    EventRunMismatchError,
    InvalidTransitionError,
    LeadAgentError,
    LeadPlannerBudgetError,
    LeadPlannerDeterministicError,
    LeadPlannerError,
    LeadPlannerMaxAttemptsError,
    LeadPlannerSchemaError,
    LeadRunAlreadyExistsError,
    LeadRunConflictError,
    LeadRunNotFoundError,
    LeadStoreClosedError,
    LeadStoreDataCorruptedError,
    LeadStoreError,
    LeadStoreWriteError,
    LeaseNotFoundError,
    MissingDependencyError,
    ResourceConflictError,
    RevisionLimitExceededError,
    SelfDependencyError,
    TerminalStateError,
    UnknownResourceError,
)
from .models import (
    EvidenceReceipt,
    ExecutionAttempt,
    GoalAmendment,
    GoalContract,
    LeadRunState,
    PlanningDecision,
    ResourceClaim,
    VerificationResult,
    VerifiedFact,
    WorkItem,
    WorkItemProposal,
)
from .state_machine import (
    TERMINAL_LEAD_STATUSES,
    TERMINAL_WORK_ITEM_STATUSES,
    ExecutionRoute,
    LeadRunStatus,
    ResourceAccessMode,
    WorkItemStatus,
    is_terminal,
    validate_transition,
)
from .validation import (
    check_resource_conflicts,
    conflicts,
    is_path_resource,
    normalize_resource,
    validate_batch,
    validate_parallel_safe,
)

# 25B：持久化与预算账本
from .budget_ledger import (
    DEFAULT_MAX_BATCHES,
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_MAX_ITEMS_PER_BATCH,
    DEFAULT_MAX_REVISIONS_PER_ITEM,
    DEFAULT_MAX_WALL_TIME_SECONDS,
    DEFAULT_MAX_WORK_ITEMS,
    BudgetLease,
    LeadBudgetLedger,
)
from .events import LeadDomainEvent, event_types
from .sqlite_store import SQLiteLeadRunStore
from .store import (
    DEFAULT_EVENT_PAGE,
    MAX_EVENT_PAGE_LIMIT,
    MAX_LIST_LIMIT,
    InMemoryLeadRunStore,
    LeadRunStore,
    checked_limit,
    checked_offset,
)

# 25C：LeadPlanner 内部服务
from .planner import (
    BudgetSnapshot,
    DEFAULT_MAX_PLANNING_ATTEMPTS,
    DefaultLeadPlanner,
    LeadPlanner,
    PlannerContext,
    PlannerOutput,
    ProposeBatch,
    RecommendSynthesis,
    RequestClarification,
    WorkItemSummary,
    build_budget_snapshot,
    default_system_prompt,
    summarize_recent_items,
)
from .planner_budget import (
    BudgetRootBudget,
    LeadLedgerRootBudget,
    RootBudget,
)

# 25D：LeadRouter
from .router import (
    DEFAULT_FORBIDDEN_SUBAGENT_TYPES,
    ExecutionDecision,
    RejectionKind,
    RoutedExecutionPlan,
    RoutingContext,
    RoutingRejection,
    ValidatedBatch,
    ValidatedWorkItem,
    route,
)

__all__ = [
    # 状态机与两个正交枚举
    "TERMINAL_LEAD_STATUSES",
    "TERMINAL_WORK_ITEM_STATUSES",
    "ExecutionRoute",
    "LeadRunStatus",
    "ResourceAccessMode",
    "WorkItemStatus",
    "is_terminal",
    "validate_transition",
    # 领域模型
    "EvidenceReceipt",
    "ExecutionAttempt",
    "GoalAmendment",
    "GoalContract",
    "LeadRunState",
    "PlanningDecision",
    "ResourceClaim",
    "VerificationResult",
    "VerifiedFact",
    "WorkItem",
    "WorkItemProposal",
    # 批次校验与资源冲突
    "check_resource_conflicts",
    "conflicts",
    "is_path_resource",
    "normalize_resource",
    "validate_batch",
    "validate_parallel_safe",
    # 持久化（25B）
    "DEFAULT_EVENT_PAGE",
    "InMemoryLeadRunStore",
    "LeadRunStore",
    "MAX_EVENT_PAGE_LIMIT",
    "MAX_LIST_LIMIT",
    "SQLiteLeadRunStore",
    "checked_limit",
    "checked_offset",
    # 审计事件与预算账本（25B）
    "DEFAULT_MAX_BATCHES",
    "DEFAULT_MAX_CONCURRENCY",
    "DEFAULT_MAX_ITEMS_PER_BATCH",
    "DEFAULT_MAX_REVISIONS_PER_ITEM",
    "DEFAULT_MAX_WALL_TIME_SECONDS",
    "DEFAULT_MAX_WORK_ITEMS",
    "BudgetLease",
    "LeadBudgetLedger",
    "LeadDomainEvent",
    "event_types",
    # LeadPlanner（25C）
    "BudgetSnapshot",
    "DEFAULT_MAX_PLANNING_ATTEMPTS",
    "DefaultLeadPlanner",
    "LeadPlanner",
    "PlannerContext",
    "PlannerOutput",
    "ProposeBatch",
    "RecommendSynthesis",
    "RequestClarification",
    "WorkItemSummary",
    "build_budget_snapshot",
    "default_system_prompt",
    "summarize_recent_items",
    # Planner 预算适配边界（25C 架构收口）
    "BudgetRootBudget",
    "LeadLedgerRootBudget",
    "RootBudget",
    # LeadRouter（25D）
    "DEFAULT_FORBIDDEN_SUBAGENT_TYPES",
    "ExecutionDecision",
    "RejectionKind",
    "RoutedExecutionPlan",
    "RoutingContext",
    "RoutingRejection",
    "ValidatedBatch",
    "ValidatedWorkItem",
    "route",
    # 异常
    "BatchValidationError",
    "CyclicDependencyError",
    "DuplicateWorkIdError",
    "EmptyGoalFieldError",
    "EventRunMismatchError",
    "InvalidTransitionError",
    "LeadAgentError",
    "LeadPlannerBudgetError",
    "LeadPlannerDeterministicError",
    "LeadPlannerError",
    "LeadPlannerMaxAttemptsError",
    "LeadPlannerSchemaError",
    "LeadRunAlreadyExistsError",
    "LeadRunConflictError",
    "LeadRunNotFoundError",
    "LeadStoreClosedError",
    "LeadStoreDataCorruptedError",
    "LeadStoreError",
    "LeadStoreWriteError",
    "LeaseNotFoundError",
    "MissingDependencyError",
    "ResourceConflictError",
    "RevisionLimitExceededError",
    "SelfDependencyError",
    "TerminalStateError",
    "UnknownResourceError",
]
