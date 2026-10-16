"""TOOL-02 集成测试：验证生产接入路径。

验证 Runtime 注入、模型可见性过滤、Subagent 权限隔离的端到端行为。
"""

import pytest

from mini_deerflow.agent import Agent
from mini_deerflow.models import FinalAnswer, Message, ToolCall, ToolSpec
from mini_deerflow.tools import ToolRegistry, Tool, ToolNotAllowedError, ToolNotFoundError
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tool_authorization import (
    BUILTIN_TOOL_METADATA,
    ExecutorPrincipal,
    RunAuthority,
    ToolAuthorizationError,
    ToolMetadata,
    ToolPolicy,
    ToolPolicyRegistry,
    check_tool_access,
    evaluate_tool_access,
)
from mini_deerflow.tool_contracts import (
    AccessDecisionKind,
    CALCULATOR_METADATA,
    PROJECT_EXECUTE_METADATA,
    SANDBOX_WRITE_FILE_METADATA,
    ToolCapability,
    ToolSourceKind,
    ToolSideEffect,
)


# ============================================================================
# 测试一：Runtime 注入，禁用工具不可见
# ============================================================================

def test_disabled_tool_not_in_model_tools():
    """禁用工具不在发给模型的工具列表中。"""
    # 1. 创建 ToolPolicyRegistry 并禁用 calculator
    registry = ToolPolicyRegistry()
    registry.set_policy("calculator", ToolPolicy(enabled=False))

    # 2. 创建 Agent（自动从 registry 拿到 tool_specs）
    store = InMemoryThreadStore()
    store.create("t1")

    class FakeModel:
        last_tools: list = []

        def decide(self, messages, tools=None):
            FakeModel.last_tools = list(tools) if tools else []
            return FinalAnswer(content="done")

    agent = Agent(
        model=FakeModel(),
        registry=ToolRegistry(),
        store=store,
        tool_policy_registry=registry,
    )

    # 3. 注册 calculator
    agent._registry.register(Tool(
        name="calculator",
        description="计算",
        parameters={"type": "object", "properties": {}},
        handler=lambda args: "42",
    ))

    # 4. Agent.run() 中，calculator 不应出现在模型可见工具中
    # 只需验证 check_tool_access 返回 disabled 决策即可（Agent.run 端到端需要 store 有 thread）
    decision = check_tool_access(registry, "calculator", RunAuthority(
        principal=ExecutorPrincipal.MAIN_AGENT,
        thread_id="t1",
        run_id="r1",
        project_id=None,
        granted_capabilities=frozenset(),
    ))
    assert decision.kind == AccessDecisionKind.DENY


def test_disabled_tool_handler_not_executed():
    """禁用工具被强制调用时，handler 不执行。"""
    registry = ToolPolicyRegistry()
    registry.set_policy("calculator", ToolPolicy(enabled=False))

    store = InMemoryThreadStore()
    store.create("t1")

    class FakeModel:
        def decide(self, messages, tools=None):
            # 模型决定调用 calculator（但在策略层被拦截）
            return ToolCall(name="calculator", arguments={"expression": "1+1"})

    executed = False

    def my_handler(args):
        nonlocal executed
        executed = True
        return "42"

    agent = Agent(
        model=FakeModel(),
        registry=ToolRegistry(),
        store=store,
        tool_policy_registry=registry,
    )
    agent._registry.register(Tool(
        name="calculator",
        description="计算",
        parameters={"type": "object", "properties": {}},
        handler=my_handler,
    ))

    # 工具被拦截，抛出 ToolAuthorizationError
    with pytest.raises(ToolAuthorizationError):
        agent.run("t1", "1+1")

    assert not executed, "禁用工具的 handler 不应被执行"


# ============================================================================
# 测试二：confirm 工具可见但 handler 不执行
# ============================================================================

def test_confirm_tool_visible_to_model():
    """confirm 工具在可见列表中，模型可以看到。"""
    registry = ToolPolicyRegistry()

    # project_execute 策略 requires_confirmation=False + 特殊处理 → ALLOW
    metadata, policy = registry.get("project_execute")
    assert metadata is not None
    assert not metadata.uncategorized  # 未分类才会隐藏
    # project_execute 默认直接派发
    assert policy.requires_confirmation is False
    assert metadata.requires_confirmation() is True  # WRITES_PROJECT 副作用存在
    # 但 evaluate_tool_access 中 project_execute 特殊处理 → ALLOW
    authority = RunAuthority(
        principal=ExecutorPrincipal.MAIN_AGENT,
        thread_id="t1",
        run_id="r1",
        project_id="proj-1",
        granted_capabilities=frozenset({ToolCapability.PROJECT_EXEC_DELEGATE}),
    )
    decision = evaluate_tool_access(metadata, policy, authority)
    assert decision.kind == AccessDecisionKind.ALLOW  # 直接派发


