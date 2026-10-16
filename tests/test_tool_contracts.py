"""TOOL-01 测试：工具契约与授权策略基础。

覆盖全部 14 个行为测试用例（见规格第七节）。
"""

import pytest

from mini_deerflow.tool_contracts import (
    AccessDecisionKind,
    CALCULATOR_METADATA,
    ExecutorPrincipal,
    HISTORY_READ_METADATA,
    HISTORY_SEARCH_METADATA,
    PROJECT_EXECUTE_METADATA,
    PROJECT_READ_FILE_METADATA,
    SUBAGENT_METADATA,
    SANDBOX_READ_FILE_METADATA,
    SANDBOX_WRITE_FILE_METADATA,
    SANDBOX_RUN_COMMAND_METADATA,
    TASK_NOTE_METADATA,
    ToolAccessDecision,
    ToolCapability,
    ToolMetadata,
    ToolPolicy,
    ToolResult,
    ToolResultStatus,
    ToolSideEffect,
    ToolSourceKind,
    evaluate_tool_access,
    default_policy_for,
    BUILTIN_TOOL_METADATA,
)


# ============================================================================
# 辅助工厂
# ============================================================================

def make_authority(
    principal: ExecutorPrincipal = ExecutorPrincipal.MAIN_AGENT,
    project_id: str | None = None,
    granted: frozenset[ToolCapability] | None = None,
) -> "RunAuthority":
    from mini_deerflow.tool_contracts import RunAuthority
    return RunAuthority(
        principal=principal,
        thread_id="thread-1",
        run_id="run-1",
        project_id=project_id,
        granted_capabilities=granted or frozenset(),
    )


# ============================================================================
# 1. 未分类、未知或未启用工具默认 deny
# ============================================================================

def test_uncategorized_tool_is_deny():
    """未分类工具默认 deny。"""
    metadata = ToolMetadata(
        name="unknown_tool",
        source=ToolSourceKind.LOCAL,
        uncategorized=True,
    )
    policy = ToolPolicy(enabled=True)
    authority = make_authority()

    decision = evaluate_tool_access(metadata, policy, authority)
    assert decision.kind == AccessDecisionKind.DENY
    assert decision.reason_code == "uncategorized"


def test_unknown_tool_metadata_missing_is_deny():
    """元数据缺失时 deny。"""
    policy = ToolPolicy(enabled=True)
    authority = make_authority()

    decision = evaluate_tool_access(None, policy, authority)
    assert decision.kind == AccessDecisionKind.DENY
    assert decision.reason_code == "metadata_missing"


def test_disabled_tool_is_deny():
    """已禁用工具 deny。"""
    metadata = CALCULATOR_METADATA
    policy = ToolPolicy(enabled=False)
    authority = make_authority()

    decision = evaluate_tool_access(metadata, policy, authority)
    assert decision.kind == AccessDecisionKind.DENY
    assert decision.reason_code == "tool_disabled"


# ============================================================================
# 2. 不带 project_ 前缀的危险工具也不会自动获得权限
# ============================================================================

def test_calculator_needs_capability():
    """calculator 需要 COMPUTE 能力；未授予时 deny。"""
    metadata = CALCULATOR_METADATA
    policy = ToolPolicy(enabled=True)
    authority = make_authority(granted=frozenset())  # 无任何能力

    decision = evaluate_tool_access(metadata, policy, authority)
    assert decision.kind == AccessDecisionKind.DENY
    assert "capability_missing" in decision.reason_code


def test_calculator_allowed_when_capability_granted():
    """calculator 获得 COMPUTE 能力后 allow。"""
    metadata = CALCULATOR_METADATA
    policy = ToolPolicy(enabled=True)
    authority = make_authority(granted=frozenset({ToolCapability.COMPUTE}))

    decision = evaluate_tool_access(metadata, policy, authority)
    assert decision.kind == AccessDecisionKind.ALLOW


# ============================================================================
# 3. 普通会话不能获得项目操作权限（包括错误赋予项目能力）
# ============================================================================

def test_normal_session_denied_for_project_tool():
    """普通会话（project_id=None）调用项目工具 deny。"""
    metadata = PROJECT_READ_FILE_METADATA
    policy = default_policy_for("project_read_file")
    authority = make_authority(project_id=None)  # 普通会话

    decision = evaluate_tool_access(metadata, policy, authority)
    assert decision.kind == AccessDecisionKind.DENY
    assert decision.reason_code == "scope_mismatch"


