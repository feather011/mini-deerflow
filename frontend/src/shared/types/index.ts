/**
 * 后端健康检查响应
 * GET /api/health → { status: "ok", service: string }
 */
export interface HealthResponse {
  status: 'ok'
  service: string
}

/**
 * 后端就绪检查响应
 * GET /api/ready → { status: "ready", runtime: boolean }
 */
export interface ReadyResponse {
  status: 'ready'
  runtime: boolean
}

/**
 * 后端统一错误响应
 * 所有错误响应均遵循此结构
 */
export interface ErrorDetail {
  code: string
  message: string
  request_id: string
  details: Record<string, unknown> | null
}

export interface ErrorResponse {
  error: ErrorDetail
}

// --- Run 类型（20C）---
export type { AgentRunRequest, AgentRunResponse, RunStatus } from './runs'

// --- SSE Event 类型（20D）---
export type { RunEvent, RunEventType } from './events'
export { isTerminalEvent, TERMINAL_EVENT_TYPES, TERMINAL_MESSAGES } from './events'

// --- Files / Artifacts（20E）---
export type { FileMetadata, FileListResponse, FileKind } from './files'
