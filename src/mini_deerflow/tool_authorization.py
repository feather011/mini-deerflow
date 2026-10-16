"""TOOL-02：接入真实 Agent 调用链。

将 TOOL-01 的 evaluate_tool_access() 接入 Agent 工具执行路径。
在不修改既有 ToolRegistry / Agent.run() 签名的情况下，通过依赖注入
接入 ToolPolicyRegistry，提供细粒度授权判定。

集成点：Agent._do_tool_call，在 registry.execute() 之前调用 evaluate_tool_access()。
若 ToolPolicyRegistry 不可用，回退到现有的 ensure_tool_allowed 白名单检查。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mini_deerflow.tool_contracts import (
    BUILTIN_TOOL_METADATA,
    ExecutorPrincipal,
    RunAuthority,
    ToolAccessDecision,
    ToolMetadata,
    ToolPolicy,
    ToolResultStatus,
    evaluate_tool_access,
)

if TYPE_CHECKING:
    from mini_deerflow.thread_store import ThreadStore


# ---------------------------------------------------------------------------
# ToolPolicyRegistry
# ---------------------------------------------------------------------------


class ToolPolicyRegistry:
    """工具策略注册表：持有内置元数据 + 可覆盖的策略。

    使用方式：
        registry = ToolPolicyRegistry(thread_store=store)
        # 可选：覆盖默认策略
        registry.set_policy("calculator", ToolPolicy(enabled=False))
        # 查询
        metadata, policy = registry.get("calculator")
    """

    def __init__(
        self,
        thread_store: "ThreadStore | None" = None,
    ) -> None:
        self._thread_store = thread_store
        # 内置元数据只读，策略可覆盖
        self._metadata: dict[str, ToolMetadata] = dict(BUILTIN_TOOL_METADATA)
        self._policies: dict[str, ToolPolicy] = {}

    def register_metadata(self, metadata: ToolMetadata) -> None:
        """注册/覆盖工具元数据。用于 MCP 工具动态注册。"""
        self._metadata[metadata.name] = metadata

    def set_policy(self, tool_name: str, policy: ToolPolicy) -> None:
        """设置/覆盖工具策略。用于运行时启用/禁用工具。"""
        self._policies[tool_name] = policy

    def get(
        self, tool_name: str
    ) -> tuple[ToolMetadata | None, ToolPolicy | None]:
        """返回 (metadata, policy)。未知工具均返回 None。"""
        metadata = self._metadata.get(tool_name)
        policy = self._policies.get(tool_name)
        if policy is None and metadata is not None:
            # 从内置默认策略回退
            from mini_deerflow.tool_contracts import default_policy_for
            policy = default_policy_for(tool_name)
        return metadata, policy

    def get_authority(
        self,
        thread_id: str,
        run_id: str,
        project_id: str | None,
        granted_capabilities: frozenset,
        principal: ExecutorPrincipal = ExecutorPrincipal.MAIN_AGENT,
    ) -> RunAuthority:
        """从执行上下文构造 RunAuthority。

        principal 参数允许调用方指定执行主体（MAIN_AGENT / MINI_SUBAGENT 等）。
        """
        return RunAuthority(
            principal=principal,
            thread_id=thread_id,
            run_id=run_id,
            project_id=project_id,
            granted_capabilities=granted_capabilities,
        )


# ---------------------------------------------------------------------------
# ToolAuthorizationError
# ---------------------------------------------------------------------------


class ToolAuthorizationError(Exception):
    """工具授权判定失败。"""

    def __init__(self, decision: ToolAccessDecision) -> None:
        self.decision = decision
        super().__init__(f"[授权失败] {decision.reason}")


# ---------------------------------------------------------------------------
# check_tool_access() — Agent._do_tool_call 集成点
# ---------------------------------------------------------------------------


def check_tool_access(
    registry: ToolPolicyRegistry,
    tool_name: str,
    authority: RunAuthority,
) -> ToolAccessDecision:
    """在工具执行前调用，判定是否允许执行。

    集成到 Agent._do_tool_call 时：
        decision = check_tool_access(policy_registry, tool_name, authority)
        if decision.is_denied:
            raise ToolAuthorizationError(decision)
        if decision.is_confirm:
            raise ToolAuthorizationError(decision)  # confirm 也阻止执行，需用户确认
        # allow → 继续 registry.execute()

    若 policy_registry 为 None，不做新授权检查，回退到 ensure_tool_allowed。
    """
    metadata, policy = registry.get(tool_name)
    return evaluate_tool_access(metadata, policy, authority)


# ---------------------------------------------------------------------------
# 工具能力集合推导
# ---------------------------------------------------------------------------


# 从工具名到能力集合的映射（用于从 allowed list 推导 granted_capabilities）
_TOOL_CAPABILITIES: dict[str, frozenset] = {
    "calculator": frozenset(),
    "history_search": frozenset(),
    "history_read": frozenset(),
    "task_note": frozenset(),
    "project_read_file": frozenset(),
    "project_glob": frozenset(),
    "project_grep": frozenset(),
    "project_execute": frozenset(),
    "subagent_submit": frozenset(),
}


def _capabilities_for_tool(tool_name: str) -> frozenset:
    """返回工具元数据所需的能力集合（用于推导 granted_capabilities）。"""
    if tool_name in _TOOL_CAPABILITIES:
        meta = BUILTIN_TOOL_METADATA.get(tool_name)
        if meta is not None:
            return meta.capabilities
    # 未知工具：保守拒绝，无能力
    return frozenset()