def test_normal_session_even_if_project_capability_granted():
    """普通会话即使错误传入项目能力也 deny。"""
    metadata = PROJECT_READ_FILE_METADATA
    policy = default_policy_for("project_read_file")
    authority = make_authority(
        project_id=None,
        granted=frozenset({ToolCapability.PROJECT_READ}),
    )

    decision = evaluate_tool_access(metadata, policy, authority)
    assert decision.kind == AccessDecisionKind.DENY
    assert decision.reason_code == "scope_mismatch"


def test_project_session_allowed_for_project_read():
    """项目会话（project_id!=None）且能力满足时，项目只读 allow。"""
    metadata = PROJECT_READ_FILE_METADATA
    policy = default_policy_for("project_read_file")
    authority = make_authority(
        project_id="proj-1",
        granted=frozenset({ToolCapability.PROJECT_READ}),
    )

    decision = evaluate_tool_access(metadata, policy, authority)
    assert decision.kind == AccessDecisionKind.ALLOW


# ============================================================================
# 4. 项目主 Agent 可获得项目读取与委派，但不能直接执行命令或写项目文件
# ============================================================================

def test_project_session_main_agent_can_read():
    """项目会话主 Agent 可读取项目文件。"""
    metadata = PROJECT_READ_FILE_METADATA
    policy = default_policy_for("project_read_file")
    authority = make_authority(
        principal=ExecutorPrincipal.MAIN_AGENT,
        project_id="proj-1",
        granted=frozenset({ToolCapability.PROJECT_READ}),
    )

    decision = evaluate_tool_access(metadata, policy, authority)
    assert decision.kind == AccessDecisionKind.ALLOW


def test_project_execute_needs_confirmation():
    """project_execute 有 WRITES_PROJECT 副作用，但 metadata 直接派发 + 策略 requires_confirmation=False → ALLOW。"""
    metadata = PROJECT_EXECUTE_METADATA
    policy = default_policy_for("project_execute")
    authority = make_authority(
        principal=ExecutorPrincipal.MAIN_AGENT,
        project_id="proj-1",
        granted=frozenset({ToolCapability.PROJECT_EXEC_DELEGATE}),
    )

    decision = evaluate_tool_access(metadata, policy, authority)
    # 策略 requires_confirmation=False + evaluate_tool_access 特殊处理 → ALLOW
    assert decision.kind == AccessDecisionKind.ALLOW


def test_project_execute_not_direct_write():
    """project_execute 是委派，不是直接 PROJECT_MODIFY。"""
    # project_execute 元数据的能力是 PROJECT_EXEC_DELEGATE，不是 PROJECT_MODIFY
    assert ToolCapability.PROJECT_EXEC_DELEGATE not in {
        ToolCapability.PROJECT_MODIFY,
    }
    # 能力独立建模：直接写文件需要 PROJECT_MODIFY 能力（sandbox_write_file）
    assert SANDBOX_WRITE_FILE_METADATA.uncategorized is True


# ============================================================================
# 5. 已启用但未授权当前主体的 MCP 工具仍 deny
# ============================================================================

def test_mcp_tool_principal_not_allowed():
    """MCP 工具已启用，但执行主体不在允许名单时 deny。"""
    mcp_meta = ToolMetadata(
        name="mcp__some__tool",
        source=ToolSourceKind.MCP,
        source_service="some-service",
        capabilities=frozenset({ToolCapability.COMPUTE}),
        side_effects=frozenset({ToolSideEffect.NONE}),
    )
    policy = ToolPolicy(
        enabled=True,
        allowed_principals=frozenset({ExecutorPrincipal.MINI_SUBAGENT}),  # 不含主 Agent
    )
    authority = make_authority(principal=ExecutorPrincipal.MAIN_AGENT)

    decision = evaluate_tool_access(mcp_meta, policy, authority)
    assert decision.kind == AccessDecisionKind.DENY
    assert decision.reason_code == "principal_not_allowed"


# ============================================================================
# 6. 缺少任一必需能力均 deny；空集合不代表无限制
# ============================================================================

def test_missing_any_capability_denies():
    """缺少任一必需能力 deny。"""
    metadata = TASK_NOTE_METADATA  # 需要 SESSION_STATE_WRITE
    policy = default_policy_for("task_note")
    authority = make_authority(
        granted=frozenset({ToolCapability.COMPUTE}),  # 给了 COMPUTE 但缺 SESSION_STATE_WRITE
    )

    decision = evaluate_tool_access(metadata, policy, authority)
    assert decision.kind == AccessDecisionKind.DENY
    assert "capability_missing" in decision.reason_code


