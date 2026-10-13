"""Phase 25A 状态机：状态转换与两个正交的枚举。

职责边界：本模块只回答「**能不能转**」与「**怎么访问资源 / 怎么执行**」
两件事，不持有实体、不做 I/O；数据模型在 `models.py`，批次与资源冲突
校验在 `validation.py`。`models` 单向依赖本模块，避免循环导入。

## 两个枚举为什么必须分开

早期实现把「读写方式」塞进了 `ExecutionRoute`，导致一个名字承载两种
毫不相干的语义：

- `ExecutionRoute` = **这条工作怎么执行**（直接 Agent / 工具 / 子代理 /
  子代理批次）—— 决定调度方式
- `ResourceAccessMode` = **对某个资源是读、写还是副作用不明** —— 决定并行冲突

把 `WRITE` 当成一种「执行路线」或把 `SUBAGENT` 当成一种「访问方式」
都说得通字面、却都是错的。两者现已彻底拆开，**不保留任何兼容别名**
——留别名只会让下一个人重新掉进同一个坑。

## 状态表的形状

沿用仓库既有模式（`subagent_models._VALID_TRANSITIONS`）：
`status -> frozenset[可到达状态]`，终态映射到空集，因此「终态不可逆」
是表的自然结果，而不是散落的 if 判断。
"""

from __future__ import annotations

from enum import Enum

from .errors import InvalidTransitionError, TerminalStateError

__all__ = [
    "ExecutionRoute",
    "LeadRunStatus",
    "ResourceAccessMode",
    "TERMINAL_LEAD_STATUSES",
    "TERMINAL_WORK_ITEM_STATUSES",
    "WorkItemStatus",
    "is_terminal",
    "validate_transition",
]


# ---------------------------------------------------------------------------
# 枚举一：执行路线（怎么跑这条工作）
# ---------------------------------------------------------------------------


class ExecutionRoute(str, Enum):
    """一条 WorkItem 的**执行路线**——决定它交给谁跑，与资源读写无关。

    - `DIRECT_AGENT`：由现有 Agent 在父线程内直接执行
    - `TOOL`：作为一次工具调用执行
    - `SUBAGENT`：派给单个 Subagent
    - `SUBAGENT_BATCH`：派给一组并行 Subagent

    资源的读/写请用 `ResourceAccessMode`——那是冲突判定用的正交概念。
    """

    DIRECT_AGENT = "direct_agent"
    TOOL = "tool"
    SUBAGENT = "subagent"
    SUBAGENT_BATCH = "subagent_batch"


class ResourceAccessMode(str, Enum):
    """对某条资源的**访问方式**，用于并行冲突判定。

    `UNKNOWN` 表示副作用不明——按冻结设计保守处理为强制冲突，
    即宁可串行也不并行。
    """

    READ = "read"
    WRITE = "write"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# 枚举二：状态
# ---------------------------------------------------------------------------


class LeadRunStatus(str, Enum):
    """Lead Run 状态（12 个，5 个终态）。

    主干：`CREATED → PLANNING → ROUTING → EXECUTING → VERIFYING
          → SYNTHESIZING → SUCCEEDED / PARTIAL`

    `WAITING_FOR_INPUT` 是**可恢复非终态**，但只从 `PLANNING` 或
    `VERIFYING` 进入、且**只回到 `PLANNING`**——用户澄清本质上是
    「规划信息不足」或「验证发现歧义」，不属于正在执行中的任务，
    因此**不存在** `EXECUTING → WAITING_FOR_INPUT` 这条边。
    """

    CREATED = "created"
    PLANNING = "planning"
    ROUTING = "routing"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    WAITING_FOR_INPUT = "waiting_for_input"
    SYNTHESIZING = "synthesizing"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class WorkItemStatus(str, Enum):
    """WorkItem 状态。

    `BLOCKED` 是**首次执行失败后等待修订**的状态（非终态）：修订一次回到
    `READY` 再跑；第二次执行必须落到终态，不允许无限重试。
    """

    PROPOSED = "proposed"
    READY = "ready"
    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"
    BLOCKED = "blocked"
    REJECTED = "rejected"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


# ---------------------------------------------------------------------------
# 终态集合
# ---------------------------------------------------------------------------

