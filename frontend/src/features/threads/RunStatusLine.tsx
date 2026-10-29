/**
 * RunStatusLine — 主聊天区的轻量运行状态行。
 *
 * 只显示**当前在做什么**（一行），不展开事件明细 ——
 * 明细在「查看任务 → 过程」页签。状态完全由真实 SSE 事件推导。
 */
import { describeRunProgress } from './runProgress'
import type { StreamStatus } from './useAgentStream'
import type { RunEvent } from '../../shared/types/events'
import styles from './RunStatusLine.module.css'

interface RunStatusLineProps {
  /** 本次 Run 已收到的真实 SSE 事件 */
  events: readonly RunEvent[] | null | undefined
  /** 当前流状态；只用于「已发起连接但还没有任何事件」这一个窗口 */
  status?: StreamStatus
}

export function RunStatusLine({ events, status }: RunStatusLineProps) {
  // 还没有任何事件时，唯一可知的事实是「连接已发起、后端尚未回响应头」。
  // 这段窗口如实说"正在建立连接"；其余无事件的情况仍然什么都不显示，
  // 绝不猜测 Agent 正在做什么。
  const progress =
    describeRunProgress(events) ??
    (status === 'connecting' ? { phase: 'starting' as const, text: '正在建立连接…' } : null)
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
