/**
 * RunEventPanel — 实时执行事件面板（20D）
 *
 * 职责：
 * - 显示 SSE 接收到的实时事件
 * - 显示当前状态（准备中 / 运行中 / 完成 / 失败）
 * - 显示 run_id
 * - 显示停止接收按钮
 * - 显示终态错误和 Request ID
 */
import type { RunEvent } from '../../shared/types/events'
import type { StreamStatus } from './useAgentStream'
import styles from './RunEventPanel.module.css'

interface RunEventPanelProps {
  /** 当前流状态 */
  status: StreamStatus
  /** 已接收的事件 */
  events: RunEvent[]
  /** Run ID */
  runId: string | null
  /** 错误信息 */
  errorMessage: string | null
  /** Request ID */
  requestId: string | null
  /** 终态事件 */
  terminalEvent: RunEvent | null
  /** 停止接收回调 */
  onStop: () => void
}

/** 状态文案 */
const STATUS_LABELS: Record<StreamStatus, string> = {
  idle: '准备连接…',
  connecting: '连接中…',
  streaming: '运行中',
  completed: '执行完成',
  failed: '执行失败',
  timed_out: '执行超时',
  cancelled: '执行取消',
  stopped: '已停止',
  error: '连接错误',
}

export function RunEventPanel({
  status,
  events,
  runId,
  errorMessage,
  requestId,
  terminalEvent,
  onStop,
}: RunEventPanelProps) {
  const isRunning = status === 'streaming' || status === 'connecting'
  // 从已收到的事件中提取最后一个错误信息（给摘要行显示）
  const latestError = (() => {
    for (let i = events.length - 1; i >= 0; i--) {
      if (events[i]!.error) return events[i]!.error
    }
    return null
  })()

  return (
    <div className={styles.panel}>
      {/* 状态行 */}
      <div className={styles.header}>
        <div className={styles.statusRow}>
          <span className={`${styles.statusDot} ${styles[status]}`} />
          <span className={styles.statusText}>{STATUS_LABELS[status]}</span>
          {runId && <span className={styles.runId}>Run: {runId}</span>}
        </div>

        {/* 停止接收按钮 */}
        {(isRunning || status === 'idle') && (
          <button
            type="button"
            className={styles.stopBtn}
            onClick={onStop}
            title="停止接收事件（不会取消后端 Agent）"
          >
            停止接收
          </button>
        )}
      </div>

      {/* 事件摘要行（明细下沉到 TaskInspector「过程」Tab）
          有事件时显示计数；空态时隐藏（避免与 header statusText 重复） */}
      {events.length > 0 && (
        <div className={styles.summary}>
          <span>已接收 {events.length} 条事件</span>
          {latestError && (
            <span className={styles.eventError}>{latestError}</span>
          )}
        </div>
      )}

      {/* 错误信息 */}
      {errorMessage && (
        <div className={styles.errorBar} role="alert">
          <span className={styles.errorMsg}>
            {errorMessage}
            {requestId && (
              <span className={styles.errorRequestId}>（请求编号：{requestId}）</span>
            )}
          </span>
        </div>
      )}

      {/* 终态提示 */}
      {terminalEvent && terminalEvent.answer && (
        <div className={styles.answerBar}>
          <div className={styles.answerLabel}>最终回答</div>
          <pre className={styles.answerText}>{terminalEvent.answer}</pre>
        </div>
      )}
    </div>
  )
}
