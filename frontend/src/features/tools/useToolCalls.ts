/**
 * useToolCalls — 从 SSE 事件里聚合工具调用（会话内折叠展示用）
 *
 * 数据来源：`tool.started` / `tool.completed` / `tool.failed` 三种事件。
 * 一次工具调用 = 一个 started + 后续的 completed（或 failed）。
 *
 * 配对方式：**后进先出**（最近一个未完成的 started 先被完成）。
 * 本项目的 Agent 循环里工具是顺序执行的，LIFO 与 FIFO 等价；
 * 万一出现并发/嵌套，LIFO 也更贴近"内层先结束"的实际顺序。
 */
import { useMemo } from 'react'
import type { RunEvent } from '../../shared/types/events'

/** 一次工具调用的聚合结果 */
export interface ToolCallEntry {
  /** 稳定唯一键（同一 run 内不重复） */
  key: string
  /** 工具名 */
  tool_name: string
  /** 入参摘要（键名 + 形状，不含实际值） */
  args_summary: string | null
  /** 结果文本（后端已截断） */
  output: string | null
  /** 失败原因 */
  error: string | null
  /** 执行状态 */
  status: 'running' | 'succeeded' | 'failed'
  /** 耗时（秒）；由事件时间戳相减得到 */
  duration_seconds: number | null
  /**
   * 时序锚点：该工具调用发生在第几条消息之后。
   *
   * 一次 run 只对应一轮问答，因此同一 run 里的工具调用共用同一个锚点
   * （= 本轮 user 消息的位置）。null = 位置未知（前端退回末尾展示）。
   */
  after_message_count: number | null
}

interface Pending {
  key: string
  tool_name: string
  args_summary: string | null
  started_at: number
}

const TOOL_EVENT_TYPES = new Set(['tool.started', 'tool.completed', 'tool.failed'])

/**
 * 聚合工具调用。
 *
 * 纯函数，便于单测（不依赖 React）。
 *
 * @param events 本轮 SSE 事件
 * @param anchor 本轮工具调用的时序锚点（第几条消息之后）；null = 未知
 */
export function collectToolCalls(
  events: readonly RunEvent[] | null | undefined,
  anchor?: number | null,
): ToolCallEntry[] {
  if (!events) return []
  const afterMessageCount = anchor ?? null
  const entries: ToolCallEntry[] = []
  const pending: Pending[] = []

  for (const event of events) {
    if (!TOOL_EVENT_TYPES.has(event.event_type)) continue
    const runId = event.run_id
    const name = event.tool_name ?? '未知工具'

    if (event.event_type === 'tool.started') {
      const item: Pending = {
        key: `${runId}#${event.sequence}`,
        tool_name: name,
        args_summary: event.tool_args ?? null,
        started_at: event.timestamp,
      }
      pending.push(item)
      entries.push({
        key: item.key,
        tool_name: name,
        args_summary: item.args_summary,
        output: null,
        error: null,
        status: 'running',
        duration_seconds: null,
        after_message_count: afterMessageCount,
      })
      continue
    }

    // completed / failed：配对最近一个未完成的 started
    const index = findLastIndex(pending, (p) => p.tool_name === name)
    const matched = index >= 0 ? pending.splice(index, 1)[0] : null
    const entry = matched
      ? entries.find((e) => e.key === matched.key)
      : undefined

    const failed = event.event_type === 'tool.failed'
    const duration =
      matched != null ? Math.max(0, event.timestamp - matched.started_at) : null

    if (entry) {
      entry.status = failed ? 'failed' : 'succeeded'
      entry.output = event.tool_output ?? null
      entry.error = event.error ?? null
      entry.duration_seconds = duration
    } else {
      // 没有配对的 started（事件被截断/丢失）：单独记一条，
      // 免得工具调用凭空消失。
      entries.push({
        key: `${runId}#${event.sequence}`,
        tool_name: name,
        args_summary: null,
        output: event.tool_output ?? null,
        error: event.error ?? null,
        status: failed ? 'failed' : 'succeeded',
        duration_seconds: null,
        after_message_count: afterMessageCount,
      })
    }
  }

  return entries
}

/** findLastIndex（ES2023 才有目标环境不一定支持，手写一个）。 */
function findLastIndex<T>(
  items: readonly T[],
  predicate: (item: T) => boolean,
): number {
  for (let i = items.length - 1; i >= 0; i -= 1) {
    if (predicate(items[i]!)) return i
  }
  return -1
}

/** React Hook 版本 */
export function useToolCalls(
  events: readonly RunEvent[] | null | undefined,
  anchor?: number | null,
): ToolCallEntry[] {
  return useMemo(() => collectToolCalls(events, anchor), [events, anchor])
}
