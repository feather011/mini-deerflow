/**
 * SSE RunEvent 类型（20D）
 *
 * 与后端 streaming.py 的 RunEvent 对齐。
 * 字段含义：
 * - run_id / thread_id: 标识本次 Run
 * - event_type: 事件类型，见 RunEventType
 * - sequence: 事件序号，用于检测乱序
 * - timestamp: 事件时间戳
 * - answer: run.completed 时携带最终回答
 * - error: run.failed/timed_out/cancelled 时携带错误信息
 * - attempt: 模型尝试次数（可选）
 * - operation: 操作名称（可选）
 * - tool_name: 工具名称（可选）
 * - duration_seconds: 持续时间（可选）
 */

// 后端终态事件类型（每个 Run 只能有其中一个）
export const TERMINAL_EVENT_TYPES = new Set([
  'run.completed',
  'run.failed',
  'run.timed_out',
  'run.cancelled',
] as const)

export type TerminalEventType = typeof TERMINAL_EVENT_TYPES extends Set<infer T>
  ? T
  : never

// SSE 事件类型（与后端 RunEventType 对齐）
export type RunEventType =
  | 'run.started'
  | 'model.started'
  | 'model.completed'
  | 'model.failed'
  | 'tool.started'
  | 'tool.completed'
  | 'tool.failed'
  | 'run.result'
  | 'run.completed'
  | 'run.failed'
  | 'run.timed_out'
  | 'run.cancelled'
  | 'heartbeat'

/**
 * 单条 SSE RunEvent
 */
export interface RunEvent {
  /** 本次 Run 的唯一 ID */
  run_id: string
  /** 所属 Thread */
  thread_id: string
  /** 事件类型 */
  event_type: RunEventType
  /** 事件序号（单调递增） */
  sequence: number
  /** 事件时间戳（Unix 秒） */
  timestamp: number
  /** run.completed 时携带最终回答 */
  answer: string | null
  /** run.failed/timed_out/cancelled 时携带错误信息 */
  error: string | null
  /** 模型尝试次数（可选） */
  attempt: number | null
  /** 操作名称（可选） */
  operation: string | null
  /** 工具名称（可选） */
  tool_name: string | null
  /** 持续时间（可选） */
  duration_seconds: number | null
  /** run.result 事件携带 project_execute 结构化结果（JSON 字符串） */
  tool_result?: string | null
}

/** 判断事件是否为终态 */
export function isTerminalEvent(eventType: RunEventType): boolean {
  // Set.has 的形参是集合元素类型，故需上溯为 RunEventType 才能接受任意事件类型
  return (TERMINAL_EVENT_TYPES as ReadonlySet<RunEventType>).has(eventType)
}

/** 终态对应的 UI 文案 */
export const TERMINAL_MESSAGES: Record<TerminalEventType, string> = {
  'run.completed': 'Agent 执行完成',
  'run.failed': 'Agent 执行失败',
  'run.timed_out': 'Agent 执行超时',
  'run.cancelled': 'Agent 执行被取消',
} as const
