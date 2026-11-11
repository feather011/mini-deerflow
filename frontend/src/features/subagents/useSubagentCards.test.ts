/**
 * useSubagentCards — Subagent 首版
 *
 * 覆盖 G6 的前端一半：**刷新后能从持久化记录恢复卡片**。
 *
 * - SSE 事件 → 实时卡片
 * - 持久化记录 → 刷新后恢复的卡片
 * - 两者按 task_id 合并，持久化优先
 * - 非子任务事件被忽略
 */
import { describe, it, expect } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useSubagentCards, type SubagentRunRecord } from './useSubagentCards'
import type { RunEvent } from '../../shared/types/events'

function makeEvent(overrides: Partial<RunEvent> = {}): RunEvent {
  return {
    run_id: 'r1',
    thread_id: 't1',
    event_type: 'subagent.started',
    sequence: 1,
    timestamp: 1736000000,
    answer: null,
    error: null,
    attempt: null,
    operation: null,
    tool_name: null,
    duration_seconds: null,
    ...overrides,
  }
}

describe('useSubagentCards', () => {
  it('没有事件也没有记录时不产生卡片', () => {
    const { result } = renderHook(() => useSubagentCards([], []))
    expect(result.current).toHaveLength(0)
  })

  it('从 SSE 事件建卡（进行中）', () => {
    const events = [
      makeEvent({
        event_type: 'subagent.started',
        subagent_task_id: 'sa-1',
        subagent_status: 'RUNNING',
        subagent_instruction: '调研某个主题',
      }),
    ]
    const { result } = renderHook(() => useSubagentCards(events, []))
    expect(result.current).toHaveLength(1)
    expect(result.current[0]!.task_id).toBe('sa-1')
    expect(result.current[0]!.status).toBe('RUNNING')
    expect(result.current[0]!.instruction).toBe('调研某个主题')
    expect(result.current[0]!.live).toBe(true)
  })

  it('从 SSE 事件建卡（已完成，带结果）', () => {
    const events = [
      makeEvent({
        event_type: 'subagent.completed',
        subagent_task_id: 'sa-2',
        subagent_status: 'SUCCEEDED',
        subagent_instruction: '另一个任务',
        subagent_output: '子任务产出',
      }),
    ]
    const { result } = renderHook(() => useSubagentCards(events, []))
    expect(result.current[0]!.status).toBe('SUCCEEDED')
    expect(result.current[0]!.output).toBe('子任务产出')
  })

  it('刷新后从持久化记录恢复卡片（G6 前端一半）', () => {
    const persisted: SubagentRunRecord[] = [
      {
        task_id: 'sa-persisted',
        instruction: '刷新前的任务',
        status: 'SUCCEEDED',
        output: '刷新前的产出',
        duration_seconds: 1.5,
      },
    ]
    // 无 SSE 事件（刷新后就是这种状态）—— 卡片仍能从记录恢复
    const { result } = renderHook(() => useSubagentCards([], persisted))
    expect(result.current).toHaveLength(1)
    expect(result.current[0]!.task_id).toBe('sa-persisted')
    expect(result.current[0]!.status).toBe('SUCCEEDED')
    expect(result.current[0]!.output).toBe('刷新前的产出')
    expect(result.current[0]!.live).toBe(false)
  })

  it('持久化记录优先于实时事件（同 task_id）', () => {
    const events = [
      makeEvent({
        event_type: 'subagent.started',
        subagent_task_id: 'sa-3',
        subagent_status: 'RUNNING',
        subagent_instruction: '实时指令',
      }),
    ]
    const persisted: SubagentRunRecord[] = [
      {
        task_id: 'sa-3',
        instruction: '落库指令',
        status: 'SUCCEEDED',
        output: '落库产出',
      },
    ]
    const { result } = renderHook(() => useSubagentCards(events, persisted))
    expect(result.current).toHaveLength(1)
    expect(result.current[0]!.status).toBe('SUCCEEDED')
    expect(result.current[0]!.output).toBe('落库产出')
    expect(result.current[0]!.live).toBe(false)
  })

  it('忽略非子任务事件', () => {
    const events = [
      makeEvent({ event_type: 'run.started' }),
      makeEvent({ event_type: 'tool.started', tool_name: 'calculator' }),
      makeEvent({ event_type: 'run.completed' }),
    ]
    const { result } = renderHook(() => useSubagentCards(events, []))
    expect(result.current).toHaveLength(0)
  })

  it('终态不被迟到的 started 事件回退', () => {
    const events = [
      makeEvent({
        event_type: 'subagent.completed',
        subagent_task_id: 'sa-4',
        subagent_status: 'SUCCEEDED',
        subagent_output: '产出',
      }),
      // 迟到的 started（乱序到达）
      makeEvent({
        event_type: 'subagent.started',
        subagent_task_id: 'sa-4',
        subagent_status: 'RUNNING',
        sequence: 2,
      }),
    ]
    const { result } = renderHook(() => useSubagentCards(events, []))
    expect(result.current[0]!.status).toBe('SUCCEEDED')
  })

  it('失败的子任务如实展示失败状态', () => {
    const persisted: SubagentRunRecord[] = [
      {
        task_id: 'sa-fail',
        instruction: '会失败的任务',
        status: 'FAILED',
        output: '',
        error_type: 'RuntimeError',
        error_message: '模型炸了',
      },
    ]
    const { result } = renderHook(() => useSubagentCards([], persisted))
    expect(result.current[0]!.status).toBe('FAILED')
    expect(result.current[0]!.error_type).toBe('RuntimeError')
  })

  it('实时卡片带本轮锚点', () => {
    const events = [
      makeEvent({
        event_type: 'subagent.started',
        subagent_task_id: 'sa-anchor',
        subagent_status: 'RUNNING',
      }),
    ]
    const { result } = renderHook(() => useSubagentCards(events, [], 3))
    expect(result.current[0]!.after_message_count).toBe(3)
  })

  it('持久化记录带回后端记下的锚点（刷新后位置不变）', () => {
    const persisted: SubagentRunRecord[] = [
      {
        task_id: 'sa-p',
        instruction: '持久化的任务',
        status: 'SUCCEEDED',
        output: '结果',
        after_message_count: 5,
      },
    ]
    const { result } = renderHook(() => useSubagentCards([], persisted, 9))
    // 持久化锚点优先于实时锚点
    expect(result.current[0]!.after_message_count).toBe(5)
  })

  it('旧记录没有锚点时退回同任务的实时锚点，再没有则为 null', () => {
    const persisted: SubagentRunRecord[] = [
      {
        task_id: 'sa-old',
        instruction: '旧记录',
        status: 'SUCCEEDED',
        output: '结果',
      },
    ]
    // 同一任务既有实时事件又有旧记录 → 用实时锚点
    const events = [
      makeEvent({
        event_type: 'subagent.started',
        subagent_task_id: 'sa-old',
        subagent_status: 'RUNNING',
      }),
    ]
    const { result } = renderHook(() => useSubagentCards(events, persisted, 7))
    expect(result.current[0]!.after_message_count).toBe(7)

    // 只有旧记录（老会话的子任务）→ 位置未知，退回末尾
    const { result: noAnchor } = renderHook(() => useSubagentCards([], persisted))
    expect(noAnchor.current[0]!.after_message_count).toBeNull()
  })

  // ====== UI-SUBAGENT-INLINE-01：父任务取消后旧卡不串到下一轮 ======

  it('持久化锚点 0 覆盖实时锚点（取消后收回的轮次边界）', () => {
    // 后端把「本轮没落库」的子任务卡锚点收到轮次边界 0。
    // 0 不是 nullish —— 持久化记录里的 0 必须赢过实时事件带的锚点，
    // 否则卡片会被实时锚点拖到下一轮提问下面。
    const persisted: SubagentRunRecord[] = [
      {
        task_id: 'sa-cancelled',
        instruction: '被取消那一轮的子任务',
        status: 'CANCELLED',
        output: '',
        after_message_count: 0,
      },
    ]
    const events = [
      makeEvent({
        event_type: 'subagent.started',
        subagent_task_id: 'sa-cancelled',
        subagent_status: 'RUNNING',
      }),
    ]
    const { result } = renderHook(() => useSubagentCards(events, persisted, 1))
    expect(result.current[0]!.after_message_count).toBe(0)
  })

  it('刷新前后旧卡的归属一致（锚点都来自持久化记录）', () => {
    const persisted: SubagentRunRecord[] = [
      {
        task_id: 'sa-cancelled',
        instruction: '被取消那一轮的子任务',
        status: 'CANCELLED',
        output: '',
        after_message_count: 0,
      },
    ]
    // 刷新前：还有上一轮的实时事件；刷新后：只剩持久化记录。
    // 两种情况下卡片的锚点必须一致 —— 归属不因刷新而改变。
    const events = [
      makeEvent({
        event_type: 'subagent.failed',
        subagent_task_id: 'sa-cancelled',
        subagent_status: 'CANCELLED',
      }),
    ]
    const { result: beforeRefresh } = renderHook(() => useSubagentCards(events, persisted, 1))
    const { result: afterRefresh } = renderHook(() => useSubagentCards([], persisted, null))
    expect(beforeRefresh.current[0]!.after_message_count).toBe(0)
    expect(afterRefresh.current[0]!.after_message_count).toBe(0)
  })
})
