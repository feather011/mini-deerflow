/**
 * SubagentCardList — 子任务通知条列表（Subagent 首版）
 *
 * 渲染在聊天区（与确认卡同位），按派发顺序展示本轮的子任务。
 * 每条只是一行通知；点击后在右侧打开子 Agent 的会话窗口。
 */
import { SubagentCard } from './SubagentCard'
import type { SubagentCard as SubagentCardModel } from './useSubagentCards'

export interface SubagentCardListProps {
  cards: SubagentCardModel[]
  /** 点击通知条：请求在右侧打开该子任务的会话窗口 */
  onOpen?: (card: SubagentCardModel) => void
}

export function SubagentCardList({ cards, onOpen }: SubagentCardListProps) {
  if (cards.length === 0) return null
  return (
    <div data-testid="subagent-card-list">
      {cards.map((card) => (
        <SubagentCard key={card.task_id} card={card} onOpen={onOpen} />
      ))}
    </div>
  )
}
