/**
 * TaskInspector — 任务审查右侧面板
 *
 * 职责：
 * - 按 threadId 展示归属该会话的一轮任务结果（生产路径下数据来自
 *   realTaskAdapter，由 AgentChatPanel 根据 SSE 状态推送；测试可
 *   通过 `adapter` prop 注入 mockTaskAdapter）
 * - 头部：任务结果标题 + 状态 pill + 关闭按钮 + run 归属副标题
 * - 三个页签（变化 / 验证 / 过程）：
 *     变化 = 文件列表 + unified diff
 *     验证 = 验证状态卡（无命令证据不显示通过）
 *     过程 = SSE 实时事件时间线（含 mock events 时附加演示段）
 * - open=false 时不渲染内容；× 按钮触发 onClose
 * - 页签与文件选择按 threadId 归属：切到别的会话显示该会话自己的状态
 *   （未交互过则回落默认页签/首个文件），不残留上一会话的选择
 */
import { useState } from 'react'
import type { JSX } from 'react'
import type {
  ChangedFile,
  TaskAdapter,
  TaskResult,
} from './types'
import { realTaskAdapter } from './realTaskAdapter'
import { useSSEContext } from '../../app/SSEContext'
import type { RunEvent } from '../../shared/types/events'
import styles from './TaskInspector.module.css'
import {
  TASK_STATUS_META,
  VALIDATION_STATUS_META,
} from './constants'

export interface TaskInspectorProps {
  /** 面板是否展开；false 时不渲染内容 */
  open: boolean
  /** 关闭回调（右上角 × 触发） */
  onClose: () => void
  /** 数据归属的会话 id；null 表示未选择会话 */
  threadId: string | null
  /** 数据源适配器；默认为 realTaskAdapter，测试可注入 mockTaskAdapter */
  adapter?: TaskAdapter
}

type TabKey = 'changes' | 'validation' | 'events'

const TABS: { key: TabKey; label: string }[] = [
  { key: 'changes', label: '变化' },
  { key: 'validation', label: '验证' },
  { key: 'events', label: '过程' },
]

/** 页签 + 文件选择状态：记录其归属的 threadId；不同会话互不串 */
interface Selection {
  threadId: string | null
  tab: TabKey
  fileIndex: number
}

export function TaskInspector({
  open,
  onClose,
  threadId,
  adapter = realTaskAdapter,
}: TaskInspectorProps): JSX.Element {
  const [selection, setSelection] = useState<Selection>({
    threadId,
    tab: 'changes',
    fileIndex: 0,
  })

  // 选择状态按 threadId 归属：记录归属其他会话时回落默认页签/首个文件
  const sameThread = selection.threadId === threadId
  const tab: TabKey = sameThread ? selection.tab : 'changes'
  const task = adapter.getTask(threadId)
  const files = task?.files ?? []
  const rawIndex = sameThread ? selection.fileIndex : 0
  const fileIndex = rawIndex < files.length ? rawIndex : 0

  if (!open) return <></>

  const statusMeta = task ? TASK_STATUS_META[task.status] : null

  return (
    <aside className={styles.panel} aria-label="任务审查面板">
      <header className={styles.header}>
        <div className={styles.titleRow}>
          <h2 className={styles.title}>任务结果</h2>
          {statusMeta && (
            <span
              className={styles.pill}
              data-tone={statusMeta.tone}
              data-testid="task-status-pill"
            >
              {statusMeta.label}
            </span>
          )}
          <button
            type="button"
            className={styles.closeBtn}
            onClick={onClose}
            aria-label="关闭任务面板"
          >
            ×
          </button>
        </div>
        <p className={styles.subtitle}>
          {task
            ? task.run_label
            : threadId
              ? `thread: ${threadId} · 此会话尚未派发操作任务`
              : '未选择会话'}
        </p>
      </header>

      <div className={styles.tabs} role="tablist" aria-label="任务详情">
        {TABS.map(({ key, label }) => (
          <button
            key={key}
            type="button"
            role="tab"
            id={`task-inspector-tab-${key}`}
            aria-selected={tab === key}
            aria-controls="task-inspector-body"
            className={`${styles.tab} ${tab === key ? styles.tabActive : ''}`}
            onClick={() => setSelection({ threadId, tab: key, fileIndex })}
          >
            {label}
          </button>
        ))}
      </div>

      <div
        className={styles.body}
        role="tabpanel"
        id="task-inspector-body"
        aria-labelledby={`task-inspector-tab-${tab}`}
      >
        {!task ? (
          <div className={styles.empty}>
            此会话暂无任务结果
            <br />
            讨论时保持只读。执行任务后，这里展示进度与结果。
          </div>
        ) : (
          <>
            {tab === 'changes' && (
              <ChangesTab
                task={task}
                fileIndex={fileIndex}
                onSelectFile={(index) =>
                  setSelection({ threadId, tab, fileIndex: index })
                }
              />
            )}
            {tab === 'validation' && <ValidationTab task={task} />}
            {tab === 'events' && <EventsTab task={task} />}
          </>
        )}
      </div>

      <footer className={styles.footer}>
        执行完成不等于业务验收通过；不自动提交或推送
      </footer>
    </aside>
  )
}

