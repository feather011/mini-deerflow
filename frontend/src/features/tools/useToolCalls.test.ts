/**
 * useToolCalls — 工具调用聚合
 *
 * 覆盖：started/completed 配对、失败、孤立事件、耗时计算。
 */
import { describe, it, expect } from 'vitest'
import { collectToolCalls } from './useToolCalls'
import type { RunEvent } from '../../shared/types/events'

function event(partial: Partial<RunEvent> & { event_type: RunEvent['event_type']; sequence: number }): RunEvent {
  return {
    run_id: 'r1',
    thread_id: 't1',
    timestamp: 1736000000,
    answer: null,
    error: null,
    attempt: null,
    operation: 'tool',
    tool_name: null,
    duration_seconds: null,
    ...partial,
  } as RunEvent
}

describe('collectToolCalls', () => {
  it('没有工具事件时返回空列表', () => {
    expect(collectToolCalls(null)).toEqual([])
    expect(collectToolCalls([])).toEqual([])
  })

  it('started 与 completed 配对成一条', () => {
    const entries = collectToolCalls([
      event({ event_type: 'tool.started', sequence: 1, tool_name: 'calculator', tool_args: 'expression=str·3字' }),
      event({ event_type: 'tool.completed', sequence: 2, tool_name: 'calculator', tool_output: '42', timestamp: 1736000000.25 }),
    ])

    expect(entries).toHaveLength(1)
    expect(entries[0]).toMatchObject({
      tool_name: 'calculator',
      args_summary: 'expression=str·3字',
      output: '42',
      status: 'succeeded',
    })
    expect(entries[0]!.duration_seconds).toBeCloseTo(0.25)
  })

  it('失败的工具标为 failed 并带错误信息', () => {
    const entries = collectToolCalls([
      event({ event_type: 'tool.started', sequence: 1, tool_name: 'project_read_file' }),
      event({ event_type: 'tool.failed', sequence: 2, tool_name: 'project_read_file', error: '工具 project_read_file 调用失败（TOOL_NOT_ALLOWED）' }),
    ])

    expect(entries).toHaveLength(1)
    expect(entries[0]!.status).toBe('failed')
    expect(entries[0]!.error).toContain('TOOL_NOT_ALLOWED')
  })

  it('执行中的工具状态是 running', () => {
    const entries = collectToolCalls([
      event({ event_type: 'tool.started', sequence: 1, tool_name: 'task_note' }),
    ])

    expect(entries[0]!.status).toBe('running')
    expect(entries[0]!.output).toBeNull()
  })

  it('没有配对的 completed 也单独记一条（事件被截断时不丢失）', () => {
    const entries = collectToolCalls([
      event({ event_type: 'tool.completed', sequence: 9, tool_name: 'history_read', tool_output: 'ok' }),
    ])

    expect(entries).toHaveLength(1)
    expect(entries[0]).toMatchObject({
      tool_name: 'history_read',
      status: 'succeeded',
      output: 'ok',
    })
  })

  it('忽略非工具事件', () => {
    const entries = collectToolCalls([
      event({ event_type: 'run.started', sequence: 0 }),
      event({ event_type: 'model.completed', sequence: 1 }),
      event({ event_type: 'subagent.completed', sequence: 2, subagent_task_id: 'sa-x' }),
    ])

    expect(entries).toEqual([])
  })
})