TERMINAL_LEAD_STATUSES: frozenset[LeadRunStatus] = frozenset(
    {
        LeadRunStatus.SUCCEEDED,
        LeadRunStatus.PARTIAL,
        LeadRunStatus.FAILED,
        LeadRunStatus.CANCELLED,
        LeadRunStatus.TIMED_OUT,
    }
)

TERMINAL_WORK_ITEM_STATUSES: frozenset[WorkItemStatus] = frozenset(
    {
        WorkItemStatus.SUCCEEDED,
        WorkItemStatus.REJECTED,
        WorkItemStatus.FAILED,
        WorkItemStatus.CANCELLED,
        WorkItemStatus.TIMED_OUT,
    }
)


def is_terminal(status: LeadRunStatus | WorkItemStatus) -> bool:
    """该状态是否为终态（终态不可逆）。"""
    if isinstance(status, LeadRunStatus):
        return status in TERMINAL_LEAD_STATUSES
    if isinstance(status, WorkItemStatus):
        return status in TERMINAL_WORK_ITEM_STATUSES
    raise TypeError(f"未知状态类型: {type(status).__name__}")


# ---------------------------------------------------------------------------
# 合法转换映射
# ---------------------------------------------------------------------------

# Lead Run 主干与澄清路径（规格给定的关键转换，另按「所有非终态可在
# 合理位置进入 CANCELLED / TIMED_OUT」补齐取消与超时边）：
#
#   CREATED → PLANNING
#   PLANNING → ROUTING | WAITING_FOR_INPUT | FAILED
#   WAITING_FOR_INPUT → PLANNING | CANCELLED | TIMED_OUT      ← 只回 PLANNING
#   ROUTING → EXECUTING | FAILED
#   EXECUTING → VERIFYING | CANCELLED | TIMED_OUT | FAILED     ← 无 WAITING_FOR_INPUT
#   VERIFYING → EXECUTING | PLANNING | WAITING_FOR_INPUT | SYNTHESIZING | FAILED
#   SYNTHESIZING → SUCCEEDED | PARTIAL | FAILED
#
# 取消/超时的「合理位置」判断：
#   CREATED 尚未开始计时 → 只允许 CANCELLED，不允许 TIMED_OUT
#   其余非终态两者的允许
_LEAD_RUN_TRANSITIONS: dict[LeadRunStatus, frozenset[LeadRunStatus]] = {
    LeadRunStatus.CREATED: frozenset(
        {
            LeadRunStatus.PLANNING,
            LeadRunStatus.CANCELLED,
        }
    ),
    LeadRunStatus.PLANNING: frozenset(
        {
            LeadRunStatus.ROUTING,
            LeadRunStatus.WAITING_FOR_INPUT,
            LeadRunStatus.CANCELLED,
            LeadRunStatus.TIMED_OUT,
            LeadRunStatus.FAILED,
        }
    ),
    LeadRunStatus.ROUTING: frozenset(
        {
            LeadRunStatus.EXECUTING,
            LeadRunStatus.CANCELLED,
            LeadRunStatus.TIMED_OUT,
            LeadRunStatus.FAILED,
        }
    ),
    LeadRunStatus.EXECUTING: frozenset(
        {
            LeadRunStatus.VERIFYING,
            LeadRunStatus.CANCELLED,
            LeadRunStatus.TIMED_OUT,
            LeadRunStatus.FAILED,
            # 刻意没有 WAITING_FOR_INPUT：执行中的任务不接受用户澄清，
            # 澄清必须先回到规划或在验证阶段提出。
        }
    ),
    LeadRunStatus.VERIFYING: frozenset(
        {
            LeadRunStatus.EXECUTING,        # 验证未过 -> 返工
            LeadRunStatus.PLANNING,         # 验证发现目标歧义 -> 回规划
            LeadRunStatus.WAITING_FOR_INPUT,
            LeadRunStatus.SYNTHESIZING,
            LeadRunStatus.CANCELLED,
            LeadRunStatus.TIMED_OUT,
            LeadRunStatus.FAILED,
        }
    ),
    LeadRunStatus.WAITING_FOR_INPUT: frozenset(
        {
            # 拿到澄清后一律回 PLANNING：澄清是对规划信息的补充，
            # 而不是「继续往下跑」的信号
            LeadRunStatus.PLANNING,
            LeadRunStatus.CANCELLED,
            LeadRunStatus.TIMED_OUT,
            LeadRunStatus.FAILED,
        }
    ),
    LeadRunStatus.SYNTHESIZING: frozenset(
        {
            LeadRunStatus.SUCCEEDED,
            LeadRunStatus.PARTIAL,
            LeadRunStatus.CANCELLED,
            LeadRunStatus.TIMED_OUT,
            LeadRunStatus.FAILED,
        }
    ),
    # --- 终态：空集 ---
    LeadRunStatus.SUCCEEDED: frozenset(),
    LeadRunStatus.PARTIAL: frozenset(),
    LeadRunStatus.FAILED: frozenset(),
    LeadRunStatus.CANCELLED: frozenset(),
    LeadRunStatus.TIMED_OUT: frozenset(),
}

