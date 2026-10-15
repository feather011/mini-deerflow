/**
 * realTaskAdapter — 任务审查面板的真实数据适配器（生产路径）
 *
 * 数据来源：AgentChatPanel 在 SSE 与 ThreadDetail 变化时通过
 * `ingestRealTaskData()` 写入模块级缓存（按 threadId 归属）。
 *
 * run.result 事件携带 project_execute 的结构化结果（tool_result = JSON 字符串）。
 * 收到后解析并更新缓存中的 files / validation 字段，使面板立即刷新。
 *
 * 已知限制（必须如实呈现，详见 closing 报告）：
 * - run.result 只在 project_execute 工具被调用且成功时出现；
 *   没有项目执行器调用的 run 不携带此事件，files/validation 保持空/未运行。
 * - 刷新页面后缓存清空，重新建立 SSE 连接时会从空状态开始（可接受）。
 */
import type {
  ChangedFile,
  DiffLine,
  TaskAdapter,
  TaskEvent,
  TaskResult,
  TaskStatus,
  ValidationEvidence,
} from './types'
import type { RunEvent, RunEventType } from '../../shared/types/events'
import type { StreamStatus } from '../threads/useAgentStream'

/** 真实数据摄入口的输入形状 */
export interface RealTaskDataInput {
  streamStatus: StreamStatus | null
  events: readonly RunEvent[]
  runId: string | null
  title: string
}

/** 项目执行器结构化结果 */
export interface ProjectExecuteResult {
  ok: boolean
  status: string
  task_id?: string
  answer?: string
  files_written?: string[]
  verification?: Array<{
    command?: string
    command_truncated?: boolean
    output?: string
    output_truncated?: boolean
    exit_code?: number | null
    duration_s?: number | null
    has_output?: boolean
  }>
  changes?: {
    files?: Array<{
      path: string
      status: 'added' | 'modified' | 'deleted'
      change?: string
      unified_diff?: string
      diff_truncated?: boolean
      reason?: string
      diff?: unknown
    }>
    baseline_complete?: boolean
    snapshot_complete?: boolean
    after_incomplete_deleted?: string[]
  }
  tool_events?: Array<{ name?: string; duration_s?: number }>
  tool_calls?: number
  error?: string
}

const cache: Map<string, TaskResult> = new Map()

/** 写入或清除指定 thread 的真实任务数据（data=null 表示清除） */
export function ingestRealTaskData(
  threadId: string,
  data: RealTaskDataInput | null,
): void {
  if (data === null) {
    cache.delete(threadId)
    return
  }
  const existing = cache.get(threadId)
  const status = mapStreamStatus(data.streamStatus)
  const run_label = data.runId
    ? `run-${data.runId.slice(0, 4)} · 归属当前会话`
    : `thread: ${threadId} · 尚未建立 Run`
  const events = data.events.map(convertToTimelineEvent)

  if (!existing) {
    // 首次进入：建立 task，结果字段用默认占位
    cache.set(threadId, {
      thread_id: threadId,
      run_label,
      title: data.title,
      status,
      validation: {
        status: 'not_run',
        command: null,
        output: null,
        note: '执行器输出尚未通过 run.result 事件推送。',
      },
      files: [],
      events,
    })
    return
  }

  // 已存在：只更新元数据（status/run_label/events/title），
  // 保留 files/validation —— 它们由 ingestProjectExecuteResult 独立维护，
  // 不应被 SSE 重连或组件重挂载清空。
  // 清空条件：检测到开始新的 run（runId 与缓存不一致）
  // - 同一 run 内的所有事件（model.* / tool.* / run.result）都不清空
  // - 新 run 开始（runId 变化）才清空
  const isNewRun =
    data.runId != null && data.runId !== existing.run_id_marker
  if (isNewRun) {
    existing.validation = {
      status: 'not_run',
      command: null,
      output: null,
      note: '执行器输出尚未通过 run.result 事件推送。',
    }
    existing.files = []
  }
  existing.status = status
  existing.run_label = run_label
  existing.title = data.title
  existing.run_id_marker = data.runId
  existing.events = events
  cache.set(threadId, existing)
}

