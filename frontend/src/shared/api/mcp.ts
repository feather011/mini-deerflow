/**
 * MCP 管理 API 客户端（TOOL-05）
 *
 * 端点：
 * - GET  /api/mcp/servers
 * - GET  /api/mcp/tools
 * - POST /api/mcp/tools/{name}/enable {enabled: bool}
 * - POST /api/mcp/tools/{name}/grant   {principal: str}
 * - POST /api/mcp/tools/{name}/revoke  {principal: str}
 *
 * 主体枚举（与后端 tool_contracts.ExecutorPrincipal 一致）：
 *   MAIN_AGENT / MINI_SUBAGENT / HARNESS_AGENT
 */
import { apiGet, apiPost } from './client'

export type McpPrincipal = 'MAIN_AGENT' | 'MINI_SUBAGENT' | 'HARNESS_AGENT'

export const MCP_PRINCIPALS: readonly McpPrincipal[] = [
  'MAIN_AGENT',
  'MINI_SUBAGENT',
  'HARNESS_AGENT',
]

export interface ServerInfo {
  name: string
  status: string
  running: boolean
  tool_count: number
  failure_kind: string | null
  failure_message: string | null
}

export interface ServerListResponse {
  servers: ServerInfo[]
}

export interface ToolAuthInfo {
  qualified_name: string
  server_name: string
  enabled: boolean
  allowed_principals: string[]
}

export interface ToolListResponse {
  tools: ToolAuthInfo[]
}

export async function listMcpServers(): Promise<ServerListResponse> {
  return apiGet<ServerListResponse>('/api/mcp/servers')
}

export async function listMcpTools(): Promise<ToolListResponse> {
  return apiGet<ToolListResponse>('/api/mcp/tools')
}

export async function setMcpToolEnabled(
  qualified_name: string,
  enabled: boolean,
): Promise<ToolAuthInfo> {
  return apiPost<ToolAuthInfo>(
    `/api/mcp/tools/${encodeURIComponent(qualified_name)}/enable`,
    { body: { enabled } },
  )
}

export async function grantMcpTool(
  qualified_name: string,
  principal: McpPrincipal,
): Promise<ToolAuthInfo> {
  return apiPost<ToolAuthInfo>(
    `/api/mcp/tools/${encodeURIComponent(qualified_name)}/grant`,
    { body: { principal } },
  )
}

export async function revokeMcpTool(
  qualified_name: string,
  principal: McpPrincipal,
): Promise<ToolAuthInfo> {
  return apiPost<ToolAuthInfo>(
    `/api/mcp/tools/${encodeURIComponent(qualified_name)}/revoke`,
    { body: { principal } },
  )
}
