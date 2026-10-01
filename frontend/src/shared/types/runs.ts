/**
 * Agent Run API 类型
 *
 * 与后端 schemas/runs.py 对齐：
 * - POST /api/threads/{thread_id}/runs
 * - 请求：{ message: string }
 * - 响应：{ run_id, thread_id, status, answer?, error? }
 */

/** Run 执行状态 */
export type RunStatus = 'succeeded' | 'failed' | 'timeout' | 'cancelled'

/** POST /api/threads/{thread_id}/runs 请求体 */
export interface AgentRunRequest {
  message: string
}

/** POST /api/threads/{thread_id}/runs 响应体 */
export interface AgentRunResponse {
  run_id: string
  thread_id: string
  status: RunStatus
  /** Run 成功时的回答（status=succeeded） */
  answer: string | null
  /** Run 失败时的错误信息（status=failed/timeout/cancelled） */
  error: string | null
}
