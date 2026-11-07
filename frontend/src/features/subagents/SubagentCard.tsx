/**
 * SubagentCard — 子任务卡（Subagent 首版）
 *
 * 展示一次子任务委派：指令、终态、结果摘录。
 * 卡片是**只读**的：子任务由主 Agent 派发，用户不直接操作它。
 */
import { SUBAGENT_STATUS_LABEL, type SubagentCard } from './useSubagentCards'
import styles from './SubagentCard.module.css'

export interface SubagentCardProps {
  card: SubagentCard
}

export function SubagentCard({ card }: SubagentCardProps) {
  const label = SUBAGENT_STATUS_LABEL[card.status] ?? card.status
  const isTerminal =
    card.status === 'SUCCEEDED' ||
    card.status === 'FAILED' ||
    card.status === 'CANCELLED' ||
    card.status === 'TIMED_OUT'

  return (
    <div
      className={styles.card}
      data-status={card.status}
      data-testid="subagent-card"
      role="region"
      aria-label={`子任务：${card.instruction}`}
    >
      <div className={styles.header}>
        <span aria-hidden>◈</span>
        <span className={styles.status}>{label}</span>
        <span className={styles.taskId} title="子任务 ID">
          {card.task_id}
        </span>
        {card.duration_seconds != null ? (
          <span className={styles.duration}>
            耗时 {card.duration_seconds.toFixed(1)}s
          </span>
        ) : null}
      </div>

      <div className={styles.summary}>指令：</div>
      <pre className={styles.instruction} data-testid="subagent-instruction">
        {card.instruction}
      </pre>

      {card.output ? (
        <>
          <div className={styles.summary}>结果：</div>
          <pre className={styles.output} data-testid="subagent-output">
            {card.output}
          </pre>
        </>
      ) : null}

      {!isTerminal ? (
        <div className={styles.meta}>
          <span>子 Agent 执行中…</span>
        </div>
      ) : null}

      {card.error_type ? (
        <div className={styles.meta} data-testid="subagent-error">
          <span className={styles.error}>
            失败原因：{card.error_type}
            {card.error_message ? `（${card.error_message}）` : ''}
          </span>
        </div>
      ) : null}
    </div>
  )
}
