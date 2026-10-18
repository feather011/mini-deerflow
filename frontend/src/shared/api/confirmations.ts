/**
 * TOOL-05 确认 API 客户端
 *
 * 浏览器侧不持有原始参数。展示用 arguments_display；执行走
 * execute-snapshot，服务端从 ConfirmationRequest.arguments 读原值。
 */
import { apiGet, apiPost } from './client'
import type {
  ActiveRunResponse,
  ConfirmationListResponse,
  ConfirmationResponse,
  ExecuteSnapshotRequest,
} from '../types/confirmations'

const PREFIX = '/confirmations'

export async function listConfirmations(params: {
  thread_id?: string
  run_id?: string
  state?: string
} = {}): Promise<ConfirmationResponse[]> {
  const qs = new URLSearchParams()
  if (params.thread_id !== undefined) qs.set('thread_id', params.thread_id)
  if (params.run_id !== undefined) qs.set('run_id', params.run_id)
  if (params.state !== undefined) qs.set('state', params.state)
  const url = qs.toString() ? `${PREFIX}?${qs.toString()}` : PREFIX
  const resp = await apiGet<ConfirmationListResponse>(url)
  return resp.confirmations
}

export async function getConfirmation(id: string): Promise<ConfirmationResponse> {
  return apiGet<ConfirmationResponse>(`${PREFIX}/${encodeURIComponent(id)}`)
}

export async function approveConfirmation(
  id: string,
  decided_by = 'user',
): Promise<ConfirmationResponse> {
  return apiPost<ConfirmationResponse>(
    `${PREFIX}/${encodeURIComponent(id)}/approve`,
    { body: { decided_by } },
  )
}

export async function denyConfirmation(
  id: string,
  decided_by = 'user',
): Promise<ConfirmationResponse> {
  return apiPost<ConfirmationResponse>(
    `${PREFIX}/${encodeURIComponent(id)}/deny`,
    { body: { decided_by } },
  )
}

export async function cancelConfirmation(
  id: string,
  decided_by = 'user',
): Promise<ConfirmationResponse> {
  return apiPost<ConfirmationResponse>(
    `${PREFIX}/${encodeURIComponent(id)}/cancel`,
    { body: { decided_by } },
  )
}

/** TOOL-05 快照执行：服务端读 ConfirmationRequest.arguments 执行。 */
export async function executeSnapshot(
  id: string,
  body: ExecuteSnapshotRequest,
): Promise<ConfirmationResponse> {
  return apiPost<ConfirmationResponse>(
    `${PREFIX}/${encodeURIComponent(id)}/execute-snapshot`,
    { body },
  )
}

/** 旧执行入口（带 arguments 客户端回传）— 保留兼容，但新代码不应用。 */
export async function executeConfirmation(
  id: string,
  body: {
    run_id: string
    thread_id: string
    tool_name: string
    arguments: Record<string, unknown>
  },
): Promise<ConfirmationResponse> {
  return apiPost<ConfirmationResponse>(
    `${PREFIX}/${encodeURIComponent(id)}/execute`,
    { body },
  )
}

/** TOOL-05 活跃 run 探针。 */
export async function getActiveRun(thread_id: string): Promise<ActiveRunResponse> {
  return apiGet<ActiveRunResponse>(
    `/threads/${encodeURIComponent(thread_id)}/active_run`,
  )
}