/** 变化页签：文件列表 + 选中文件的 unified diff */
function ChangesTab({
  task,
  fileIndex,
  onSelectFile,
}: {
  task: TaskResult
  fileIndex: number
  onSelectFile: (index: number) => void
}) {
  const files = task.files

  if (files.length === 0) {
    return <div className={styles.empty}>本轮暂无文件变更</div>
  }

  return (
    <div>
      <p className={styles.summary}>{files.length} 个本轮变更文件 · 原有修改单独保留</p>
      <ul className={styles.fileList}>
        {files.map((file, index) => (
          <li key={file.path}>
            <button
              type="button"
              className={`${styles.fileButton} ${index === fileIndex ? styles.fileButtonActive : ''}`}
              aria-pressed={index === fileIndex}
              onClick={() => onSelectFile(index)}
            >
              <span className={styles.fileSymbol}>{file.change}</span>
              <span className={styles.filePath}>{file.path}</span>
              <span className={styles.fileCounts}>
                +{file.added} −{file.removed}
              </span>
            </button>
          </li>
        ))}
      </ul>
      <DiffView file={files[fileIndex] as ChangedFile} />
    </div>
  )
}

/** unified diff 渲染：add/remove/context 三色行 + 新旧行号 */
function DiffView({ file }: { file: ChangedFile }) {
  return (
    <div className={styles.diff}>
      <div className={styles.diffHead}>{file.path} · 本轮变化</div>
      <div className={styles.diffLines}>
        {file.diff.map((line, index) => (
          <div
            key={index}
            className={styles.diffLine}
            data-line-type={line.type}
          >
            <span className={styles.lineNo}>{line.old_line ?? ''}</span>
            <span className={styles.lineNo}>{line.new_line ?? ''}</span>
            <span className={styles.lineText}>
              {line.type === 'add' ? '+' : line.type === 'remove' ? '-' : ' '}
              {line.text}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

/** 验证页签：验证状态卡；没有命令证据不显示测试通过 */
function ValidationTab({ task }: { task: TaskResult }) {
  const { validation } = task
  const meta = VALIDATION_STATUS_META[validation.status]
  const completedUnverified = task.status === 'completed' && validation.status === 'not_run'

  return (
    <div>
      <div className={styles.validationCard}>
        <div className={styles.validationHead}>
          <span
            className={styles.pill}
            data-tone={meta.tone}
            data-testid="validation-pill"
          >
            {meta.label}
          </span>
          {validation.command && (
            <code className={styles.validationCommand}>{validation.command}</code>
          )}
        </div>
        {validation.output && (
          <pre className={styles.validationOutput}>{validation.output}</pre>
        )}
        <p className={styles.validationNote}>{validation.note}</p>
      </div>
      {completedUnverified && (
        <p className={styles.validationHint}>
          执行完成不等于业务验收通过；没有命令证据不显示测试通过。
        </p>
      )}
      <p className={styles.validationHint}>业务验收：等待用户审查。</p>
    </div>
  )
}

/** 过程页签：实时 SSE 事件优先；mock 数据（来自 task.events）只在测试 fixture 中存在 */
function EventsTab({ task }: { task: TaskResult }) {
  const { events: realEvents } = useSSEContext()
  const hasRealEvents = realEvents.length > 0
  const hasMockEvents = task.events.length > 0

  if (!hasRealEvents && !hasMockEvents) {
    return <div className={styles.empty}>暂无事件</div>
  }

  return (
    <div>
      {hasRealEvents && (
        <>
          <p className={styles.realEventsHeader}>本轮运行过程</p>
          <ol className={styles.eventList}>
            {realEvents.map((event, index) => (
              <SSEEventItem key={index} event={event} />
            ))}
          </ol>
        </>
      )}
      {hasMockEvents && (
        <>
          {hasRealEvents && <p className={styles.mockDivider}>以下为演示数据</p>}
          <ol className={styles.eventList}>
            {task.events.map((event, index) => (
              <li key={index} className={styles.event}>
                <span className={styles.eventTime}>{event.time}</span>
                <div className={styles.eventMain}>
                  <div className={styles.eventTitle}>{event.title}</div>
                  <p className={styles.eventDesc}>{event.description}</p>
                </div>
              </li>
            ))}
          </ol>
        </>
      )}
    </div>
  )
}

/** SSE 真实事件项（与 EVENT_LABELS 对齐） */
const SSE_EVENT_LABELS: Record<string, string> = {
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

function SSEEventItem({ event }: { event: RunEvent }) {
  const label = SSE_EVENT_LABELS[event.event_type] ?? event.event_type
  const isTerminal = event.event_type.startsWith('run.')

  return (
    <li
      className={`${styles.event} ${isTerminal ? styles.eventTerminal : ''}`}
    >
      <div className={styles.eventMain}>
        <div className={styles.eventTitle}>{label}</div>
        {event.operation && (
          <p className={styles.eventDesc}>操作: {event.operation}</p>
        )}
        {event.tool_name && (
          <p className={styles.eventDesc}>工具: {event.tool_name}</p>
        )}
        {typeof event.attempt === 'number' && (
          <p className={styles.eventDesc}>尝试: {event.attempt}</p>
        )}
        {typeof event.duration_seconds === 'number' && (
          <p className={styles.eventDesc}>
            耗时: {event.duration_seconds.toFixed(1)}s
          </p>
        )}
        {event.error && (
          <p className={styles.eventError}>错误: {event.error}</p>
        )}
      </div>
    </li>
  )
}