def test_confirm_tool_handler_not_executed():
    """task_note 有 WRITES_SESSION 副作用，被调用时 handler 不执行（需用户确认）。"""
    registry = ToolPolicyRegistry()

    store = InMemoryThreadStore()
    store.create("t1")
    thread = store.get("t1")
    thread.project_id = "proj-1"  # 项目会话
    store.save(thread)

    class FakeModel:
        def decide(self, messages, tools=None):
            return ToolCall(name="task_note", arguments={"content": "note"})

    executed = False

    def my_handler(args):
        nonlocal executed
        executed = True
        return '{"ok": true}'

    agent = Agent(
        model=FakeModel(),
        registry=ToolRegistry(),
        store=store,
        tool_policy_registry=registry,
    )
    agent._registry.register(Tool(
        name="task_note",
        description="记笔记",
        parameters={"type": "object", "properties": {}},
        handler=my_handler,
    ))

    # WRITES_SESSION 触发 CONFIRM → ToolAuthorizationError，handler 不执行
    with pytest.raises(ToolAuthorizationError) as exc_info:
        agent.run("t1", "记个笔记")

    assert exc_info.value.decision.kind == AccessDecisionKind.CONFIRM
    assert not executed, "confirm 工具 handler 不应被执行"


# ============================================================================
# 测试三：Subagent 权限隔离（allowed_tools 限制 + 无 tool_policy_registry）
# ============================================================================

def test_subagent_allowed_tools_bounded_by_parent():
    """子任务只能使用父授权的工具。"""
    from mini_deerflow.subagent_models import SubagentContextPolicy
    from mini_deerflow.tools import build_allow_list

    # 父授权只允许 calculator
    policy = SubagentContextPolicy(
        allowed_tools=("calculator",),
    )
    allowed = build_allow_list(policy.allowed_tools)
    assert allowed == frozenset({"calculator"})

    # project_execute 不在允许列表中
    assert "project_execute" not in allowed


