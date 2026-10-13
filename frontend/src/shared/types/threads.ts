/**
 * Thread API 类型定义
 *
 * 这些类型描述的是 HTTP 契约，而不是后端 Python 领域对象。
 * 前端只依赖公开 JSON 字段，不依赖 ThreadState 的内部实现。
 */
import type { ErrorDetail } from './index'

/**
 * POST /api/threads 请求体
 * thread_id 为可选字段；不指定时由服务端生成 UUID4 风格的 ID。
 * project_id 为可选字段；指定时创建项目内会话。
 */
export interface CreateThreadRequest {
  thread_id?: string
  project_id?: string
}

/**
 * 对话消息的对外视图。
 */
export interface MessageResponse {
  role: 'system' | 'user' | 'assistant' | 'tool'
  content: string
}

/**
 * 列表项：仅包含 thread_id，不暴露消息内容或内部版本。
 */
export interface ThreadSummary {
  thread_id: string
}

/**
 * 详情：thread_id + 完整消息列表。
 */
export interface ThreadDetail {
  thread_id: string
  project_id?: string | null  // 如果属于项目，则为项目 ID
  messages: MessageResponse[]
}

/**
 * GET /api/threads 的响应包装。
 */
export interface ThreadListResponse {
  threads: ThreadSummary[]
}

/**
 * 后端错误响应包装。
 */
export interface ThreadErrorResponse {
  error: ErrorDetail
}
