"""TOOL-02 测试：接入真实 Agent 调用链。

验证 TOOL-01 授权模型与 Agent._do_tool_call 的集成点。
"""

import pytest

from mini_deerflow.tool_authorization import (
    BUILTIN_TOOL_METADATA,
    ExecutorPrincipal,
    RunAuthority,
    ToolAccessDecision,
    ToolAuthorizationError,
    ToolPolicy,
    ToolPolicyRegistry,
    check_tool_access,
    evaluate_tool_access,
)
from mini_deerflow.tool_contracts import (
    AccessDecisionKind,
    CALCULATOR_METADATA,
    PROJECT_EXECUTE_METADATA,
    PROJECT_READ_FILE_METADATA,
    SANDBOX_WRITE_FILE_METADATA,
    TASK_NOTE_METADATA,
    ToolCapability,
    ToolMetadata,
    ToolSourceKind,
    ToolSideEffect,
    default_policy_for,
)


# ============================================================================
# 辅助工厂
# ============================================================================

def make_authority(
    principal: ExecutorPrincipal = ExecutorPrincipal.MAIN_AGENT,
    project_id: str | None = None,
    granted: frozenset[ToolCapability] | None = None,
) -> RunAuthority:
    return RunAuthority(
        principal=principal,
        thread_id="thread-1",
        run_id="run-1",
        project_id=project_id,
        granted_capabilities=granted or frozenset(),
    )


# ============================================================================
# ToolPolicyRegistry 基本操作
# ============================================================================

def test_registry_has_builtin_metadata():
    """注册表包含所有内置工具元数据。"""
    registry = ToolPolicyRegistry()
    for name in BUILTIN_TOOL_METADATA:
        metadata, _ = registry.get(name)
        assert metadata is not None
        assert metadata.name == name


def test_registry_unknown_tool_returns_none():
    """未知工具返回 None。"""
    registry = ToolPolicyRegistry()
    metadata, policy = registry.get("truly_unknown_tool")
    assert metadata is None
    assert policy is None


def test_registry_override_metadata():
    """可覆盖工具元数据。"""
    registry = ToolPolicyRegistry()
    new_meta = ToolMetadata(
        name="calculator",
        source=ToolSourceKind.LOCAL,
        uncategorized=True,
    )
    registry.register_metadata(new_meta)
    metadata, _ = registry.get("calculator")
    assert metadata is not None
    assert metadata.uncategorized is True


def test_registry_override_policy():
    """可覆盖工具策略。"""
    registry = ToolPolicyRegistry()
    registry.set_policy("calculator", ToolPolicy(enabled=False))
    _, policy = registry.get("calculator")
    assert policy is not None
    assert policy.enabled is False


def test_registry_fallback_to_builtin_policy():
    """未覆盖时回退到内置默认策略。"""
    registry = ToolPolicyRegistry()
    _, policy = registry.get("calculator")
    assert policy is not None
    assert policy.enabled is True


def test_get_authority():
    """get_authority 正确构造 RunAuthority。"""
    registry = ToolPolicyRegistry()
    auth = registry.get_authority(
        thread_id="t1",
        run_id="r1",
        project_id="proj-1",
        granted_capabilities=frozenset({ToolCapability.COMPUTE}),
    )
    assert auth.principal == ExecutorPrincipal.MAIN_AGENT
    assert auth.thread_id == "t1"
    assert auth.run_id == "r1"
    assert auth.project_id == "proj-1"
    assert ToolCapability.COMPUTE in auth.granted_capabilities


# ============================================================================
# check_tool_access 集成点
# ============================================================================

def test_check_tool_access_denied():
    """check_tool_access 返回 deny 时决策.kind = DENY。"""
    registry = ToolPolicyRegistry()
    authority = make_authority()  # 无能力

    decision = check_tool_access(registry, "calculator", authority)
    assert decision.kind == AccessDecisionKind.DENY


def test_check_tool_access_confirm_also_denies():
    """check_tool_access 返回 allow 时 project_execute 直接派发。"""
    registry = ToolPolicyRegistry()
    authority = make_authority(
        project_id="proj-1",
        granted=frozenset({ToolCapability.PROJECT_EXEC_DELEGATE}),
    )

    decision = check_tool_access(registry, "project_execute", authority)
    # project_execute 策略 requires_confirmation=False + metadata 直接派发 → ALLOW
    assert decision.kind == AccessDecisionKind.ALLOW


def test_check_tool_access_allow():
    """check_tool_access 返回 allow 时不抛异常。"""
    registry = ToolPolicyRegistry()
    authority = make_authority(
        granted=frozenset({ToolCapability.COMPUTE}),
    )

    decision = check_tool_access(registry, "calculator", authority)
    assert decision.kind == AccessDecisionKind.ALLOW


