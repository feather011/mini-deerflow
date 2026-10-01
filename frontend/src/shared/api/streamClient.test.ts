/**
 * streamClient.ts 单元测试
 *
 * 覆盖：
 * - SSE 跨 chunk 解析
 * - 多事件同 chunk
 * - CRLF 换行
 * - 非法 JSON 跳过
 * - HTTP 错误解析
 * - 终态事件立即结束
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { connectAgentStream, StreamError } from './streamClient'
import type { RunEvent } from '../types/events'

// ---------------------------------------------------------------------------
// 辅助函数
// ---------------------------------------------------------------------------

/**
 * SSE 编码：将事件对象转为 SSE 文本块
 *
 * 每个事件以双换行（\n\n）结尾，符合 SSE 规范。
 */
function encodeSse(events: Array<{ event: string; data: object; id?: string }>): string {
  const parts: string[] = []
  for (const ev of events) {
    let block = ''
    if (ev.id !== undefined) block += `id: ${ev.id}\n`
    block += `event: ${ev.event}\n`
    block += `data: ${JSON.stringify(ev.data)}\n`
    block += '\n' // 终止空行
    parts.push(block)
  }
  return parts.join('')
}

/**
 * 创建 mock Response（一次性发送所有内容）
 */
function mockResponse(
  status: number,
  body: string | { error: { code: string; message: string; request_id: string } },
  headers: Record<string, string> = {},
) {
  const text = typeof body === 'string' ? body : JSON.stringify(body)
  const encoded = new TextEncoder().encode(text)

  return {
    status,
    headers: new Map(Object.entries(headers)),
    ok: status >= 200 && status < 300,
    body: {
      getReader() {
        let consumed = false
        return {
          async read() {
            if (consumed) return { done: true, value: new Uint8Array() }
            consumed = true
            return { done: false, value: encoded }
          },
          releaseLock() {},
        }
      },
    },
  }
}

/**
 * 测试事件工厂
 */
function ev(opts: Partial<RunEvent> = {}): RunEvent {
  return {
    run_id: 'run-1',
    thread_id: 't1',
    event_type: 'run.started',
    sequence: 0,
    timestamp: 1000,
    answer: null,
    error: null,
    attempt: null,
    operation: null,
    tool_name: null,
    duration_seconds: null,
    ...opts,
  }
}

// ---------------------------------------------------------------------------
// 测试
// ---------------------------------------------------------------------------

