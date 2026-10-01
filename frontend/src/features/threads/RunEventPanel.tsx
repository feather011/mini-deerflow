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
import type { RunEvent, RunEventType } from '../../shared/types/events'
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

/** 事件类型中文标签 */
const EVENT_LABELS: Record<RunEventType, string> = {
  'run.started': 'Run 开始',
  'model.started': '模型开始',
  'model.completed': '模型完成',
  'model.failed': '模型失败',
  'tool.started': '工具开始',
  'tool.completed': '工具完成',
  'tool.failed': '工具失败',
  'run.completed': 'Run 完成',
  'run.failed': 'Run 失败',
  'run.timed_out': 'Run 超时',
  'run.cancelled': 'Run 取消',
  'heartbeat': '心跳',
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

      {/* 事件列表 */}
      <div className={styles.eventList}>
        {events.length === 0 && status === 'idle' && (
          <div className={styles.emptyHint}>等待事件…</div>
        )}

        {events.map((event, index) => (
          <EventItem key={index} event={event} />
        ))}
      </div>

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

/** 单条事件项 */
function EventItem({ event }: { event: RunEvent }) {
  const label = EVENT_LABELS[event.event_type] ?? event.event_type
  const isTerminal = event.event_type.startsWith('run.')

  return (
    <div className={`${styles.eventItem} ${isTerminal ? styles.terminal : ''}`}>
      <span className={styles.eventLabel}>{label}</span>
      {event.operation && (
        <span className={styles.eventField}>操作: {event.operation}</span>
      )}
      {event.tool_name && (
        <span className={styles.eventField}>工具: {event.tool_name}</span>
      )}
      {/* 用 typeof 判空：后端在事件里会直接省略这两个字段（undefined），
          仅写 !== null 会让 .toFixed 在 undefined 上崩溃，把整个应用带进 ErrorBoundary */}
      {typeof event.attempt === 'number' && (
        <span className={styles.eventField}>尝试: {event.attempt}</span>
      )}
      {typeof event.duration_seconds === 'number' && (
        <span className={styles.eventField}>
          耗时: {event.duration_seconds.toFixed(1)}s
        </span>
      )}
      {event.error && (
        <span className={styles.eventError}>错误: {event.error}</span>
      )}
    </div>
  )
}