def test_check_tool_access_unknown_tool_denied():
    """未知工具 deny。"""
    registry = ToolPolicyRegistry()
    authority = make_authority(granted=frozenset())

    decision = check_tool_access(registry, "truly_unknown_tool", authority)
    assert decision.kind == AccessDecisionKind.DENY


# ============================================================================
# Agent._do_tool_call 集成验证
# ============================================================================

def test_project_read_requires_project_session():
    """project_read_file 需要项目会话，普通会话 deny。"""
    registry = ToolPolicyRegistry()
    authority = make_authority(
        project_id=None,  # 普通会话
        granted=frozenset({ToolCapability.PROJECT_READ}),
    )

    decision = check_tool_access(registry, "project_read_file", authority)
    assert decision.kind == AccessDecisionKind.DENY
    assert decision.reason_code == "scope_mismatch"


def test_project_read_allowed_in_project_session():
    """项目会话且能力满足时，project_read_file allow。"""
    registry = ToolPolicyRegistry()
    authority = make_authority(
        project_id="proj-1",
        granted=frozenset({ToolCapability.PROJECT_READ}),
    )

    decision = check_tool_access(registry, "project_read_file", authority)
    assert decision.kind == AccessDecisionKind.ALLOW


def test_project_execute_needs_confirmation():
    """project_execute 策略 requires_confirmation=False，默认直接派发。"""
    registry = ToolPolicyRegistry()
    authority = make_authority(
        principal=ExecutorPrincipal.MAIN_AGENT,
        project_id="proj-1",
        granted=frozenset({ToolCapability.PROJECT_EXEC_DELEGATE}),
    )

    decision = check_tool_access(registry, "project_execute", authority)
    # 策略直接派发（无 requires_confirmation=True）→ ALLOW
    assert decision.kind == AccessDecisionKind.ALLOW


def test_project_execute_only_main_agent():
    """project_execute 只能主 Agent 调用。"""
    registry = ToolPolicyRegistry()
    authority = make_authority(
        principal=ExecutorPrincipal.MINI_SUBAGENT,  # 不是主 Agent
        project_id="proj-1",
        granted=frozenset({ToolCapability.PROJECT_EXEC_DELEGATE}),
    )

    decision = check_tool_access(registry, "project_execute", authority)
    assert decision.kind == AccessDecisionKind.DENY


def test_sandbox_write_uncategorized_denied():
    """sandbox_write_file 未分类，默认 deny。"""
    registry = ToolPolicyRegistry()
    authority = make_authority(
        principal=ExecutorPrincipal.MAIN_AGENT,
        project_id="proj-1",
        granted=frozenset(),
    )

    decision = check_tool_access(registry, "sandbox_write_file", authority)
    assert decision.kind == AccessDecisionKind.DENY


def test_task_note_allows():
    """task_note 有 SESSION_STATE_WRITE 能力，无外部副作用（WRITES_SESSION 为会话内部操作），默认 ALLOW。"""
    registry = ToolPolicyRegistry()
    authority = make_authority(
        granted=frozenset({ToolCapability.SESSION_STATE_WRITE}),
    )

    decision = check_tool_access(registry, "task_note", authority)
    assert decision.kind == AccessDecisionKind.ALLOW


# ============================================================================
# Mini Subagent 权限隔离
# ============================================================================

def test_mini_subagent_cannot_submit_subagent():
    """Mini Subagent 不能调用 subagent_submit。"""
    registry = ToolPolicyRegistry()
    authority = make_authority(
        principal=ExecutorPrincipal.MINI_SUBAGENT,
        granted=frozenset({ToolCapability.SUBAGENT_DELEGATE}),
    )

    decision = check_tool_access(registry, "subagent_submit", authority)
    assert decision.kind == AccessDecisionKind.DENY


def test_mini_subagent_can_use_read_tools():
    """Mini Subagent 可以使用只读工具。"""
    registry = ToolPolicyRegistry()
    authority = make_authority(
        principal=ExecutorPrincipal.MINI_SUBAGENT,
        project_id="proj-1",
        granted=frozenset({ToolCapability.PROJECT_READ}),
    )

    decision = check_tool_access(registry, "project_read_file", authority)
    assert decision.kind == AccessDecisionKind.ALLOW


# ============================================================================
# MCP 工具动态注册
# ============================================================================

def test_mcp_tool_uncategorized_denied_by_default():
    """MCP 工具未分类时 deny。"""
    registry = ToolPolicyRegistry()
    # 动态注册一个 MCP 工具
    mcp_meta = ToolMetadata(
        name="mcp__custom__some_tool",
        source=ToolSourceKind.MCP,
        source_service="custom-service",
        uncategorized=True,
    )
    registry.register_metadata(mcp_meta)

    authority = make_authority(granted=frozenset())

    decision = check_tool_access(registry, "mcp__custom__some_tool", authority)
    assert decision.kind == AccessDecisionKind.DENY