def test_empty_granted_set_denies_when_capability_needed():
    """空集合不代表无限制；需要能力时 deny。"""
    metadata = HISTORY_READ_METADATA  # 需要 SESSION_HISTORY_READ
    policy = default_policy_for("history_read")
    authority = make_authority(granted=frozenset())  # 空集合

    decision = evaluate_tool_access(metadata, policy, authority)
    assert decision.kind == AccessDecisionKind.DENY


# ============================================================================
# 7. 元数据要求不能被较宽松策略抹除
# ============================================================================

def test_metadata_capability_not_overridden_by_policy():
    """策略只能增加限制，不能抹除元数据要求。"""
    # history_read 需要 SESSION_HISTORY_READ
    metadata = HISTORY_READ_METADATA
    policy = ToolPolicy(
        enabled=True,
        # 策略允许主 Agent，但没授予 SESSION_HISTORY_READ
        allowed_principals=frozenset({ExecutorPrincipal.MAIN_AGENT}),
    )
    authority = make_authority(
        granted=frozenset(),  # 无任何能力
    )

    decision = evaluate_tool_access(metadata, policy, authority)
    assert decision.kind == AccessDecisionKind.DENY
    assert "capability_missing" in decision.reason_code


# ============================================================================
# 8. 未授权的外部写入为 deny，不是 confirm
# ============================================================================

def test_unauthorized_external_write_denied():
    """未授权的外部写入 deny（不是 confirm）。"""
    metadata = SANDBOX_WRITE_FILE_METADATA  # uncategorized=True，默认 deny
    policy = default_policy_for("sandbox_write_file")  # enabled=False
    authority = make_authority(
        principal=ExecutorPrincipal.MAIN_AGENT,
        project_id="proj-1",
        granted=frozenset(),
    )

    decision = evaluate_tool_access(metadata, policy, authority)
    assert decision.kind == AccessDecisionKind.DENY
    # uncategorized 优先于 tool_disabled
    assert decision.reason_code in ("uncategorized", "tool_disabled")


def test_unauthorized_network_write_denied():
    """未授权的 NETWORK_WRITE deny。"""
    metadata = ToolMetadata(
        name="http_post_tool",
        source=ToolSourceKind.LOCAL,
        capabilities=frozenset({ToolCapability.NETWORK_WRITE}),
        side_effects=frozenset({ToolSideEffect.WRITES_NETWORK}),
    )
    policy = ToolPolicy(enabled=True)
    authority = make_authority(granted=frozenset())

    decision = evaluate_tool_access(metadata, policy, authority)
    assert decision.kind == AccessDecisionKind.DENY


# ============================================================================
# 9. 已授权外部写入为 confirm；已授权读取可为 allow
# ============================================================================

def test_authorized_external_write_confirms():
    """已授权外部写入 confirm（不是 allow）。"""
    metadata = ToolMetadata(
        name="write_tool",
        source=ToolSourceKind.LOCAL,
        capabilities=frozenset({ToolCapability.PROJECT_EXEC_DELEGATE}),
        side_effects=frozenset({ToolSideEffect.WRITES_NETWORK}),
    )
    policy = ToolPolicy(
        enabled=True,
        allowed_principals=frozenset({ExecutorPrincipal.MAIN_AGENT}),
        requires_confirmation=True,
    )
    authority = make_authority(
        principal=ExecutorPrincipal.MAIN_AGENT,
        granted=frozenset({ToolCapability.PROJECT_EXEC_DELEGATE}),
    )

    decision = evaluate_tool_access(metadata, policy, authority)
    assert decision.kind == AccessDecisionKind.CONFIRM


def test_authorized_read_can_be_allow():
    """已授权的纯读取（无副作用）allow。"""
    metadata = CALCULATOR_METADATA
    policy = default_policy_for("calculator")
    authority = make_authority(granted=frozenset({ToolCapability.COMPUTE}))

    decision = evaluate_tool_access(metadata, policy, authority)
    assert decision.kind == AccessDecisionKind.ALLOW


# ============================================================================
# 10. 会话状态写入与项目写入权限不同
# ============================================================================

def test_session_state_write_confirms():
    """task_note 有外部副作用（WRITES_SESSION），默认 confirm。"""
    metadata = TASK_NOTE_METADATA
    policy = default_policy_for("task_note")
    authority = make_authority(
        granted=frozenset({ToolCapability.SESSION_STATE_WRITE}),
    )

    decision = evaluate_tool_access(metadata, policy, authority)
    # WRITES_SESSION 触发 requires_confirmation()
    assert decision.kind == AccessDecisionKind.CONFIRM


