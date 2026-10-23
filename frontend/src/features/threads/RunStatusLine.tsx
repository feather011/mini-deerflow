/**
 * RunStatusLine — 主聊天区的轻量运行状态行。
 *
 * 只显示**当前在做什么**（一行），不展开事件明细 ——
 * 明细在「查看任务 → 过程」页签。状态完全由真实 SSE 事件推导。
 */
import { describeRunProgress } from './runProgress'
import type { RunEvent } from '../../shared/types/events'
import styles from './RunStatusLine.module.css'

interface RunStatusLineProps {
  /** 本次 Run 已收到的真实 SSE 事件 */
  events: readonly RunEvent[] | null | undefined
}

export function RunStatusLine({ events }: RunStatusLineProps) {
  const progress = describeRunProgress(events)
  if (!progress) return null

  return (
    <div
      className={styles.status}
      data-phase={progress.phase}
      role="status"
      aria-live="polite"
      aria-label="当前运行状态"
    >
      <span className={styles.spinner} aria-hidden="true" />
      <span className={styles.text}>{progress.text}</span>
    </div>
  )
}
