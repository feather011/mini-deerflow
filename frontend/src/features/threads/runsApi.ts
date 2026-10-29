/**
 * Agent Run API 调用函数
 *
 * 职责：
 * - POST /api/threads/{thread_id}/runs
 * - POST /api/threads/{thread_id}/runs/stop（真实取消正在运行的 Run）
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

/** POST /api/threads/{thread_id}/runs/stop 的响应 */
export interface AgentRunStopResponse {
  status: string
  thread_id: string
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

/**
 * POST /api/threads/{thread_id}/runs/stop
 *
 * 取消该 Thread 上正在运行的 Agent Run：后端 cancel token →
 * project_execute 透传给 HarnessRunner → 子进程被回收、锁释放，
 * SSE 终态为 run.cancelled。
 *
 * 404 = 该 Thread 没有正在运行的 Run（已结束或从未启动）。
 */
export async function stopAgentRun(
  threadId: string,
  signal?: AbortSignal,
): Promise<AgentRunStopResponse> {
  return apiPost<AgentRunStopResponse>(
    `${runsPath(threadId)}/stop`,
    { signal },
  )
}