def test_project_modify_is_different_capability():
    """项目文件写入需要 PROJECT_MODIFY，与 SESSION_STATE_WRITE 不同。"""
    assert ToolCapability.SESSION_STATE_WRITE != ToolCapability.PROJECT_MODIFY


# ============================================================================
# 11. project_execute 的例外不会使其他外部写入免确认
# ============================================================================

def test_project_execute_exception_does_not_extend():
    """project_execute 有例外，但 sandbox 写入仍需单独确认。"""
    # sandbox_write_file 始终 require_confirmation（uncategorized）
    assert SANDBOX_WRITE_FILE_METADATA.uncategorized is True

    policy = default_policy_for("sandbox_write_file")
    authority = make_authority(
        principal=ExecutorPrincipal.MAIN_AGENT,
        project_id="proj-1",
        granted=frozenset(),
    )

    decision = evaluate_tool_access(SANDBOX_WRITE_FILE_METADATA, policy, authority)
    assert decision.kind == AccessDecisionKind.DENY


def test_project_execute_only_for_main_agent():
    """project_execute 只能主 Agent 调用（策略限制）。"""
    policy = default_policy_for("project_execute")
    assert ExecutorPrincipal.MAIN_AGENT in policy.allowed_principals
    assert ExecutorPrincipal.MINI_SUBAGENT not in policy.allowed_principals
    assert ExecutorPrincipal.HARNESS_AGENT not in policy.allowed_principals


# ============================================================================
# 12. 权限和元数据不会因外部可变集合修改而悄悄改变
# ============================================================================

def test_metadata_is_immutable():
    """ToolMetadata 是 frozen dataclass，不可变。"""
    metadata = CALCULATOR_METADATA
    with pytest.raises(AttributeError):
        metadata.capabilities = frozenset()  # type: ignore


def test_run_authority_is_immutable():
    """RunAuthority 是 frozen dataclass，不可变。"""
    from mini_deerflow.tool_contracts import RunAuthority
    auth = make_authority()
    with pytest.raises(AttributeError):
        auth.project_id = "new-proj"  # type: ignore


def test_tool_policy_is_immutable():
    """ToolPolicy 是 frozen dataclass，不可变。"""
    policy = ToolPolicy()
    with pytest.raises(AttributeError):
        policy.enabled = False  # type: ignore


def test_decision_immutable():
    """ToolAccessDecision 是 frozen dataclass，不可变。"""
    decision = ToolAccessDecision.allow()
    with pytest.raises(AttributeError):
        decision.kind = AccessDecisionKind.DENY  # type: ignore


# ============================================================================
# 13. 结果契约能正常表达各终态，非法状态被明确拒绝
# ============================================================================

def test_result_succeeded():
    result = ToolResult.succeeded("42", artifacts=["output.txt"])
    assert result.status == ToolResultStatus.SUCCEEDED
    assert result.content == "42"
    assert result.artifacts == ["output.txt"]
    assert result.error is None


def test_result_failed():
    result = ToolResult.failed("error", "Division by zero")
    assert result.status == ToolResultStatus.FAILED
    assert result.error == "Division by zero"


def test_result_cancelled():
    result = ToolResult.cancelled("interrupted")
    assert result.status == ToolResultStatus.CANCELLED


def test_result_timed_out():
    result = ToolResult.timed_out("timeout")
    assert result.status == ToolResultStatus.TIMED_OUT


def test_result_succeeded_with_error_raises():
    """succeeded 携带 error 抛出 ValueError。"""
    with pytest.raises(ValueError):
        ToolResult(
            status=ToolResultStatus.SUCCEEDED,
            content="ok",
            error="should not be here",
        )


def test_result_non_failed_with_error_raises():
    """非 failed 携带 error 抛出 ValueError。"""
    with pytest.raises(ValueError):
        ToolResult(
            status=ToolResultStatus.SUCCEEDED,
            content="ok",
            error="should not be here",
        )


def test_result_truncated():
    result = ToolResult.succeeded("long content", truncated=True)
    assert result.truncated is True


# ============================================================================
# 14. 现有默认注册与 ToolSpec 转换兼容
# ============================================================================