describe('streamClient', () => {
  const realFetch = vi.fn()

  beforeEach(() => {
    vi.clearAllMocks()
    global.fetch = realFetch
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  // ---------------------------------------------------------------------------
  // 基本功能
  // ---------------------------------------------------------------------------

  it('单一 chunk 中的多个事件正确分发', async () => {
    const events: RunEvent[] = []
    const terminals: RunEvent[] = []

    const sseText = encodeSse([
      { event: 'run.started', data: ev({ sequence: 0, event_type: 'run.started' }) },
      { event: 'model.started', data: ev({ sequence: 1, event_type: 'model.started', attempt: 1 }) },
      { event: 'run.completed', data: ev({ sequence: 2, event_type: 'run.completed', answer: '完成', duration_seconds: 10 }) },
    ])

    realFetch.mockResolvedValue(mockResponse(200, sseText, { 'X-Run-ID': 'run-1' }))

    const { runId, cancel } = connectAgentStream('t1', 'hi', {
      onEvent: (e) => {
        events.push(e)
      },
      onTerminal: (e) => terminals.push(e),
    })

    const id = await runId
    expect(id).toBe('run-1')

    // 等待终态事件
    await vi.waitFor(() => expect(terminals.length).toBeGreaterThanOrEqual(1), { timeout: 2000 })

    cancel()

    expect(events.length + terminals.length).toBeGreaterThanOrEqual(3)
    // run.completed 应该在 terminals 或 events 中
    const allEvents = [...events, ...terminals]
    expect(allEvents.some((e) => e.event_type === 'run.completed')).toBe(true)
  })

  it('跨 chunk 解析：分两次发送', async () => {
    const events: RunEvent[] = []
    const terminals: RunEvent[] = []

    // 拆成两块：第一块含 run.started（不完整），第二块含后续
    const fullText = encodeSse([
      { event: 'run.started', data: ev({ sequence: 0, event_type: 'run.started' }) },
      { event: 'model.started', data: ev({ sequence: 1, event_type: 'model.started', attempt: 1 }) },
    ])

    const splitPoint = Math.floor(fullText.length / 2)
    const chunk1 = fullText.slice(0, splitPoint)
    const chunk2 = fullText.slice(splitPoint)

    const encoded1 = new TextEncoder().encode(chunk1)
    const encoded2 = new TextEncoder().encode(chunk2)

    realFetch.mockResolvedValue({
      status: 200,
      headers: new Map([['X-Run-ID', 'run-1']]),
      ok: true,
      body: {
        getReader() {
          const chunks = [encoded1, encoded2]
          let index = 0
          return {
            async read() {
              if (index >= chunks.length) return { done: true, value: new Uint8Array() }
              return { done: false, value: chunks[index++] }
            },
            releaseLock() {},
          }
        },
      },
    })

    const { runId, cancel } = connectAgentStream('t1', 'hi', {
      onEvent: (e) => {
        events.push(e)
      },
      onTerminal: (e) => terminals.push(e),
    })

    await runId

    await vi.waitFor(() => expect(events.length).toBeGreaterThanOrEqual(2), { timeout: 2000 })

    cancel()

    expect(events.length).toBeGreaterThanOrEqual(2)
    const types = events.map((e) => e.event_type)
    expect(types).toContain('run.started')
  })

  it('CRLF 换行符（\\r\\n）正确解析', async () => {
    const events: RunEvent[] = []

    const sseText =
      'id: 0\r\nevent: run.started\r\ndata: ' + JSON.stringify(ev({ sequence: 0, event_type: 'run.started' })) + '\r\n\r\n' +
      'id: 1\r\nevent: model.started\r\ndata: ' + JSON.stringify(ev({ sequence: 1, event_type: 'model.started', attempt: 1 })) + '\r\n\r\n'

    realFetch.mockResolvedValue(mockResponse(200, sseText, { 'X-Run-ID': 'run-1' }))

    const { runId, cancel } = connectAgentStream('t1', 'hi', {
      onEvent: (e) => {
        events.push(e)
      },
    })

    await runId
    await vi.waitFor(() => expect(events.length).toBeGreaterThanOrEqual(2), { timeout: 2000 })

    cancel()

    expect(events.length).toBeGreaterThanOrEqual(2)
    expect(events[0]!.event_type).toBe('run.started')
    expect(events[1]!.event_type).toBe('model.started')
  })

  it('非法 JSON 跳过该事件，继续处理后续事件', async () => {
    const events: RunEvent[] = []

    const sseText =
      encodeSse([{ event: 'run.started', data: ev({ sequence: 0, event_type: 'run.started' }) }]) +
      'id: 1\nevent: model.started\ndata: {invalid json}\n\n' +
      encodeSse([{ event: 'tool.started', data: ev({ sequence: 2, event_type: 'tool.started', tool_name: 'bash' }) }])

    realFetch.mockResolvedValue(mockResponse(200, sseText, { 'X-Run-ID': 'run-1' }))

    const { runId, cancel } = connectAgentStream('t1', 'hi', {
      onEvent: (e) => {
        events.push(e)
      },
    })

    await runId
    await vi.waitFor(() => expect(events.length).toBeGreaterThanOrEqual(2), { timeout: 2000 })

    cancel()

    // 非法 JSON 事件被跳过，但前后事件正常
    expect(events.length).toBeGreaterThanOrEqual(2)
    expect(events.some((e) => e.event_type === 'run.started')).toBe(true)
    expect(events.some((e) => e.event_type === 'tool.started')).toBe(true)
  })

  // ---------------------------------------------------------------------------
  // HTTP 错误
  // ---------------------------------------------------------------------------

  it('HTTP 非 2xx 错误：捕获状态码和 Request ID', async () => {
    let capturedError: StreamError | null = null

    realFetch.mockResolvedValue(
      mockResponse(
        409,
        { error: { code: 'CONFLICT', message: '线程正在运行', request_id: 'req-123' } },
        { 'X-Request-ID': 'req-123' },
      ),
    )

    const { cancel } = connectAgentStream('t1', 'hi', {
      onError: (err) => {
        capturedError = err
      },
    })

    await vi.waitFor(() => expect(capturedError).not.toBeNull(), { timeout: 2000 })

    cancel()

    expect(capturedError!.status).toBe(409)
    expect(capturedError!.message).toBe('线程正在运行')
    expect(capturedError!.requestId).toBe('req-123')
  })

  // ---------------------------------------------------------------------------
  // 终态
  // ---------------------------------------------------------------------------

  it('四种终态都触发 onTerminal', async () => {
    const terminalTypes: Array<RunEvent['event_type']> = [
      'run.completed',
      'run.failed',
      'run.timed_out',
      'run.cancelled',
    ]

    for (const terminalType of terminalTypes) {
      const terminals: RunEvent[] = []
      vi.clearAllMocks()

      const data = ev({
        sequence: 0,
        event_type: terminalType,
        answer: terminalType === 'run.completed' ? 'OK' : null,
        error: terminalType !== 'run.completed' ? 'Error' : null,
        duration_seconds: terminalType === 'run.completed' ? 1 : null,
      })

      realFetch.mockResolvedValue(
        mockResponse(200, encodeSse([{ event: terminalType, data }]), { 'X-Run-ID': 'r1' }),
      )

      const { runId, cancel } = connectAgentStream('t1', 'hi', {
        onTerminal: (e) => terminals.push(e),
      })

      await runId
      await vi.waitFor(() => expect(terminals).toHaveLength(1), { timeout: 2000 })

      cancel()

      expect(terminals[0]!.event_type).toBe(terminalType)
    }
  })

  // ---------------------------------------------------------------------------
  // 注释行
  // ---------------------------------------------------------------------------

  it('注释行（: 开头）被忽略', async () => {
    const events: RunEvent[] = []

    // 注释行后面需要空行（\n\n）才能真正分隔
    const sseText =
      ': heartbeat\n\n' +
      encodeSse([{ event: 'run.started', data: ev({ sequence: 0, event_type: 'run.started' }) }]) +
      ': keepalive\n\n' +
      encodeSse([{ event: 'model.started', data: ev({ sequence: 1, event_type: 'model.started' }) }])

    realFetch.mockResolvedValue(mockResponse(200, sseText, { 'X-Run-ID': 'r1' }))

    const { runId, cancel } = connectAgentStream('t1', 'hi', {
      onEvent: (e) => {
        events.push(e)
      },
    })

    await runId
    await vi.waitFor(() => expect(events).toHaveLength(2), { timeout: 2000 })

    cancel()

    expect(events[0]!.event_type).toBe('run.started')
    expect(events[1]!.event_type).toBe('model.started')
  })

  it('cancel 调用终止连接', () => {
    realFetch.mockResolvedValue({
      status: 200,
      headers: new Map([['X-Run-ID', 'r1']]),
      ok: true,
      body: {
        getReader() {
          return {
            async read() {
              return { done: true, value: new Uint8Array() }
            },
            releaseLock() {},
          }
        },
      },
    })

    const { cancel } = connectAgentStream('t1', 'hi', {})

    // 不应抛出
    expect(() => cancel()).not.toThrow()
  })
})
