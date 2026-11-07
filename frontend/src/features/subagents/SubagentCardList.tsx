/**
 * SubagentCardList — 子任务卡列表（Subagent 首版）
 *
 * 渲染在聊天区（与确认卡同位），按派发顺序展示本轮的子任务。
 */
import { SubagentCard } from './SubagentCard'
import type { SubagentCard as SubagentCardModel } from './useSubagentCards'

export interface SubagentCardListProps {
  cards: SubagentCardModel[]
}

export function SubagentCardList({ cards }: SubagentCardListProps) {
  if (cards.length === 0) return null
  return (
    <div data-testid="subagent-card-list">
      {cards.map((card) => (
        <SubagentCard key={card.task_id} card={card} />
      ))}
    </div>
  )
}
