/**
 * 上下文用量快照 API 客户端
 *
 * 单一职责：薄包装 `apiGet` 调用 `GET /api/threads/{id}/context/usage`。
 * 不持任何状态、不解析具体业务——只把响应转成 TS 类型。
 */
import { apiGet } from '../../shared/api/client'
import type { ContextUsageResponse } from '../../shared/types/contextUsage'

/** 读取某 Thread 最近一次上下文用量快照。 */
export function fetchContextUsage(
  threadId: string,
  signal?: AbortSignal,
): Promise<ContextUsageResponse> {
  return apiGet<ContextUsageResponse>(
    `/api/threads/${encodeURIComponent(threadId)}/context/usage`,
    { signal },
  )
}