/**
 * 解析 run.result 事件中的 project_execute 结果并更新缓存。
 * @param threadId 归属的 thread
 * @param toolResult JSON 字符串（project_execute 返回的 ok_response）
 */
export function ingestProjectExecuteResult(
  threadId: string,
  toolResult: string,
): void {
  const task = cache.get(threadId)
  if (!task) return // 未建立任务数据时忽略

  let parsed: ProjectExecuteResult
  try {
    parsed = JSON.parse(toolResult) as ProjectExecuteResult
  } catch {
    return // 非 JSON 时忽略
  }

  // 更新 validation
  task.validation = buildValidationFromResult(parsed)

  // 更新 files
  task.files = buildChangedFilesFromResult(parsed)

  // 强制触发 React 重渲染：替换整个缓存条目
  cache.set(threadId, { ...task })
}

/** 调试 / 重置用：清空所有缓存 */
export function clearRealTaskData(): void {
  cache.clear()
}

/** 生产路径使用的 TaskAdapter：从模块级缓存读真实数据 */
export const realTaskAdapter: TaskAdapter = {
  getTask(threadId: string | null): TaskResult | null {
    if (!threadId) return null
    return cache.get(threadId) ?? null
  },
}

// ---- 构造 ----

function mapStreamStatus(status: StreamStatus | null): TaskStatus {
  switch (status) {
    case 'completed':
      return 'completed'
    case 'failed':
    case 'error':
      return 'failed'
    case 'cancelled':
    case 'stopped':
      return 'stopped'
    case 'timed_out':
      return 'timeout'
    case 'idle':
    case 'connecting':
    case 'streaming':
    default:
      return 'running'
  }
}

function buildValidationFromResult(result: ProjectExecuteResult): ValidationEvidence {
  if (!result.verification || result.verification.length === 0) {
    // 有 changes 但没有 verification：执行了但未运行验证命令
    if (result.changes && result.changes.files && result.changes.files.length > 0) {
      return {
        status: 'not_run',
        command: null,
        output: null,
        note: '本轮执行器运行完成，但未运行验证命令。',
      }
    }
    // 没有任何结果
    return {
      status: 'not_run',
      command: null,
      output: null,
      note: '执行器输出当前未通过 SSE 推送。',
    }
  }

  const verifications = result.verification
  const allPassed = verifications.every((v) => v.exit_code === 0)
  const anyFailed = verifications.some((v) => v.exit_code != null && v.exit_code !== 0)

  let status: ValidationEvidence['status']
  if (result.status === 'completed') {
    status = allPassed ? 'passed' : anyFailed ? 'failed' : 'not_run'
  } else if (result.status === 'stopped') {
    status = 'interrupted'
  } else {
    status = 'interrupted'
  }

  // 合并多个验证命令：取第一个命令作为主证据
  const primary = verifications[0]
  if (!primary) {
    return {
      status: 'not_run',
      command: null,
      output: null,
      note: '验证命令无记录。',
    }
  }

  return {
    status,
    command: primary.command ?? null,
    output: primary.output ?? null,
    note:
      status === 'passed'
        ? '验证命令已执行且全部退出码为 0。'
        : status === 'failed'
          ? `验证失败（退出码 ${primary.exit_code ?? '未知'}）。`
          : status === 'interrupted'
            ? '任务被停止，验证未完成。'
            : '执行完成但未运行验证命令。',
  }
}

function buildChangedFilesFromResult(result: ProjectExecuteResult): ChangedFile[] {
  const changes = result.changes?.files
  if (!changes || changes.length === 0) return []

  return changes.map((f) => {
    const added =
      f.status === 'added'
        ? (f.unified_diff?.split('\n').filter((l) => l.startsWith('+') && !l.startsWith('+++')).length ?? 0)
        : 0
    const removed =
      f.status === 'modified'
        ? (f.unified_diff?.split('\n').filter((l) => l.startsWith('-') && !l.startsWith('---')).length ?? 0)
        : f.status === 'deleted'
          ? 1
          : 0

    return {
      path: f.path,
      change: f.status === 'added' ? 'A' : f.status === 'modified' ? 'M' : 'D',
      added,
      removed,
      diff: parseUnifiedDiff(f.unified_diff ?? ''),
    }
  })
}

