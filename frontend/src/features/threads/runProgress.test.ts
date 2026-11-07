/**
 * runProgress — 从真实 SSE 事件推导「当前在做什么」。
 *
 * 覆盖：
 * - 没有事件时不编造状态（返回 null）
 * - 工具事件显示工具名（用户能看懂"在执行什么"）
 * - heartbeat 不影响阶段判断
 * - 终态显示终态文案
 */
import { describe, it, expect } from 'vitest'
import { describeRunProgress } from './runProgress'
import type { RunEvent } from '../../shared/types/events'

function makeEvent(overrides: Partial<RunEvent> = {}): RunEvent {
  return {
    run_id: 'r1',
    thread_id: 't1',
    event_type: 'run.started',
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

describe('describeRunProgress', () => {
  it('没有事件时返回 null（不编造"正在执行"）', () => {
    expect(describeRunProgress([])).toBeNull()
    expect(describeRunProgress(null)).toBeNull()
    expect(describeRunProgress(undefined)).toBeNull()
  })

  it('run.started → 准备中', () => {
    const p = describeRunProgress([makeEvent({ event_type: 'run.started' })])
    expect(p?.phase).toBe('starting')
    expect(p?.text).toContain('准备')
  })

  it('model.started → 思考中', () => {
    const p = describeRunProgress([
      makeEvent({ event_type: 'run.started', sequence: 1 }),
      makeEvent({ event_type: 'model.started', sequence: 2 }),
    ])
    expect(p?.phase).toBe('thinking')
    expect(p?.text).toContain('思考')
  })

  it('model.token → 正在组织回答', () => {
    const p = describeRunProgress([
      makeEvent({ event_type: 'model.started', sequence: 1 }),
      makeEvent({ event_type: 'model.token', sequence: 2, delta: '你' }),
    ])
    expect(p?.phase).toBe('generating')
  })

  it('tool.started → 显示工具名（能看懂在执行什么）', () => {
    const p = describeRunProgress([
      makeEvent({ event_type: 'model.started', sequence: 1 }),
      makeEvent({
        event_type: 'tool.started',
        sequence: 2,
        tool_name: 'project_execute',
      }),
    ])
    expect(p?.phase).toBe('tool')
    expect(p?.text).toContain('project_execute')
  })

  it('tool.started 没有工具名时也不编造工具名', () => {
    const p = describeRunProgress([
      makeEvent({ event_type: 'tool.started', sequence: 1, tool_name: null }),
    ])
    expect(p?.text).toContain('未知工具')
  })

  it('tool.completed → 工具已完成，正在汇总', () => {
    const p = describeRunProgress([
      makeEvent({
        event_type: 'tool.completed',
        sequence: 1,
        tool_name: 'project_execute',
      }),
    ])
    expect(p?.phase).toBe('tool')
    expect(p?.text).toContain('project_execute')
  })

  it('run.result → 执行结果已到达', () => {
    const p = describeRunProgress([
      makeEvent({ event_type: 'run.result', sequence: 1, tool_result: '{}' }),
    ])
    expect(p?.phase).toBe('result')
  })

  it('heartbeat 不改变阶段判断', () => {
    const p = describeRunProgress([
      makeEvent({
        event_type: 'tool.started',
        sequence: 1,
        tool_name: 'project_execute',
      }),
      makeEvent({ event_type: 'heartbeat', sequence: 2 }),
    ])
    expect(p?.phase).toBe('tool')
    expect(p?.text).toContain('project_execute')
  })

  it('终态事件 → done 阶段', () => {
    expect(
      describeRunProgress([makeEvent({ event_type: 'run.completed' })])?.phase,
    ).toBe('done')
    expect(
      describeRunProgress([makeEvent({ event_type: 'run.failed' })])?.text,
    ).toContain('失败')
    expect(
      describeRunProgress([makeEvent({ event_type: 'run.timed_out' })])?.text,
    ).toContain('超时')
    expect(
      describeRunProgress([makeEvent({ event_type: 'run.cancelled' })])?.text,
    ).toContain('取消')
  })

  it('只依据最后一条有意义的事件（乱序时取序列号最大之后的最后一条）', () => {
    const p = describeRunProgress([
      makeEvent({ event_type: 'run.started', sequence: 1 }),
      makeEvent({
        event_type: 'tool.started',
        sequence: 2,
        tool_name: 'project_execute',
      }),
      makeEvent({ event_type: 'model.completed', sequence: 3 }),
    ])
    expect(p?.text).toContain('模型已给出本轮回答')
  })
})
