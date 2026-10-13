"""Phase 25A / 状态机测试。

覆盖规格必测场景：全部合法与非法状态转换、终态不可逆、PARTIAL 终态、
WAITING_FOR_INPUT 可恢复。

关键做法：期望转换表在测试里**独立写一份**，不 import 生产侧的
`_LEAD_RUN_TRANSITIONS` / `_WORK_ITEM_TRANSITIONS`——否则等于拿生产表
验证生产表，永远绿。两份表不一致时这里会红。
"""

from __future__ import annotations

import pytest

from mini_deerflow.lead_agent import (
    ExecutionRoute,
    InvalidTransitionError,
    LeadRunStatus,
    ResourceAccessMode,
    TerminalStateError,
    WorkItemStatus,
    is_terminal,
    validate_transition,
)

# ---------------------------------------------------------------------------
# 期望转换表（独立于实现）
# ---------------------------------------------------------------------------

EXPECTED_LEAD_RUN: dict[LeadRunStatus, frozenset[LeadRunStatus]] = {
    LeadRunStatus.CREATED: frozenset(
        {LeadRunStatus.PLANNING, LeadRunStatus.CANCELLED}
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
            # 刻意没有 WAITING_FOR_INPUT：澄清不属于正在执行的任务
        }
    ),
    LeadRunStatus.VERIFYING: frozenset(
        {
            LeadRunStatus.EXECUTING,
            LeadRunStatus.PLANNING,
            LeadRunStatus.WAITING_FOR_INPUT,
            LeadRunStatus.SYNTHESIZING,
            LeadRunStatus.CANCELLED,
            LeadRunStatus.TIMED_OUT,
            LeadRunStatus.FAILED,
        }
    ),
    LeadRunStatus.WAITING_FOR_INPUT: frozenset(
        {
            LeadRunStatus.PLANNING,       # 澄清后只回规划，不直接继续执行
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
    LeadRunStatus.SUCCEEDED: frozenset(),
    LeadRunStatus.PARTIAL: frozenset(),
    LeadRunStatus.FAILED: frozenset(),
    LeadRunStatus.CANCELLED: frozenset(),
    LeadRunStatus.TIMED_OUT: frozenset(),
}

EXPECTED_WORK_ITEM: dict[WorkItemStatus, frozenset[WorkItemStatus]] = {
    WorkItemStatus.PROPOSED: frozenset(
        {WorkItemStatus.READY, WorkItemStatus.REJECTED, WorkItemStatus.CANCELLED}
    ),
    WorkItemStatus.READY: frozenset(
        {WorkItemStatus.RUNNING, WorkItemStatus.CANCELLED, WorkItemStatus.REJECTED}
    ),
    WorkItemStatus.RUNNING: frozenset(
        {
            WorkItemStatus.SUCCEEDED,
            WorkItemStatus.BLOCKED,
            WorkItemStatus.WAITING_FOR_INPUT,
            WorkItemStatus.CANCELLED,
            WorkItemStatus.TIMED_OUT,
            WorkItemStatus.FAILED,
        }
    ),
    WorkItemStatus.WAITING_FOR_INPUT: frozenset(
        {WorkItemStatus.RUNNING, WorkItemStatus.CANCELLED, WorkItemStatus.FAILED}
    ),
    WorkItemStatus.BLOCKED: frozenset(
        {WorkItemStatus.READY, WorkItemStatus.CANCELLED}
    ),
    WorkItemStatus.REJECTED: frozenset(),
    WorkItemStatus.SUCCEEDED: frozenset(),
    WorkItemStatus.FAILED: frozenset(),
    WorkItemStatus.CANCELLED: frozenset(),
    WorkItemStatus.TIMED_OUT: frozenset(),
}

LEAD_TERMINAL = {
    LeadRunStatus.SUCCEEDED,
    LeadRunStatus.PARTIAL,
    LeadRunStatus.FAILED,
    LeadRunStatus.CANCELLED,
    LeadRunStatus.TIMED_OUT,
}

WORK_ITEM_TERMINAL = {
    WorkItemStatus.SUCCEEDED,
    WorkItemStatus.REJECTED,
    WorkItemStatus.FAILED,
    WorkItemStatus.CANCELLED,
    WorkItemStatus.TIMED_OUT,
}


# ---------------------------------------------------------------------------
# 穷举全部 (current, target) 组合
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("current", list(LeadRunStatus))
@pytest.mark.parametrize("target", list(LeadRunStatus))
def test_every_lead_run_transition_matches_spec(
    current: LeadRunStatus, target: LeadRunStatus
) -> None:
    should_succeed = target in EXPECTED_LEAD_RUN[current]
    if should_succeed:
        validate_transition("run-1", current, target)  # 不应抛
    else:
        with pytest.raises(InvalidTransitionError):
            validate_transition("run-1", current, target)


@pytest.mark.parametrize("current", list(WorkItemStatus))
@pytest.mark.parametrize("target", list(WorkItemStatus))
def test_every_work_item_transition_matches_spec(
    current: WorkItemStatus, target: WorkItemStatus
) -> None:
    should_succeed = target in EXPECTED_WORK_ITEM[current]
    if should_succeed:
        validate_transition("w-1", current, target)
    else:
        with pytest.raises(InvalidTransitionError):
            validate_transition("w-1", current, target)


def test_transition_counts() -> None:
    """守卫测试：状态枚举规模一旦变化，穷举测试要同步复核。"""
    assert len(LeadRunStatus) == 12
    assert len(WorkItemStatus) == 10


# ---------------------------------------------------------------------------
# 终态不可逆
# ---------------------------------------------------------------------------


def test_lead_run_terminal_statuses_match_spec() -> None:
    """冻结设计的五个终态，一个不多一个不少。"""
    assert LEAD_TERMINAL == {
        LeadRunStatus.SUCCEEDED,
        LeadRunStatus.PARTIAL,
        LeadRunStatus.FAILED,
        LeadRunStatus.CANCELLED,
        LeadRunStatus.TIMED_OUT,
    }
    assert {s for s in LeadRunStatus if is_terminal(s)} == LEAD_TERMINAL


@pytest.mark.parametrize("terminal", sorted(LEAD_TERMINAL, key=lambda s: s.value))
@pytest.mark.parametrize("target", list(LeadRunStatus))
def test_lead_run_terminal_is_irreversible(
    terminal: LeadRunStatus, target: LeadRunStatus
) -> None:
    """终态 → 任何状态（含自身）都必须抛 TerminalStateError。"""
    with pytest.raises(TerminalStateError):
        validate_transition("run-1", terminal, target)


@pytest.mark.parametrize("terminal", sorted(WORK_ITEM_TERMINAL, key=lambda s: s.value))
@pytest.mark.parametrize("target", list(WorkItemStatus))
def test_work_item_terminal_is_irreversible(
    terminal: WorkItemStatus, target: WorkItemStatus
) -> None:
    with pytest.raises(TerminalStateError):
        validate_transition("w-1", terminal, target)


def test_terminal_error_is_invalid_transition_subclass() -> None:
    """调用方可以只捕获基类，也能按终态语义细分。"""
    assert issubclass(TerminalStateError, InvalidTransitionError)


# ---------------------------------------------------------------------------
# 关键语义
# ---------------------------------------------------------------------------


def test_partial_is_terminal_and_unreachable_from_itself() -> None:
    """PARTIAL 是终态（规格必测）。"""
    assert is_terminal(LeadRunStatus.PARTIAL)
    with pytest.raises(TerminalStateError):
        validate_transition("run-1", LeadRunStatus.PARTIAL, LeadRunStatus.SUCCEEDED)
    with pytest.raises(TerminalStateError):
        validate_transition("run-1", LeadRunStatus.PARTIAL, LeadRunStatus.PARTIAL)


def test_partial_reachable_only_through_synthesis() -> None:
    """PARTIAL 是终态，且只能在收口阶段（SYNTHESIZING）抵达。

    执行或验证阶段不能直接宣告「部分完成」——部分完成是**综合之后**
    对结果的判断，不是执行过程中的中间态。
    """
    assert is_terminal(LeadRunStatus.PARTIAL)
    with pytest.raises(InvalidTransitionError):
        validate_transition("run-1", LeadRunStatus.EXECUTING, LeadRunStatus.PARTIAL)
    with pytest.raises(InvalidTransitionError):
        validate_transition("run-1", LeadRunStatus.VERIFYING, LeadRunStatus.PARTIAL)
    validate_transition(
        "run-1", LeadRunStatus.SYNTHESIZING, LeadRunStatus.PARTIAL
    )


def test_waiting_for_input_is_recoverable_non_terminal() -> None:
    """WAITING_FOR_INPUT 可恢复（非终态），且只经 PLANNING 闭环。"""
    assert is_terminal(LeadRunStatus.WAITING_FOR_INPUT) is False
    # 进入等待：只允许从规划或验证
    validate_transition(
        "run-1", LeadRunStatus.PLANNING, LeadRunStatus.WAITING_FOR_INPUT
    )
    validate_transition(
        "run-1", LeadRunStatus.VERIFYING, LeadRunStatus.WAITING_FOR_INPUT
    )
    # 恢复：拿到澄清回 PLANNING
    validate_transition(
        "run-1", LeadRunStatus.WAITING_FOR_INPUT, LeadRunStatus.PLANNING
    )
    # 但不能从 WAITING 直接「成功」——必须重新规划并走完整流程
    with pytest.raises(InvalidTransitionError):
        validate_transition(
            "run-1", LeadRunStatus.WAITING_FOR_INPUT, LeadRunStatus.SUCCEEDED
        )
    with pytest.raises(InvalidTransitionError):
        validate_transition(
            "run-1", LeadRunStatus.WAITING_FOR_INPUT, LeadRunStatus.EXECUTING
        )


def test_waiting_for_input_work_item_recovers_to_running() -> None:
    validate_transition(
        "w-1", WorkItemStatus.RUNNING, WorkItemStatus.WAITING_FOR_INPUT
    )
    validate_transition(
        "w-1", WorkItemStatus.WAITING_FOR_INPUT, WorkItemStatus.RUNNING
    )


def test_rolling_batch_edge_goes_through_verify_and_synth() -> None:
    """滚动批次的推进路径：EXECUTING → VERIFYING → SYNTHESIZING → 终态。

    不再有 `EXECUTING → PLANNING` 这种「执行中直接跳回规划」的边：
    批次推进必须先经验证（VERIFYING），只有验证发现问题才回 PLANNING。
    """
    validate_transition(
        "run-1", LeadRunStatus.EXECUTING, LeadRunStatus.VERIFYING
    )
    validate_transition(
        "run-1", LeadRunStatus.VERIFYING, LeadRunStatus.SYNTHESIZING
    )
    validate_transition(
        "run-1", LeadRunStatus.SYNTHESIZING, LeadRunStatus.SUCCEEDED
    )
    with pytest.raises(InvalidTransitionError):
        validate_transition(
            "run-1", LeadRunStatus.EXECUTING, LeadRunStatus.PLANNING
        )
    # 规划中不能直接跳到执行完成
    with pytest.raises(InvalidTransitionError):
        validate_transition(
            "run-1", LeadRunStatus.PLANNING, LeadRunStatus.SUCCEEDED
        )


def test_verification_can_return_to_executing_or_planning() -> None:
    """验证是分叉点：返工回 EXECUTING、目标歧义回 PLANNING、
    信息不足进 WAITING_FOR_INPUT、通过进 SYNTHESIZING。"""
    for target in (
        LeadRunStatus.EXECUTING,
        LeadRunStatus.PLANNING,
        LeadRunStatus.WAITING_FOR_INPUT,
        LeadRunStatus.SYNTHESIZING,
    ):
        validate_transition("run-1", LeadRunStatus.VERIFYING, target)


def test_clarification_never_pauses_execution() -> None:
    """澄清只发生在规划或验证阶段：EXECUTING 不接受 WAITING_FOR_INPUT。"""
    with pytest.raises(InvalidTransitionError):
        validate_transition(
            "run-1", LeadRunStatus.EXECUTING, LeadRunStatus.WAITING_FOR_INPUT
        )
    # 规划与验证阶段可以进入等待
    validate_transition(
        "run-1", LeadRunStatus.PLANNING, LeadRunStatus.WAITING_FOR_INPUT
    )
    validate_transition(
        "run-1", LeadRunStatus.VERIFYING, LeadRunStatus.WAITING_FOR_INPUT
    )


def test_clarification_resumes_at_planning_not_executing() -> None:
    """拿到澄清一律回 PLANNING——澄清是对规划信息的补充，
    不是「继续往下跑」的信号，因此 WAITING 不能直达 EXECUTING。"""
    validate_transition(
        "run-1", LeadRunStatus.WAITING_FOR_INPUT, LeadRunStatus.PLANNING
    )
    with pytest.raises(InvalidTransitionError):
        validate_transition(
            "run-1", LeadRunStatus.WAITING_FOR_INPUT, LeadRunStatus.EXECUTING
        )


def test_created_only_reaches_planning() -> None:
    """CREATED 是起点：只能进入 PLANNING（或被取消）。"""
    validate_transition("run-1", LeadRunStatus.CREATED, LeadRunStatus.PLANNING)
    validate_transition("run-1", LeadRunStatus.CREATED, LeadRunStatus.CANCELLED)
    with pytest.raises(InvalidTransitionError):
        validate_transition(
            "run-1", LeadRunStatus.CREATED, LeadRunStatus.EXECUTING
        )
    # 尚未开始计时的任务不会 TIMED_OUT
    with pytest.raises(InvalidTransitionError):
        validate_transition(
            "run-1", LeadRunStatus.CREATED, LeadRunStatus.TIMED_OUT
        )


def test_routing_must_go_through_executing() -> None:
    """ROUTING 是调度决策点：只能进 EXECUTING 或失败。"""
    validate_transition("run-1", LeadRunStatus.ROUTING, LeadRunStatus.EXECUTING)
    with pytest.raises(InvalidTransitionError):
        validate_transition(
            "run-1", LeadRunStatus.ROUTING, LeadRunStatus.SUCCEEDED
        )


def test_work_item_blocked_is_recoverable_not_terminal() -> None:
    """BLOCKED（待修订）非终态，可回到 READY。"""
    assert is_terminal(WorkItemStatus.BLOCKED) is False
    validate_transition("w-1", WorkItemStatus.BLOCKED, WorkItemStatus.READY)


def test_type_mismatch_raises_type_error() -> None:
    with pytest.raises(TypeError):
        validate_transition("x", LeadRunStatus.CREATED, WorkItemStatus.READY)


def test_unknown_status_type_raises_type_error() -> None:
    with pytest.raises(TypeError):
        validate_transition("x", "created", "planning")  # type: ignore[arg-type]


def test_error_carries_context() -> None:
    try:
        validate_transition("run-42", LeadRunStatus.SUCCEEDED, LeadRunStatus.PLANNING)
    except TerminalStateError as exc:
        assert exc.entity_id == "run-42"
        assert exc.from_status == "succeeded"
        assert exc.to_status == "planning"
        assert "run-42" in str(exc)
    else:
        pytest.fail("应当抛 TerminalStateError")


def test_is_terminal_rejects_unknown_type() -> None:
    with pytest.raises(TypeError):
        is_terminal("succeeded")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 两个枚举的职责拆分
# ---------------------------------------------------------------------------


def test_execution_route_is_about_how_work_runs() -> None:
    """`ExecutionRoute` 只描述「这条工作怎么执行」，不含任何读写语义。"""
    assert {e.value for e in ExecutionRoute} == {
        "direct_agent",
        "tool",
        "subagent",
        "subagent_batch",
    }
    # 反序列化可用值构造，且与 context_state 同风格
    assert ExecutionRoute("subagent") is ExecutionRoute.SUBAGENT


def test_resource_access_mode_is_about_read_write() -> None:
    """`ResourceAccessMode` 只描述「对资源读还是写」，不含执行语义。"""
    assert {e.value for e in ResourceAccessMode} == {"read", "write", "unknown"}
    assert ResourceAccessMode("write") is ResourceAccessMode.WRITE


def test_two_enums_are_disjoint_and_no_aliases_left() -> None:
    """拆分后不允许残留语义混乱的别名——两组值不能有交集。"""
    assert not ({e.value for e in ExecutionRoute} & {e.value for e in ResourceAccessMode})
    # 旧的 ExecutionRoute.READ 之类别名必须彻底消失
    for legacy in ("READ", "WRITE", "UNKNOWN"):
        assert not hasattr(ExecutionRoute, legacy), f"ExecutionRoute 残留旧别名 {legacy}"
    for legacy in ("DIRECT_AGENT", "TOOL", "SUBAGENT", "SUBAGENT_BATCH"):
        assert not hasattr(ResourceAccessMode, legacy), f"ResourceAccessMode 残留 {legacy}"