function parseUnifiedDiff(diff: string): DiffLine[] {
  if (!diff) return []
  const lines = diff.split('\n')
  const result: DiffLine[] = []
  let oldLine = 0
  let newLine = 0

  for (const raw of lines) {
    if (raw.startsWith('---') || raw.startsWith('+++') || raw.startsWith('@@')) {
      // 解析 @@ 行中的行号
      const m = raw.match(/@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/)
      if (m?.[1] && m?.[2]) {
        oldLine = parseInt(m[1]!, 10)
        newLine = parseInt(m[2]!, 10)
      }
      continue
    }
    if (raw.startsWith('+')) {
      result.push({ type: 'add', old_line: null, new_line: newLine++, text: raw.slice(1) })
    } else if (raw.startsWith('-')) {
      result.push({ type: 'remove', old_line: oldLine++, new_line: null, text: raw.slice(1) })
    } else if (raw.length > 0) {
      result.push({ type: 'context', old_line: oldLine++, new_line: newLine++, text: raw })
    }
  }
  return result
}

function buildTaskResult(threadId: string, data: RealTaskDataInput): TaskResult {
  // 已废弃：原实现每次都重建 task 并把 files/validation 重置为空，
  // 导致「重新进入 thread」或「SSE 重连」时把上一次 run.result 的成果清空。
  // 保留以避免外部测试引用；新逻辑在 ingestRealTaskData 内联处理。
  const status = mapStreamStatus(data.streamStatus)
  return {
    thread_id: threadId,
    run_label: data.runId
      ? `run-${data.runId.slice(0, 4)} · 归属当前会话`
      : `thread: ${threadId} · 尚未建立 Run`,
    title: data.title,
    status,
    validation: {
      status: 'not_run',
      command: null,
      output: null,
      note: '执行器输出尚未通过 run.result 事件推送。',
    },
    files: [], // 默认空；run.result 事件触发更新
    events: data.events.map(convertToTimelineEvent),
    run_id_marker: data.runId,
  }
}

// ---- 工具：把 SSE RunEvent 转为 TaskEvent 时间线条目 ----

const SSE_EVENT_LABELS: Record<RunEventType, string> = {
  'run.started': 'Run 开始',
  'model.started': '模型开始',
  'model.token': 'Token 流式',
  'model.completed': '模型完成',
  'model.failed': '模型失败',
  'tool.started': '工具开始',
  'tool.completed': '工具完成',
  'tool.failed': '工具失败',
  'run.result': '执行结果',
  'run.completed': 'Run 完成',
  'run.failed': 'Run 失败',
  'run.timed_out': 'Run 超时',
  'run.cancelled': 'Run 取消',
  'heartbeat': '心跳',
}

function convertToTimelineEvent(event: RunEvent): TaskEvent {
  const time = formatTimestamp(event.timestamp)
  const title = SSE_EVENT_LABELS[event.event_type] ?? event.event_type
  const description = buildEventDescription(event)
  return { time, title, description }
}

function buildEventDescription(event: RunEvent): string {
  const parts: string[] = []
  if (event.tool_name) parts.push(`工具 ${event.tool_name}`)
  if (typeof event.attempt === 'number') parts.push(`第 ${event.attempt + 1} 次`)
  if (typeof event.duration_seconds === 'number') {
    parts.push(`耗时 ${event.duration_seconds.toFixed(1)}s`)
  }
  if (event.error) parts.push(`错误：${event.error}`)
  if (event.answer) parts.push('已生成回答')
  if (event.event_type === 'run.result') parts.push('执行器结果已到达')
  return parts.length === 0 ? '执行中' : parts.join(' · ')
}

function formatTimestamp(ts: number): string {
  const d = new Date(ts * 1000)
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  const ss = String(d.getSeconds()).padStart(2, '0')
  return `${hh}:${mm}:${ss}`
}