_WORK_ITEM_TRANSITIONS: dict[WorkItemStatus, frozenset[WorkItemStatus]] = {
    WorkItemStatus.PROPOSED: frozenset(
        {
            WorkItemStatus.READY,        # 批次校验通过
            WorkItemStatus.REJECTED,     # 批次校验拒绝
            WorkItemStatus.CANCELLED,
        }
    ),
    WorkItemStatus.READY: frozenset(
        {WorkItemStatus.RUNNING, WorkItemStatus.CANCELLED, WorkItemStatus.REJECTED}
    ),
    WorkItemStatus.RUNNING: frozenset(
        {
            WorkItemStatus.SUCCEEDED,
            WorkItemStatus.BLOCKED,              # 首次失败 -> 等待唯一一次修订
            WorkItemStatus.WAITING_FOR_INPUT,
            WorkItemStatus.CANCELLED,
            WorkItemStatus.TIMED_OUT,
            WorkItemStatus.FAILED,               # 已用尽修订次数的最终失败
        }
    ),
    WorkItemStatus.WAITING_FOR_INPUT: frozenset(
        {WorkItemStatus.RUNNING, WorkItemStatus.CANCELLED, WorkItemStatus.FAILED}
    ),
    # BLOCKED -> READY：发起一次修订；不允许直接去终态之外的其他状态
    WorkItemStatus.BLOCKED: frozenset(
        {WorkItemStatus.READY, WorkItemStatus.CANCELLED}
    ),
    # --- 终态：空集 ---
    WorkItemStatus.REJECTED: frozenset(),
    WorkItemStatus.SUCCEEDED: frozenset(),
    WorkItemStatus.FAILED: frozenset(),
    WorkItemStatus.CANCELLED: frozenset(),
    WorkItemStatus.TIMED_OUT: frozenset(),
}

_TABLES: dict[type[Enum], dict[Enum, frozenset[Enum]]] = {
    LeadRunStatus: _LEAD_RUN_TRANSITIONS,  # type: ignore[dict-item]
    WorkItemStatus: _WORK_ITEM_TRANSITIONS,  # type: ignore[dict-item]
}


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def validate_transition(
    entity_id: str,
    current: LeadRunStatus | WorkItemStatus,
    target: LeadRunStatus | WorkItemStatus,
) -> None:
    """校验状态转换；非法时抛专用异常。

    两层判定的顺序有讲究：**先判终态再查转换表**——终态虽然也查不到边，
    但单独抛 `TerminalStateError`（`InvalidTransitionError` 的子类），
    调用方可以把它当「迟到回调，忽略即可」，与「状态表用错」区别对待。

    Raises:
        TypeError: current/target 类型不匹配或未知。
        TerminalStateError: current 已是终态（终态不可逆）。
        InvalidTransitionError: 转换表中不存在该边。
    """
    if type(current) is not type(target):
        raise TypeError(
            f"状态类型不一致: {type(current).__name__} -> {type(target).__name__}"
        )
    table = _TABLES.get(type(current))
    if table is None:
        raise TypeError(f"未知状态类型: {type(current).__name__}")

    if is_terminal(current):
        raise TerminalStateError(entity_id, current.value, target.value)

    allowed = table.get(current, frozenset())
    if target not in allowed:
        raise InvalidTransitionError(entity_id, current.value, target.value)
