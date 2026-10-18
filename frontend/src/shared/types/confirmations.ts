/**
 * TOOL-05 确认（confirmation）类型定义
 *
 * 服务端契约（见 src/mini_deerflow/api/routes/confirmations.py）：
 * - GET /confirmations：返回列表，可按 thread_id / run_id / state 过滤
 * - GET /confirmations/{id}：返回详情
 * - POST /confirmations/{id}/approve：批准
 * - POST /confirmations/{id}/deny：拒绝
 * - POST /confirmations/{id}/cancel：取消
 * - POST /confirmations/{id}/execute-snapshot：服务端读快照执行
 *   （请求只携带 run_id/thread_id/tool_name/arguments_hash；不携带原值）
 *
 * 重要：响应中只暴露 `arguments_display`（脱敏摘要），不返回原 `arguments`。
 */

/** 确认请求的对外视图。 */
export interface ConfirmationResponse {
  request_id: string
  run_id: string
  thread_id: string
  project_id: string | null
  /** 主体名：MAIN_AGENT / MINI_SUBAGENT / HARNESS_AGENT */
  principal: string
  tool_name: string
  arguments_hash: string
  /** 脱敏后的参数摘要（用于展示）。原值不下发。 */
  arguments_display: Record<string, unknown>
  requested_at: number
  expires_at: number
  state: 'pending' | 'approved' | 'denied' | 'expired' | 'cancelled' | 'executed'
  decided_at: number | null
  decided_by: string | null
  executed_at: number | null
  /** 工具结果：ok / error:xxx / unknown */
  execution_result: string | null
}

export interface ConfirmationListResponse {
  confirmations: ConfirmationResponse[]
}

/** execute-snapshot 请求体（不携带原值）。 */
export interface ExecuteSnapshotRequest {
  run_id: string
  thread_id: string
  tool_name: string
  arguments_hash: string
}

/** active_run 探针响应。 */
export interface ActiveRunResponse {
  run_id: string | null
}
