/**
 * useSubagentCards — Subagent 首版
 *
 * 从两个来源聚��子任务卡：
 * 1. **本轮 SSE 事件**（`subagent.started` / `subagent.completed` / `subagent.failed`）
 *    → 实时进度
 * 2. **API 重读的持久化记录**（`thread.subagent_runs`）
 *    → 页面刷新后恢复（G6 的前端一半）
 *
 * 两者按 `task_id` 合并：**持久化记录优先**（它是落库后的权威事实），
 * 实时事件只补充尚未落库的部分（如正在运行中）。
 */
import { useMemo } from 'react'
import type { RunEvent } from '../../shared/types/events'

/** 一条子任务记录（与后端 ThreadState.subagent_runs 的元素对应） */
export interface SubagentRunRecord {
  task_id: string
  instruction: string
  status: string
  output: string
  error_type?: string
  error_message?: string
  started_at?: number
  finished_at?: number
  duration_seconds?: number
  tool_calls?: number
  model_calls?: number
  recorded_at?: number
}

/** 卡片视图模型 */
export interface SubagentCard {
  task_id: string
  instruction: string
  status: string
  output: string
  error_type?: string
  error_message?: string
  duration_seconds?: number
  /** 该卡片是否来自本轮实时事件（false = 从持久化记录恢复） */
  live: boolean
}

/** 终态集合：这些状态不再变化 */
const TERMINAL_STATUSES = new Set([
  'SUCCEEDED',
  'FAILED',
  'CANCELLED',
  'TIMED_OUT',
])

/** 状态 → 中文标签 */
export const SUBAGENT_STATUS_LABEL: Record<string, string> = {
  RUNNING: '执行中',
  QUEUED: '排队中',
  SUCCEEDED: '已完成',
  FAILED: '已失败',
  CANCELLED: '已取消',
  TIMED_OUT: '已超时',
}

/**
 * 从 SSE 事件里抽子任务信息。
 *
 * 只看 `subagent.*` 三种事件；其它事件一律忽略。
 */
function collectFromEvents(
  events: readonly RunEvent[] | null | undefined,
): Map<string, SubagentCard> {
  const cards = new Map<string, SubagentCard>()
  if (!events) return cards
  for (const event of events) {
    const type = event.event_type
    if (
      type !== 'subagent.started' &&
      type !== 'subagent.completed' &&
      type !== 'subagent.failed'
    ) {
      continue
    }
    const taskId = event.subagent_task_id
    if (!taskId) continue
    const existing = cards.get(taskId)
    // 终态不被后续的 started 事件回退
    if (existing && TERMINAL_STATUSES.has(existing.status) && type === 'subagent.started') {
      continue
    }
    cards.set(taskId, {
      task_id: taskId,
      instruction: event.subagent_instruction ?? existing?.instruction ?? '',
      status: event.subagent_status ?? (type === 'subagent.started' ? 'RUNNING' : 'FAILED'),
      output: event.subagent_output ?? existing?.output ?? '',
      error_type: existing?.error_type,
      error_message: existing?.error_message,
      duration_seconds: existing?.duration_seconds,
      live: true,
    })
  }
  return cards
}

/**
 * 聚��子任务卡。
 *
 * @param events 本轮 SSE 事件（可空）
 * @param persisted API 重读到的持久化记录（可空）—— 刷新后靠它恢复卡片
 */
export function useSubagentCards(
  events: readonly RunEvent[] | null | undefined,
  persisted?: readonly SubagentRunRecord[] | null,
): SubagentCard[] {
  return useMemo(() => {
    const cards = collectFromEvents(events)

    // 持久化记录优先（落库后的权威事实）
    for (const record of persisted ?? []) {
      if (!record?.task_id) continue
      cards.set(record.task_id, {
        task_id: record.task_id,
        instruction: record.instruction ?? '',
        status: record.status ?? 'SUCCEEDED',
        output: record.output ?? '',
        error_type: record.error_type,
        error_message: record.error_message,
        duration_seconds: record.duration_seconds,
        live: false,
      })
    }

    // 按记录顺序排（持久化在前），未落库的按开始时间追加
    return [...cards.values()]
  }, [events, persisted])
}
