"""MCP 管理路由（TOOL-03）。

端点：
- GET    /api/mcp/servers                  列出服务器及运行状态
- GET    /api/mcp/tools                    列出所有 discovered 工具及授权状态
- POST   /api/mcp/tools/{name}/enable      启用工具
- POST   /api/mcp/tools/{name}/disable     停用工具
- POST   /api/mcp/tools/{name}/grant       为某主体授权
- POST   /api/mcp/tools/{name}/revoke      撤销某主体授权
- GET    /api/mcp/state-file               状态文件路径

设计原则：
- 发现 ≠ 启用 ≠ 授权
- 新发现工具默认 disabled（需要显式启用）
- 三类主体可分别授权（MAIN_AGENT / MINI_SUBAGENT / HARNESS_AGENT）
- 状态持久化，重建 runtime 后保持
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field

from mini_deerflow.api.dependencies import get_runtime
from mini_deerflow.mcp_tool_state import MCPToolStateStore
from mini_deerflow.runtime import RuntimeContext
from mini_deerflow.tool_authorization import ToolPolicy, ToolPolicyRegistry
from mini_deerflow.tool_contracts import ExecutorPrincipal

router = APIRouter(prefix="/mcp", tags=["mcp"])


def _get_state_store(runtime: RuntimeContext) -> MCPToolStateStore:
    """从 runtime 获取状态存储。如果 runtime 不持有则创建默认实例。"""
    store = getattr(runtime, "mcp_tool_state_store", None)
    if store is None:
        store = MCPToolStateStore()
        runtime.mcp_tool_state_store = store
    return store


def _get_policy_registry(runtime: RuntimeContext) -> ToolPolicyRegistry | None:
    """从 runtime 获取当前 ToolPolicyRegistry。None 表示 runtime 未持有。"""
    # RuntimeContext 没有直接持有 tool_policy_registry；Agent 持有
    agent = getattr(runtime, "agent", None) or getattr(runtime, "integrated_agent", None)
    if agent is None:
        return None
    return getattr(agent, "_tool_policy_registry", None)


def _sync_policy(
    runtime: RuntimeContext,
    qualified_name: str,
    enabled: bool,
    allowed_principals: frozenset,
) -> None:
    """TOOL-03 R1：把持久化状态同步到当前 Agent 策略注册表。

    若 runtime 未持有 Agent 策略注册表（例如重启前），不抛错。
    失败抛 HTTPException(500)。
    """
    registry = _get_policy_registry(runtime)
    if registry is None:
        return
    try:
        registry.set_policy(qualified_name, ToolPolicy(
            enabled=enabled,
            allowed_principals=allowed_principals,
        ))
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"策略同步失败: {type(e).__name__}: {e}",
        )


def _get_principal_or_400(value: str) -> ExecutorPrincipal:
    """字符串 → ExecutorPrincipal；非法 → 400。"""
    try:
        return ExecutorPrincipal[value]
    except KeyError:
        raise HTTPException(
            status_code=400,
            detail=f"未知主体: {value!r}。有效值: MAIN_AGENT / MINI_SUBAGENT / HARNESS_AGENT",
        )


class ServerInfo(BaseModel):
    """单个 MCP 服务器的运行信息。"""

    name: str
    status: str  # not_started / running / failed / stopped
    running: bool
    tool_count: int
    # TOOL-03 R5：失败原因（仅安全码/脱敏摘要）
    failure_kind: str | None = None
    failure_message: str | None = None


class ServerListResponse(BaseModel):
    """服务器列表响应。"""

    servers: list[ServerInfo]


class ToolAuthInfo(BaseModel):
    """单个 discovered 工具的授权信息。"""

    qualified_name: str
    server_name: str
    enabled: bool
    allowed_principals: list[str] = Field(default_factory=list)


class ToolListResponse(BaseModel):
    """工具授权状态列表。"""

    tools: list[ToolAuthInfo]


class EnableRequest(BaseModel):
    """启用/停用工具的请求体。"""

    enabled: bool


class GrantRequest(BaseModel):
    """授权/撤销主体请求体。"""

    principal: str


@router.get(
    "/servers",
    response_model=ServerListResponse,
    summary="列出 MCP 服务器及运行状态",
)
def list_servers(runtime: RuntimeContext = Depends(get_runtime)) -> ServerListResponse:
    """TOOL-03 R5：服务器状态来源是「配置」，启动失败/被禁用/被停止的服务器
    仍在清单中。失败原因仅安全码/脱敏摘要。"""
    mcp = getattr(runtime, "mcp_manager", None)
    if mcp is None:
        return ServerListResponse(servers=[])

    states = mcp.list_server_states()
    infos = [
        ServerInfo(
            name=st.name,
            status=st.status.value,
            running=st.status.value == "running",
            tool_count=st.tool_count,
            failure_kind=st.failure_kind,
            failure_message=st.failure_message,
        )
        for st in sorted(states, key=lambda s: s.name)
    ]
    return ServerListResponse(servers=infos)


@router.get(
    "/tools",
    response_model=ToolListResponse,
    summary="列出所有 discovered 工具的授权状态",
)
def list_tools(runtime: RuntimeContext = Depends(get_runtime)) -> ToolListResponse:
    store = _get_state_store(runtime)
    mcp = getattr(runtime, "mcp_manager", None)

    if mcp is None or not mcp._tools:
        return ToolListResponse(tools=[])

    tools: list[ToolAuthInfo] = []
    # mcp._tools: qualified_name -> (server_name, original_name)
    for qname in sorted(mcp._tools):
        server_name, _ = mcp._tools[qname]
        state = store.get(qname)
        tools.append(
            ToolAuthInfo(
                qualified_name=qname,
                server_name=server_name,
                enabled=state.enabled,
                allowed_principals=sorted(p.name for p in state.allowed_principals),
            )
        )
    return ToolListResponse(tools=tools)


@router.post(
    "/tools/{name}/enable",
    response_model=ToolAuthInfo,
    summary="启用/停用工具",
)
def set_enabled(
    name: str = Path(...),
    body: EnableRequest = ...,
    runtime: RuntimeContext = Depends(get_runtime),
) -> ToolAuthInfo:
    store = _get_state_store(runtime)
    mcp = getattr(runtime, "mcp_manager", None)
    if mcp is None or name not in mcp._tools:
        raise HTTPException(status_code=404, detail=f"未知工具: {name}")

    state = store.set_enabled(name, body.enabled)
    # TOOL-03 R1：同步到当前 Agent 策略
    _sync_policy(runtime, name, state.enabled, state.allowed_principals)
    server_name, _ = mcp._tools[name]
    return ToolAuthInfo(
        qualified_name=name,
        server_name=server_name,
        enabled=state.enabled,
        allowed_principals=sorted(p.name for p in state.allowed_principals),
    )


@router.post(
    "/tools/{name}/grant",
    response_model=ToolAuthInfo,
    summary="为主体授权",
)
def grant_principal(
    name: str = Path(...),
    body: GrantRequest = ...,
    runtime: RuntimeContext = Depends(get_runtime),
) -> ToolAuthInfo:
    store = _get_state_store(runtime)
    mcp = getattr(runtime, "mcp_manager", None)
    if mcp is None or name not in mcp._tools:
        raise HTTPException(status_code=404, detail=f"未知工具: {name}")
    principal = _get_principal_or_400(body.principal)

    state = store.grant(name, principal)
    # TOOL-03 R1：同步到当前 Agent 策略
    _sync_policy(runtime, name, state.enabled, state.allowed_principals)
    server_name, _ = mcp._tools[name]
    return ToolAuthInfo(
        qualified_name=name,
        server_name=server_name,
        enabled=state.enabled,
        allowed_principals=sorted(p.name for p in state.allowed_principals),
    )


@router.post(
    "/tools/{name}/revoke",
    response_model=ToolAuthInfo,
    summary="撤销主体授权",
)
def revoke_principal(
    name: str = Path(...),
    body: GrantRequest = ...,
    runtime: RuntimeContext = Depends(get_runtime),
) -> ToolAuthInfo:
    store = _get_state_store(runtime)
    mcp = getattr(runtime, "mcp_manager", None)
    if mcp is None or name not in mcp._tools:
        raise HTTPException(status_code=404, detail=f"未知工具: {name}")
    principal = _get_principal_or_400(body.principal)

    state = store.revoke(name, principal)
    # TOOL-03 R1：同步到当前 Agent 策略
    _sync_policy(runtime, name, state.enabled, state.allowed_principals)
    server_name, _ = mcp._tools[name]
    return ToolAuthInfo(
        qualified_name=name,
        server_name=server_name,
        enabled=state.enabled,
        allowed_principals=sorted(p.name for p in state.allowed_principals),
    )


@router.get(
    "/state-file",
    summary="状态文件路径",
)
def get_state_file(runtime: RuntimeContext = Depends(get_runtime)) -> dict:
    store = _get_state_store(runtime)
    return {"path": str(store.path)}
