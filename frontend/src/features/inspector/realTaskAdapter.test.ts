/**
 * Verify realTaskAdapter — 任务审查面板生产路径数据源。
 *
 * 覆盖：
 * - ingestRealTaskData 写入按 threadId 归属，跨 threadId 不串
 * - realTaskAdapter.getTask 同步返回构造好的 TaskResult
 * - StreamStatus → TaskStatus 映射覆盖六种对外状态
 * - 已知限制：files / validation 在 SSE 接入工具输出前为空，
 *   但 validation.status 固定 not_run（不假装通过）
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import {
  clearRealTaskData,
  ingestRealTaskData,
  realTaskAdapter,
} from './realTaskAdapter'
import type { RunEvent } from '../../shared/types/events'
import type { StreamStatus } from '../threads/useAgentStream'

function makeEvent(overrides: Partial<RunEvent> = {}): RunEvent {
  return {
    run_id: 'r1',
    thread_id: 't1',
    event_type: 'run.started',
    sequence: 1,
    timestamp: 1736000000, // 2025-01-04T16:53:20Z → 任意时间戳即可
    answer: null,
    error: null,
    attempt: null,
    operation: null,
    tool_name: null,
    duration_seconds: null,
    ...overrides,
  }
}

beforeEach(() => {
  clearRealTaskData()
})

afterEach(() => {
  clearRealTaskData()
})

// ---- 缓存归属 ----

describe('realTaskAdapter 缓存归属', () => {
  it('ingest 后按 threadId 归属，跨 threadId 不串', () => {
    ingestRealTaskData('thread-A', {
      streamStatus: 'completed',
      events: [],
      runId: 'abcd1234',
      title: 'A 的会话',
    })
    ingestRealTaskData('thread-B', {
      streamStatus: 'failed',
      events: [],
      runId: 'wxyz5678',
      title: 'B 的会话',
    })

    expect(realTaskAdapter.getTask('thread-A')?.status).toBe('completed')
    expect(realTaskAdapter.getTask('thread-A')?.title).toBe('A 的会话')
    expect(realTaskAdapter.getTask('thread-B')?.status).toBe('failed')
    expect(realTaskAdapter.getTask('thread-B')?.title).toBe('B 的会话')
  })

  it('threadId=null 返回 null；未知 threadId 返回 null', () => {
    ingestRealTaskData('thread-X', {
      streamStatus: 'completed',
      events: [],
      runId: 'rX',
      title: 'X',
    })
    expect(realTaskAdapter.getTask(null)).toBeNull()
    expect(realTaskAdapter.getTask('thread-Y')).toBeNull()
  })

  it('传入 null 数据会清除该 threadId 的缓存', () => {
    ingestRealTaskData('thread-Q', {
      streamStatus: 'completed',
      events: [],
      runId: 'rQ',
      title: 'Q',
    })
    expect(realTaskAdapter.getTask('thread-Q')).not.toBeNull()
    ingestRealTaskData('thread-Q', null)
    expect(realTaskAdapter.getTask('thread-Q')).toBeNull()
  })

  it('clearRealTaskData 清空所有缓存', () => {
    ingestRealTaskData('thread-A', { streamStatus: 'completed', events: [], runId: 'rA', title: 'A' })
    ingestRealTaskData('thread-B', { streamStatus: 'failed', events: [], runId: 'rB', title: 'B' })
    clearRealTaskData()
    expect(realTaskAdapter.getTask('thread-A')).toBeNull()
    expect(realTaskAdapter.getTask('thread-B')).toBeNull()
  })
})

// ---- StreamStatus → TaskStatus 映射 ----

describe('realTaskAdapter 状态映射', () => {
  const statusCases: Array<{ status: StreamStatus | null; expected: string }> = [
    { status: 'idle', expected: 'running' },
    { status: 'connecting', expected: 'running' },
    { status: 'streaming', expected: 'running' },
    { status: 'completed', expected: 'completed' },
    { status: 'failed', expected: 'failed' },
    { status: 'error', expected: 'failed' },
    { status: 'stopped', expected: 'stopped' },
    { status: 'cancelled', expected: 'stopped' },
    { status: 'timed_out', expected: 'timeout' },
    { status: null, expected: 'running' },
  ]

  for (const { status, expected } of statusCases) {
    it(`streamStatus=${String(status)} → task.status=${expected}`, () => {
      ingestRealTaskData('thread-Z', {
        streamStatus: status,
        events: [],
        runId: null,
        title: 'Z',
      })
      expect(realTaskAdapter.getTask('thread-Z')?.status).toBe(expected)
    })
  }
})

// ---- 已知限制：files / validation 占位 ----

describe('realTaskAdapter 占位说明', () => {
  it('files 当前固定为空数组（SSE payload 未带工具输出）', () => {
    ingestRealTaskData('thread-F', {
      streamStatus: 'completed',
      events: [],
      runId: 'rF',
      title: 'F',
    })
    expect(realTaskAdapter.getTask('thread-F')?.files).toEqual([])
  })

  it('validation.status 固定 not_run：不假装通过，UI 据此显示「未运行」', () => {
    ingestRealTaskData('thread-V', {
      streamStatus: 'completed',
      events: [],
      runId: 'rV',
      title: 'V',
    })
    const v = realTaskAdapter.getTask('thread-V')?.validation
    expect(v?.status).toBe('not_run')
    expect(v?.command).toBeNull()
    expect(v?.output).toBeNull()
    expect(v?.note).toMatch(/run\.result 事件推送/)
  })
})

// ---- 事件时间线 ----

describe('realTaskAdapter 事件时间线', () => {
  it('events 列表逐条转换为 TaskEvent（含中文标签）', () => {
    const events: RunEvent[] = [
      makeEvent({ event_type: 'run.started', sequence: 1 }),
      makeEvent({ event_type: 'tool.started', sequence: 2, tool_name: 'bash' }),
      makeEvent({
        event_type: 'tool.completed',
        sequence: 3,
        tool_name: 'bash',
        duration_seconds: 0.42,
      }),
      makeEvent({ event_type: 'run.completed', sequence: 4, answer: 'done' }),
    ]
    ingestRealTaskData('thread-E', {
      streamStatus: 'completed',
      events,
      runId: 'rE',
      title: 'E',
    })
    const result = realTaskAdapter.getTask('thread-E')
    expect(result?.events).toHaveLength(4)
    expect(result?.events[0]?.title).toBe('Run 开始')
    expect(result?.events[1]?.title).toBe('工具开始')
    expect(result?.events[1]?.description).toContain('bash')
    expect(result?.events[2]?.description).toContain('0.4s')
    expect(result?.events[3]?.description).toContain('已生成回答')
  })
})

// ---- run_label ----

describe('realTaskAdapter run_label', () => {
  it('有 runId 时显示短前缀 + "归属当前会话"', () => {
    ingestRealTaskData('thread-L', {
      streamStatus: 'completed',
      events: [],
      runId: 'abcdef1234',
      title: 'L',
    })
    expect(realTaskAdapter.getTask('thread-L')?.run_label).toBe(
      'run-abcd · 归属当前会话',
    )
  })

  it('runId 为 null 时显示 thread id + 尚未建立 Run', () => {
    ingestRealTaskData('thread-L2', {
      streamStatus: 'idle',
      events: [],
      runId: null,
      title: 'L2',
    })
    expect(realTaskAdapter.getTask('thread-L2')?.run_label).toBe(
      'thread: thread-L2 · 尚未建立 Run',
    )
  })
})

// ---- 重新进入 thread：保留 files/validation ----

import { ingestProjectExecuteResult } from './realTaskAdapter'

describe('realTaskAdapter 重新进入 thread', () => {
  it('run.result 写入 files 后，再次进入 thread（idle + 空 events）不丢失 files', () => {
    // 1) 第一次进入：跑完 run.result
    ingestRealTaskData('thread-R1', {
      streamStatus: 'streaming',
      events: [makeEvent({ event_type: 'run.started', sequence: 1 })],
      runId: 'run-aaaa',
      title: 'R1',
    })
    const result = JSON.stringify({
      ok: true,
      status: 'completed',
      changes: {
        files: [
          {
            path: 'foo.py',
            status: 'added',
            unified_diff: '+def f():\n+    return 1\n',
          },
        ],
      },
    })
    ingestProjectExecuteResult('thread-R1', result)
    expect(realTaskAdapter.getTask('thread-R1')?.files).toHaveLength(1)

    // 2) 用户离开 thread 再回来：组件重挂载，events 重置为空、streamStatus='idle'、runId=null
    ingestRealTaskData('thread-R1', {
      streamStatus: 'idle',
      events: [],
      runId: null,
      title: 'R1',
    })
    // files 必须保留
    expect(realTaskAdapter.getTask('thread-R1')?.files).toHaveLength(1)
    expect(realTaskAdapter.getTask('thread-R1')?.files[0]?.path).toBe('foo.py')
  })

  it('开始新 run（runId 变化）时清空旧的 files 与 validation', () => {
    ingestRealTaskData('thread-R2', {
      streamStatus: 'completed',
      events: [],
      runId: 'old-run',
      title: 'R2',
    })
    const result = JSON.stringify({
      ok: true,
      status: 'completed',
      changes: { files: [{ path: 'old.py', status: 'added', unified_diff: '' }] },
    })
    ingestProjectExecuteResult('thread-R2', result)
    expect(realTaskAdapter.getTask('thread-R2')?.files).toHaveLength(1)

    // 切换到新 run：runId 变化，且 streamStatus 变 running
    ingestRealTaskData('thread-R2', {
      streamStatus: 'streaming',
      events: [makeEvent({ event_type: 'run.started', sequence: 1 })],
      runId: 'new-run',
      title: 'R2',
    })
    expect(realTaskAdapter.getTask('thread-R2')?.files).toEqual([])
  })
})
