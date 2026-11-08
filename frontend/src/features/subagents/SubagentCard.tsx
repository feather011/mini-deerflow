/**
 * SubagentCard — 子任务通知条（Subagent 首版）
 *
 * 会话窗口里**只显示一条通知**，不铺开指令与结果：
 *     ◈ 子 Agent · 调研这个报错的三个可能方向        已完成 · 2.4s
 * 点击后在**右侧**打开该子 Agent 的会话窗口（SubagentConversationPanel）。
 *
 * 卡片是只读的：子任务由主 Agent 派发，用户不直接操作它；
 * 唯一的交互就是"去看看它聊了什么"。
 */
import { SUBAGENT_STATUS_LABEL, type SubagentCard } from './useSubagentCards'
import styles from './SubagentCard.module.css'

export interface SubagentCardProps {
  card: SubagentCard
  /** 点击通知条：请求在右侧打开该子任务的会话窗口 */
  onOpen?: (card: SubagentCard) => void
}

/** 指令摘要：单行显示，超长截断 */
function summarize(instruction: string, max = 40): string {
  const text = instruction.replace(/\s+/g, ' ').trim()
  return text.length > max ? `${text.slice(0, max)}…` : text
}

export function SubagentCard({ card, onOpen }: SubagentCardProps) {
  const label = SUBAGENT_STATUS_LABEL[card.status] ?? card.status
  const isTerminal =
    card.status === 'SUCCEEDED' ||
    card.status === 'FAILED' ||
    card.status === 'CANCELLED' ||
    card.status === 'TIMED_OUT'

  return (
    <button
      type="button"
      className={styles.card}
      data-status={card.status}
      data-testid="subagent-card"
      aria-label={`子任务：${card.instruction}（${label}），点击查看子 Agent 会话`}
      onClick={() => onOpen?.(card)}
      title={card.instruction}
    >
      <span className={styles.icon} aria-hidden="true">
        ◈
      </span>
      <span className={styles.name}>子 Agent</span>
      <span className={styles.instruction} data-testid="subagent-instruction">
        {summarize(card.instruction)}
      </span>
      <span className={styles.status}>{label}</span>
      {card.duration_seconds != null ? (
        <span className={styles.duration}>{card.duration_seconds.toFixed(1)}s</span>
      ) : null}
      {card.error_type ? (
        <span className={styles.error} data-testid="subagent-error">
          {card.error_type}
        </span>
      ) : null}
      {!isTerminal ? <span className={styles.meta}>执行中…</span> : null}
      <span className={styles.openHint} aria-hidden="true">
        ›
      </span>
    </button>
  )
}
