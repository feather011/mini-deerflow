/**
 * Thread API 调用函数
 *
 * 职责：
 * - 封装所有 Thread 相关 HTTP 请求
 * - 负责 URL 安全编码（thread_id 属于路径参数）
 * - 不操作 React 状态或 TanStack Query 缓存
 * - 错误由调用方统一处理
 */
import type {
  CreateThreadRequest,
  ThreadDetail,
  ThreadListResponse,
} from '../../shared/types/threads'
import { apiGet, apiPost, apiDelete } from '../../shared/api/client'

/**
 * 构造 Thread 路径（安全编码）
 * thread_id 属于 URL 路径参数，必须编码以避免改变路由结构。
 */
function threadPath(threadId: string): string {
  return `/api/threads/${encodeURIComponent(threadId)}`
}

// ---------------------------------------------------------------------------
// API 函数
// ---------------------------------------------------------------------------

/** GET /api/threads — 列出所有 Thread */
export async function listThreads(
  signal?: AbortSignal,
): Promise<ThreadListResponse> {
  return apiGet<ThreadListResponse>('/api/threads', { signal })
}

/** POST /api/threads — 创建 Thread（201） */
export async function createThread(
  request?: CreateThreadRequest,
  signal?: AbortSignal,
): Promise<ThreadDetail> {
  return apiPost<ThreadDetail>('/api/threads', {
    signal,
    ...(request !== undefined ? { body: request } : {}),
  })
}

/** GET /api/threads/{thread_id} — 读取详情 */
export async function getThread(
  threadId: string,
  signal?: AbortSignal,
): Promise<ThreadDetail> {
  return apiGet<ThreadDetail>(threadPath(threadId), { signal })
}

/** DELETE /api/threads/{thread_id} — 删除（204） */
export async function deleteThread(
  threadId: string,
  signal?: AbortSignal,
): Promise<void> {
  return apiDelete(threadPath(threadId), { signal })
}