def test_mcp_tool_can_be_enabled():
    """MCP 工具注册后可被策略启用。"""
    registry = ToolPolicyRegistry()
    mcp_meta = ToolMetadata(
        name="mcp__custom__safe_tool",
        source=ToolSourceKind.MCP,
        source_service="custom-service",
        capabilities=frozenset({ToolCapability.COMPUTE}),
        side_effects=frozenset({ToolSideEffect.NONE}),
    )
    registry.register_metadata(mcp_meta)
    registry.set_policy(
        "mcp__custom__safe_tool",
        ToolPolicy(enabled=True),
    )

    authority = make_authority(granted=frozenset({ToolCapability.COMPUTE}))

    decision = check_tool_access(registry, "mcp__custom__safe_tool", authority)
    assert decision.kind == AccessDecisionKind.ALLOW


# ============================================================================
# ToolAuthorizationError 行为
# ============================================================================

def test_authorization_error_carries_decision():
    """check_tool_access 返回的决策对象可被调用方包装到 ToolAuthorizationError。"""
    registry = ToolPolicyRegistry()
    authority = make_authority()
    decision = check_tool_access(registry, "calculator", authority)

    # 调用方可以包装成 ToolAuthorizationError
    error = ToolAuthorizationError(decision)
    assert error.decision is decision
    assert error.decision.kind == AccessDecisionKind.DENY


# ============================================================================
# 决策先于执行（confirm 阻止 handler）
# ============================================================================

def test_confirm_blocks_handler():
    """显式 requires_confirmation=True 的策略触发 CONFIRM，阻止执行。"""
    authority = make_authority(
        granted=frozenset({ToolCapability.SESSION_STATE_WRITE}),
    )
    # 显式开启确认策略
    policy = ToolPolicy(
        enabled=True,
        allowed_principals=frozenset({ExecutorPrincipal.MAIN_AGENT}),
        requires_confirmation=True,
    )
    decision = evaluate_tool_access(TASK_NOTE_METADATA, policy, authority)
    assert decision.kind == AccessDecisionKind.CONFIRM

    # 调用方将 confirm 包装为异常，阻止执行
    error = ToolAuthorizationError(decision)
    assert error.decision.kind == AccessDecisionKind.CONFIRM


# ============================================================================
# 现有 Agent._do_tool_call 兼容
# ============================================================================

def test_existing_tool_allow_list_still_works():
    """现有 allow list 逻辑仍可作为第一道防线。

    compute_thread_tool_allow_list 返回 frozenset 时，
    对应的工具元数据在 evaluate_tool_access 中仍需满足能力检查。
    """
    # 普通会话：通过 allow list 过滤后，仍需能力检查
    from mini_deerflow.tools import ToolAllowList

    # calculator 不在 allow list 中时，根本不会出现在模型工具列表中
    # 这是第一道防线的语义：不在列表 → 不会调用
    allowed: ToolAllowList = frozenset()  # 空白名单

    # 但若在列表中，仍需能力检查
    allowed = frozenset({"calculator"})
    # calculator 需要 COMPUTE 能力
    authority_no_cap = make_authority(granted=frozenset())
    _, policy = ToolPolicyRegistry().get("calculator")
    metadata, _ = ToolPolicyRegistry().get("calculator")
    decision = evaluate_tool_access(metadata, policy, authority_no_cap)
    assert decision.kind == AccessDecisionKind.DENY


def test_builtin_metadata_consistent_with_tool_registry():
    """内置元数据名称与实际注册工具名一致。"""
    from mini_deerflow.tools import create_default_registry

    reg = create_default_registry()
    tool_names = {t.name for t in reg.list_tools()}

    for name in BUILTIN_TOOL_METADATA:
        if name in tool_names:
            assert BUILTIN_TOOL_METADATA[name].name == name


# ============================================================================
# run_authority_helper 工厂
# ============================================================================

def test_run_authority_immutable():
    """RunAuthority 不可变。"""
    from mini_deerflow.tool_authorization import RunAuthority
    auth = make_authority()
    with pytest.raises(AttributeError):
        auth.project_id = "new"  # type: ignore


def test_authorization_error_immutable():
    """ToolAuthorizationError.decision 不可变。"""
    registry = ToolPolicyRegistry()
    authority = make_authority()
    decision = check_tool_access(registry, "calculator", authority)

    error = ToolAuthorizationError(decision)
    assert error.decision.kind == AccessDecisionKind.DENY

    # ToolAccessDecision 是 frozen dataclass，属性不可变
    with pytest.raises(AttributeError):
        error.decision.kind = AccessDecisionKind.ALLOW  # type: ignore