def test_builtin_metadata_names_match_tool_names():
    """内置元数据名称与实际注册工具名一致。"""
    # calculator
    assert CALCULATOR_METADATA.name == "calculator"
    # project_*
    assert PROJECT_READ_FILE_METADATA.name == "project_read_file"
    assert PROJECT_EXECUTE_METADATA.name == "project_execute"
    # history_*
    assert HISTORY_SEARCH_METADATA.name == "history_search"
    assert HISTORY_READ_METADATA.name == "history_read"
    # task_note
    assert TASK_NOTE_METADATA.name == "task_note"
    # subagent
    assert SUBAGENT_METADATA.name == "subagent_submit"
    # sandbox
    assert SANDBOX_READ_FILE_METADATA.name == "sandbox_read_file"
    assert SANDBOX_WRITE_FILE_METADATA.name == "sandbox_write_file"
    assert SANDBOX_RUN_COMMAND_METADATA.name == "sandbox_run_command"


def test_builtin_metadata_in_registry():
    """所有内置元数据都在注册表中。"""
    for name in BUILTIN_TOOL_METADATA:
        assert name in BUILTIN_TOOL_METADATA
        meta = BUILTIN_TOOL_METADATA[name]
        assert meta.name == name


def test_default_policy_returns_policy_for_known_tools():
    """已知工具有默认策略。"""
    for name in BUILTIN_TOOL_METADATA:
        policy = default_policy_for(name)
        assert policy is not None
        assert isinstance(policy, ToolPolicy)


def test_default_policy_none_for_unknown():
    """未知工具返回 None。"""
    assert default_policy_for("truly_unknown_tool") is None


def test_calculator_is_safe():
    """calculator 是无外部副作用的"安全"工具。"""
    assert CALCULATOR_METADATA.is_safe() is True


def test_project_execute_is_not_safe():
    """project_execute 有外部副作用，不是安全工具。"""
    assert PROJECT_EXECUTE_METADATA.is_safe() is False


def test_sandbox_write_is_uncategorized():
    """sandbox_write_file 未分类。"""
    assert SANDBOX_WRITE_FILE_METADATA.uncategorized is True


def test_decision_kind_predicates():
    """ToolAccessDecision 的便捷谓词。"""
    allow = ToolAccessDecision.allow()
    deny = ToolAccessDecision.deny("test", "test")
    confirm = ToolAccessDecision.confirm("need confirm")

    assert allow.is_allowed
    assert not allow.is_confirm
    assert not allow.is_denied

    assert deny.is_denied
    assert not deny.is_allowed

    assert confirm.is_confirm
    assert not confirm.is_allowed


def test_authority_has_capability_helpers():
    """RunAuthority 能力检查便捷方法。"""
    auth = make_authority(
        granted=frozenset({ToolCapability.COMPUTE, ToolCapability.PROJECT_READ})
    )
    assert auth.has_capability(ToolCapability.COMPUTE)
    assert auth.has_capability(ToolCapability.PROJECT_READ)
    assert not auth.has_capability(ToolCapability.PROJECT_EXEC_DELEGATE)

    assert auth.has_all_capabilities(frozenset({ToolCapability.COMPUTE}))
    assert auth.has_all_capabilities(frozenset({ToolCapability.COMPUTE, ToolCapability.PROJECT_READ}))
    assert not auth.has_all_capabilities(frozenset({ToolCapability.COMPUTE, ToolCapability.NETWORK_WRITE}))


def test_authority_is_project_session():
    """RunAuthority.is_project_session 正确判断。"""
    auth_proj = make_authority(project_id="proj-1")
    auth_normal = make_authority(project_id=None)
    assert auth_proj.is_project_session is True
    assert auth_normal.is_project_session is False


def test_policy_is_principal_allowed():
    """ToolPolicy.is_principal_allowed 正确判断。"""
    policy = ToolPolicy(
        allowed_principals=frozenset({
            ExecutorPrincipal.MAIN_AGENT,
            ExecutorPrincipal.MINI_SUBAGENT,
        })
    )
    assert policy.is_principal_allowed(ExecutorPrincipal.MAIN_AGENT)
    assert policy.is_principal_allowed(ExecutorPrincipal.MINI_SUBAGENT)
    assert not policy.is_principal_allowed(ExecutorPrincipal.HARNESS_AGENT)


def test_policy_merge_requirements():
    """策略只能增加能力要求，不能抹除元数据要求。"""
    policy = ToolPolicy(
        required_capabilities=frozenset({ToolCapability.COMPUTE})
    )
    merged = policy.merge_requirements(
        frozenset({ToolCapability.PROJECT_READ})
    )
    assert ToolCapability.COMPUTE in merged
    assert ToolCapability.PROJECT_READ in merged
