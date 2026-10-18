/**
 * TOOL-05 ConfirmationCardList — round 2
 * 按 run_id 分组；将真实 activeRunId 透传给每张卡。
 */
import type { ConfirmationResponse } from '../../shared/types/confirmations'
import { ConfirmationCard } from './ConfirmationCard'

export interface ConfirmationCardListProps {
  byRunId: Record<string, ConfirmationResponse[]>
  activeRunId: string | null
  online: boolean
  inFlight: Set<string>
  onDeny: (id: string) => void
  onApproveAndExecute: (id: string) => void
}

export function ConfirmationCardList({
  byRunId,
  activeRunId,
  online,
  inFlight,
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
                activeRunId={activeRunId}
                online={online}
                inFlight={inFlight}
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
