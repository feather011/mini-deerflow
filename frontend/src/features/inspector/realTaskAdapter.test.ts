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
    // idle / null 都意味着"还没有任何 Run"，不能说成运行中
    { status: 'idle', expected: 'no_run' },
    { status: 'connecting', expected: 'running' },
    { status: 'streaming', expected: 'running' },
    { status: 'completed', expected: 'completed' },
    { status: 'failed', expected: 'failed' },
    { status: 'error', expected: 'failed' },
    { status: 'stopped', expected: 'stopped' },
    { status: 'cancelled', expected: 'stopped' },
    { status: 'timed_out', expected: 'timeout' },
    { status: null, expected: 'no_run' },
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

// ---- 无 Run 语义 + run_id_marker 回归防护 ----

describe('realTaskAdapter 无 Run 语义', () => {
  it('流未开始（idle）时状态是 no_run，不得是 running', () => {
    ingestRealTaskData('thread-IDLE', {
      streamStatus: 'idle',
      events: [],
      runId: null,
      title: 'IDLE',
    })
    expect(realTaskAdapter.getTask('thread-IDLE')?.status).not.toBe('running')
    expect(realTaskAdapter.getTask('thread-IDLE')?.status).toBe('no_run')
  })

  it('无流信息（null）时同样是 no_run', () => {
    ingestRealTaskData('thread-NULL', {
      streamStatus: null,
      events: [],
      runId: null,
      title: 'NULL',
    })
    expect(realTaskAdapter.getTask('thread-NULL')?.status).toBe('no_run')
  })

  it('无 Run 时副标题与「暂无运行」一致（都说明尚未建立 Run）', () => {
    ingestRealTaskData('thread-LBL', {
      streamStatus: 'idle',
      events: [],
      runId: null,
      title: 'LBL',
    })
    expect(realTaskAdapter.getTask('thread-LBL')?.run_label).toContain('尚未建立 Run')
  })

  it('首次 ingest 就必须落盘 run_id_marker', () => {
    // 不变式：首次建立 task 时记录的 run_id_marker 必须等于当前 runId。
    // 缺省（undefined）会让下一轮把同一个 run 误判成"新 run"从而清空结果。
    ingestRealTaskData('thread-MARK', {
      streamStatus: 'streaming',
      events: [],
      runId: 'run-1',
      title: 'MARK',
    })
    expect(realTaskAdapter.getTask('thread-MARK')?.run_id_marker).toBe('run-1')
  })

  it('同一 run 内后续事件不会清空 run.result 已写入的文件', () => {
    // 首次 ingest 就带着 runId（重连后立刻收到事件的情形）
    ingestRealTaskData('thread-RM', {
      streamStatus: 'streaming',
      events: [],
      runId: 'run-1',
      title: 'RM',
    })
    ingestProjectExecuteResult(
      'thread-RM',
      JSON.stringify({
        ok: true,
        status: 'completed',
        changes: {
          files: [
            { path: 'hello.c', status: 'added', unified_diff: '@@ -0,0 +1,1 @@\n+line1' },
          ],
        },
      }),
    )
    expect(realTaskAdapter.getTask('thread-RM')?.files).toHaveLength(1)

    // 同一 run 的终态事件再进来一次 —— 文件必须还在
    ingestRealTaskData('thread-RM', {
      streamStatus: 'completed',
      events: [],
      runId: 'run-1',
      title: 'RM',
    })
    expect(realTaskAdapter.getTask('thread-RM')?.files).toHaveLength(1)
  })

  it('换了一个 runId 才清空上一轮结果', () => {
    ingestRealTaskData('thread-RM2', {
      streamStatus: 'streaming',
      events: [],
      runId: 'run-1',
      title: 'RM2',
    })
    ingestProjectExecuteResult(
      'thread-RM2',
      JSON.stringify({
        ok: true,
        status: 'completed',
        changes: { files: [{ path: 'a.c', status: 'added', unified_diff: '@@ -0,0 +1,1 @@\n+x' }] },
      }),
    )
    ingestRealTaskData('thread-RM2', {
      streamStatus: 'streaming',
      events: [],
      runId: 'run-2',
      title: 'RM2',
    })
    expect(realTaskAdapter.getTask('thread-RM2')?.files).toEqual([])
  })
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

// ---- 变化文件：diff 正文与计数（F1） ----

describe('realTaskAdapter 变化文件 diff', () => {
  it('新增文件带 unified_diff 时：计数按 diff 正文行数，且 diff 行可渲染', () => {
    ingestRealTaskData('thread-D1', {
      streamStatus: 'completed',
      events: [],
      runId: 'run-d1',
      title: 'D1',
    })
    const result = JSON.stringify({
      ok: true,
      status: 'completed',
      changes: {
        files: [
          {
            path: 'hello.c',
            status: 'added',
            unified_diff:
              '--- /dev/null\n+++ b/hello.c\n@@ -0,0 +1,2 @@\n+#include <stdio.h>\n+int main(){return 0;}',
            diff_truncated: false,
          },
        ],
      },
    })
    ingestProjectExecuteResult('thread-D1', result)

    const file = realTaskAdapter.getTask('thread-D1')?.files[0]
    expect(file?.change).toBe('A')
    expect(file?.added).toBe(2)
    expect(file?.removed).toBe(0)
    expect(file?.diff).toHaveLength(2)
    expect(file?.diff[0]?.type).toBe('add')
    expect(file?.diff[0]?.text).toBe('#include <stdio.h>')
    expect(file?.diff[1]?.new_line).toBe(2)
  })

  it('修改文件：added / removed 计数与 diff 正文一致', () => {
    ingestRealTaskData('thread-D2', {
      streamStatus: 'completed',
      events: [],
      runId: 'run-d2',
      title: 'D2',
    })
    const result = JSON.stringify({
      ok: true,
      status: 'completed',
      changes: {
        files: [
          {
            path: 'a.py',
            status: 'modified',
            change: 'content_changed',
            unified_diff:
              '--- a/a.py\n+++ b/a.py\n@@ -1,3 +1,3 @@\n line1\n-line2\n+CHANGED\n line3',
          },
        ],
      },
    })
    ingestProjectExecuteResult('thread-D2', result)

    const file = realTaskAdapter.getTask('thread-D2')?.files[0]
    expect(file?.change).toBe('M')
    expect(file?.added).toBe(1)
    expect(file?.removed).toBe(1)
    expect(file?.diff.map((l) => l.type)).toEqual([
      'context',
      'remove',
      'add',
      'context',
    ])
    // 上下文行不带 diff 的前导空格（渲染时由组件补一个）
    expect(file?.diff[0]?.text).toBe('line1')
  })

  it('hunk 内的 "--- "/"+++ " 内容行不被误当文件头丢掉', () => {
    ingestRealTaskData('thread-D3', {
      streamStatus: 'completed',
      events: [],
      runId: 'run-d3',
      title: 'D3',
    })
    const result = JSON.stringify({
      ok: true,
      status: 'completed',
      changes: {
        files: [
          {
            path: 'x.md',
            status: 'modified',
            unified_diff:
              '--- a/x.md\n+++ b/x.md\n@@ -1,2 +1,2 @@\n---- old heading\n+++ new heading',
          },
        ],
      },
    })
    ingestProjectExecuteResult('thread-D3', result)

    const file = realTaskAdapter.getTask('thread-D3')?.files[0]
    // 两行都在（removed 1 行、added 1 行），没有因为 "---"/"+++" 前缀被吞掉
    expect(file?.diff).toHaveLength(2)
    expect(file?.removed).toBe(1)
    expect(file?.added).toBe(1)
    expect(file?.diff[0]?.text).toBe('--- old heading')
    expect(file?.diff[1]?.text).toBe('++ new heading')
  })

  it('diff 被截断时透出 diff_truncated 标记', () => {
    ingestRealTaskData('thread-D4', {
      streamStatus: 'completed',
      events: [],
      runId: 'run-d4',
      title: 'D4',
    })
    const result = JSON.stringify({
      ok: true,
      status: 'completed',
      changes: {
        files: [
          {
            path: 'big.py',
            status: 'added',
            unified_diff: '--- /dev/null\n+++ b/big.py\n@@ -0,0 +1,1 @@\n+x = 1',
            diff_truncated: true,
          },
        ],
      },
    })
    ingestProjectExecuteResult('thread-D4', result)
    expect(realTaskAdapter.getTask('thread-D4')?.files[0]?.diff_truncated).toBe(true)
  })

  it('没有 diff 正文时计数不谎报（新增文件 added 为 0）', () => {
    ingestRealTaskData('thread-D5', {
      streamStatus: 'completed',
      events: [],
      runId: 'run-d5',
      title: 'D5',
    })
    const result = JSON.stringify({
      ok: true,
      status: 'completed',
      changes: { files: [{ path: 'bin.dat', status: 'added', reason: 'binary file' }] },
    })
    ingestProjectExecuteResult('thread-D5', result)

    const file = realTaskAdapter.getTask('thread-D5')?.files[0]
    expect(file?.added).toBe(0)
    expect(file?.removed).toBe(0)
    expect(file?.diff).toEqual([])
  })
})
