/**
 * TOOL-05 确认卡列表（按 run_id 分组）
 *
 * 渲染策略：
 * - 顶层按 run_id 分组
 * - 每组内按 requested_at 升序
 * - 终态（executed/denied/expired/cancelled）也展示（用户能查到结果）
 */
import type { ConfirmationResponse } from '../../shared/types/confirmations'
import { ConfirmationCard } from './ConfirmationCard'

export interface ConfirmationCardListProps {
  byRunId: Record<string, ConfirmationResponse[]>
  hasActiveRun: boolean
  online: boolean
  onDeny: (id: string) => void
  onApproveAndExecute: (id: string) => void
}

export function ConfirmationCardList({
  byRunId,
  hasActiveRun,
  online,
  onDeny,
  onApproveAndExecute,
}: ConfirmationCardListProps) {
  const runIds = Object.keys(byRunId)
  if (runIds.length === 0) return null
  return (
    <div data-testid="confirmation-card-list">
      {runIds.map((runId) => {
        const list = byRunId[runId] ?? []
        const sorted = [...list].sort((a, b) => a.requested_at - b.requested_at)
        return (
          <div key={runId} data-testid={`confirmation-group-${runId}`}>
            {sorted.map((c) => (
              <ConfirmationCard
                key={c.request_id}
                confirmation={c}
                hasActiveRun={hasActiveRun}
                online={online}
                onDeny={onDeny}
                onApproveAndExecute={onApproveAndExecute}
              />
            ))}
          </div>
        )
      })}
    </div>
  )
}
