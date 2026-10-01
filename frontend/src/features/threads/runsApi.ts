/**
 * Agent Run API 调用函数
 *
 * 职责：
 * - POST /api/threads/{thread_id}/runs
 * - 负责 URL 安全编码
 * - 不操作状态或缓存
 */
import type { AgentRunRequest, AgentRunResponse } from '../../shared/types'
import { apiPost } from '../../shared/api/client'

/**
 * 构造带 threadId 的路径（安全编码）
 */
function runsPath(threadId: string): string {
  return `/api/threads/${encodeURIComponent(threadId)}/runs`
}

/**
 * POST /api/threads/{thread_id}/runs
 * 同步执行 Agent Run，等待完成后返回结果
 */
export async function runAgent(
  threadId: string,
  request: AgentRunRequest,
  signal?: AbortSignal,
): Promise<AgentRunResponse> {
  return apiPost<AgentRunResponse>(runsPath(threadId), {
    body: request,
    signal,
  })
}