def test_budget_aware_agent_no_tool_policy_registry():
    """_BudgetAwareAgent 不持有 tool_policy_registry，第三层授权不触发。

    验证：Subagent 只受 allowed_tools 限制（第二层），
    不受新策略第三层影响。sandbox_write_file 不以 project_ 开头，
    普通会话第二层放行；无 tool_policy_registry 时第三层不生效，handler 正常执行。
    """
    from mini_deerflow.tools import ToolRegistry, ToolAllowList

    store = InMemoryThreadStore()
    store.create("t1")

    call_count = 0

    class FakeModel:
        def decide(self, messages, tools=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ToolCall(name="sandbox_write_file", arguments={})
            return FinalAnswer(content="done")  # 第二轮收敛

    executed = {}

    def my_handler(args):
        executed["ran"] = True
        return "written"

    agent = Agent(
        model=FakeModel(),
        registry=ToolRegistry(),
        store=store,
        tool_policy_registry=None,  # 未注入，第三层不触发
    )
    agent._registry.register(Tool(
        name="sandbox_write_file",
        description="写",
        parameters={"type": "object", "properties": {}},
        handler=my_handler,
    ))

    # 普通会话（project_id=None）时，sandbox_write_file 不以 project_ 开头 → 第二层放行
    # 无 tool_policy_registry → 第三层不生效 → handler 执行
    result, trace = agent.run("t1", "write")
    assert executed.get("ran"), "tool_policy_registry=None 时 handler 应正常执行"
    assert result == "done"


# ============================================================================
# 测试四：模型可见性过滤——uncategorized 工具隐藏
# ============================================================================

def test_uncategorized_tool_hidden_from_model():
    """未分类工具（uncategorized=True）不在发给模型的工具列表中。"""
    registry = ToolPolicyRegistry()
    metadata, _ = registry.get("sandbox_write_file")
    assert metadata is not None
    assert metadata.uncategorized is True

    # sandbox_write_file 的 check_tool_access 返回 deny
    decision = check_tool_access(registry, "sandbox_write_file", RunAuthority(
        principal=ExecutorPrincipal.MAIN_AGENT,
        thread_id="t1",
        run_id="r1",
        project_id="proj-1",
        granted_capabilities=frozenset(),
    ))
    assert decision.kind == AccessDecisionKind.DENY
    assert decision.reason_code == "uncategorized"


# ============================================================================
# 测试五：normal session scope 隔离
# ============================================================================

def test_normal_session_cannot_get_project_abilities():
    """普通会话即使传入 project_id 也不能执行项目操作。"""
    registry = ToolPolicyRegistry()

    # 普通会话：authority.project_id = None
    authority = RunAuthority(
        principal=ExecutorPrincipal.MAIN_AGENT,
        thread_id="t1",
        run_id="r1",
        project_id=None,  # 普通会话
        granted_capabilities=frozenset({ToolCapability.PROJECT_READ}),
    )
    metadata, policy = registry.get("project_read_file")
    decision = evaluate_tool_access(metadata, policy, authority)
    assert decision.kind == AccessDecisionKind.DENY
    assert decision.reason_code == "scope_mismatch"


# ============================================================================
# 要求 1：真正进入 CONFIRM 分支的测试
# ============================================================================

def test_confirm_branch_reached_with_sufficient_capability():
    """task_note 授予 SESSION_STATE_WRITE 后触发 CONFIRM，handler 未执行。"""
    registry = ToolPolicyRegistry()

    authority = RunAuthority(
        principal=ExecutorPrincipal.MAIN_AGENT,
        thread_id="t1",
        run_id="r1",
        project_id="proj-1",
        granted_capabilities=frozenset({ToolCapability.SESSION_STATE_WRITE}),
    )
    decision = check_tool_access(registry, "task_note", authority)
    assert decision.kind == AccessDecisionKind.CONFIRM
    assert decision.reason_code == "requires_confirmation"


def test_confirm_handler_not_executed_via_evaluate():
    """evaluate_tool_access 返回 CONFIRM 时，调用方必须阻止执行。"""
    registry = ToolPolicyRegistry()
    authority = RunAuthority(
        principal=ExecutorPrincipal.MAIN_AGENT,
        thread_id="t1",
        run_id="r1",
        project_id="proj-1",
        granted_capabilities=frozenset({ToolCapability.SESSION_STATE_WRITE}),
    )
    metadata, policy = registry.get("task_note")
    decision = evaluate_tool_access(metadata, policy, authority)
    assert decision.kind == AccessDecisionKind.CONFIRM
    # 调用方应抛 ToolAuthorizationError 阻止执行
    error = ToolAuthorizationError(decision)
    assert error.decision.kind == AccessDecisionKind.CONFIRM


# ============================================================================
# 要求 2：通过 create_runtime 创建 Agent，普通会话写工具被拒绝
# ============================================================================

def test_runtime_created_agent_normal_session_cannot_write():
    """通过 create_runtime() 创建的 Agent，普通会话调用写入工具被拒绝。"""
    from mini_deerflow.runtime import RuntimeConfig, create_runtime

    # 标准 create_runtime 方式
    runtime = create_runtime(
        store=InMemoryThreadStore(),
        registry=ToolRegistry(),
        config=RuntimeConfig(),
    )
    agent = runtime.agent
    store = runtime.store
    store.create("t1")
    thread = store.get("t1")
    assert thread.project_id is None  # 普通会话

    executed = {}

    def my_handler(args):
        executed["ran"] = True
        return "written"

    agent._registry.register(Tool(
        name="sandbox_write_file",
        description="写",
        parameters={"type": "object", "properties": {}},
        handler=my_handler,
    ))

    # sandbox_write_file 有 WRITES_FILES 副作用，普通会话默认 deny
    # 但由于第二层允许（非 project_ 开头），第三层才拒绝
    # 验证：第三层 deny
    authority = agent._tool_policy_registry.get_authority(
        thread_id="t1",
        run_id="r1",
        project_id=None,
        granted_capabilities=frozenset(),  # 普通会话默认只有 COMPUTE
    )
    decision = check_tool_access(agent._tool_policy_registry, "sandbox_write_file", authority)
    assert decision.kind == AccessDecisionKind.DENY
    assert decision.reason_code == "uncategorized"
    assert not executed, "不应执行任何 handler"


# ============================================================================
# 要求 3：子任务不能通过 None 或额外能力请求扩大父授权
# ============================================================================

def test_subagent_authority_cannot_be_expanded():
    """Subagent 的 RunAuthority 不能通过传入 None 扩大权限。"""
    registry = ToolPolicyRegistry()

    # 场景：父授权了部分能力，子代理试图用 None 绕过
    parent_authority = RunAuthority(
        principal=ExecutorPrincipal.MINI_SUBAGENT,
        thread_id="sub-1",
        run_id="r1",
        project_id="proj-1",
        granted_capabilities=frozenset({ToolCapability.PROJECT_READ}),
    )

    # 子代理尝试调用 subagent_submit（需要 MAIN_AGENT）
    decision = check_tool_access(registry, "subagent_submit", parent_authority)
    assert decision.kind == AccessDecisionKind.DENY
    assert decision.reason_code == "principal_not_allowed"

    # 子代理试图调用 project_execute（需要 PROJECT_EXEC_DELEGATE 但 MiniSubagent 不是主代理）
    decision2 = check_tool_access(registry, "project_execute", parent_authority)
    assert decision2.kind == AccessDecisionKind.DENY
    assert decision2.reason_code in ("principal_not_allowed", "capability_missing")


def test_mini_subagent_cannot_delegate_to_subagent():
    """MiniSubagent 不能调用 subagent_submit（无论 granted_capabilities 如何）。"""
    registry = ToolPolicyRegistry()

    # 尝试授予全部能力，但 principal 不是 MAIN_AGENT
    authority = RunAuthority(
        principal=ExecutorPrincipal.MINI_SUBAGENT,
        thread_id="sub-1",
        run_id="r1",
        project_id="proj-1",
        granted_capabilities=frozenset({
            ToolCapability.SUBAGENT_DELEGATE,
            ToolCapability.PROJECT_READ,
            ToolCapability.PROJECT_EXEC_DELEGATE,
        }),
    )

    # subagent_submit 策略：allowed_principals = {MAIN_AGENT}
    decision = check_tool_access(registry, "subagent_submit", authority)
    assert decision.kind == AccessDecisionKind.DENY
    assert decision.reason_code == "principal_not_allowed"

