/**
 * useAgentStream Hook 测试（20D）
 *
 * 覆盖 reducer 状态机和缓存失效逻辑
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useAgentStream } from './useAgentStream'
import type { ReactNode } from 'react'
import type { RunEvent } from '../../shared/types/events'

// Mock streamClient
const mockConnectAgentStream = vi.hoisted(() => vi.fn())

vi.mock('../../shared/api/streamClient', () => ({
  connectAgentStream: mockConnectAgentStream,
}))

describe('useAgentStream', () => {
  let queryClient: QueryClient

  beforeEach(() => {
    vi.clearAllMocks()
    queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    // 默认 mock 行为：返回空闲 stream
    mockConnectAgentStream.mockReturnValue({
      runId: Promise.resolve('run-1'),
      cancel: vi.fn(),
    })
  })

  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )

  it('初始状态为 idle', () => {
    const { result } = renderHook(() => useAgentStream({ threadId: 't1', message: 'hi' }), { wrapper })
    expect(result.current.status).toBe('idle')
    expect(result.current.events).toEqual([])
    expect(result.current.runId).toBeNull()
  })

  it('start 后建立 SSE 连接', () => {
    const { result } = renderHook(() => useAgentStream({ threadId: 't1', message: 'hi' }), { wrapper })
    act(() => result.current.start())
    expect(mockConnectAgentStream).toHaveBeenCalledWith(
      't1',
      'hi',
      expect.objectContaining({ onEvent: expect.any(Function), onTerminal: expect.any(Function) }),
    )
  })

  it('stop 调用 cancel 函数', () => {
    const mockCancel = vi.fn()
    mockConnectAgentStream.mockReturnValue({
      runId: Promise.resolve('run-1'),
      cancel: mockCancel,
    })

    const { result } = renderHook(() => useAgentStream({ threadId: 't1', message: 'hi' }), { wrapper })

    act(() => result.current.start())
    act(() => result.current.stop())

    expect(mockCancel).toHaveBeenCalled()
    expect(result.current.status).toBe('stopped')
  })

  it('收到 onEvent 时更新 events 列表', async () => {
    let capturedOnEvent: ((event: RunEvent) => boolean | void) | null = null

    mockConnectAgentStream.mockImplementation((_threadId, _message, opts) => {
      capturedOnEvent = opts.onEvent ?? null
      return { runId: Promise.resolve('run-1'), cancel: vi.fn() }
    })

    const { result } = renderHook(() => useAgentStream({ threadId: 't1', message: 'hi' }), { wrapper })
    act(() => result.current.start())

    // 触发事件
    act(() => {
      capturedOnEvent!({
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
      } as RunEvent)
    })

    expect(result.current.events).toHaveLength(1)
    expect(result.current.status).toBe('streaming')
  })

  it('收到 onTerminal 后切换到 completed 状态', async () => {
    let capturedOnTerminal: ((event: RunEvent) => void) | null = null

    mockConnectAgentStream.mockImplementation((_threadId, _message, opts) => {
      capturedOnTerminal = opts.onTerminal ?? null
      return { runId: Promise.resolve('run-1'), cancel: vi.fn() }
    })

    const { result } = renderHook(() => useAgentStream({ threadId: 't1', message: 'hi' }), { wrapper })
    act(() => result.current.start())

    act(() => {
      capturedOnTerminal!({
        run_id: 'run-1',
        thread_id: 't1',
        event_type: 'run.completed',
        sequence: 1,
        timestamp: 1010,
        answer: 'OK',
        error: null,
        attempt: null,
        operation: null,
        tool_name: null,
        duration_seconds: 10,
      } as RunEvent)
    })

    expect(result.current.status).toBe('completed')
    expect(result.current.terminalEvent?.answer).toBe('OK')
  })

  it('收到 onTerminal 后失效当前 Thread 详情', async () => {
    let capturedOnTerminal: ((event: RunEvent) => void) | null = null

    mockConnectAgentStream.mockImplementation((_threadId, _message, opts) => {
      capturedOnTerminal = opts.onTerminal ?? null
      return { runId: Promise.resolve('run-1'), cancel: vi.fn() }
    })

    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries')

    const { result } = renderHook(() => useAgentStream({ threadId: 't1', message: 'hi' }), { wrapper })
    act(() => result.current.start())

    act(() => {
      capturedOnTerminal!({
        run_id: 'run-1',
        thread_id: 't1',
        event_type: 'run.completed',
        sequence: 1,
        timestamp: 1010,
        answer: 'OK',
        error: null,
        attempt: null,
        operation: null,
        tool_name: null,
        duration_seconds: 10,
      } as RunEvent)
    })

    expect(invalidateSpy).toHaveBeenCalledWith(
      expect.objectContaining({
        queryKey: expect.arrayContaining(['threads', 'detail', 't1']),
      }),
    )
  })

  it('收到 onError 时切换到 error 状态', () => {
    let capturedOnError: ((error: Error) => void) | null = null

    mockConnectAgentStream.mockImplementation((_threadId, _message, opts) => {
      capturedOnError = opts.onError ?? null
      return { runId: Promise.resolve('run-1'), cancel: vi.fn() }
    })

    const { result } = renderHook(() => useAgentStream({ threadId: 't1', message: 'hi' }), { wrapper })
    act(() => result.current.start())

    act(() => {
      capturedOnError!(new Error('连接失败'))
    })

    expect(result.current.status).toBe('error')
    expect(result.current.errorMessage).toBe('连接失败')
  })

  it('run.completed 外的三种终态对应正确 status', async () => {
    const terminalCases: Array<{ type: RunEvent['event_type']; expected: string }> = [
      { type: 'run.failed', expected: 'failed' },
      { type: 'run.timed_out', expected: 'timed_out' },
      { type: 'run.cancelled', expected: 'cancelled' },
    ]

    for (const { type, expected } of terminalCases) {
      let captured: ((event: RunEvent) => void) | null = null

      mockConnectAgentStream.mockImplementation((_tid, _msg, opts) => {
        captured = opts.onTerminal ?? null
        return { runId: Promise.resolve('r'), cancel: vi.fn() }
      })

      const { result } = renderHook(() => useAgentStream({ threadId: 't1', message: 'hi' }), { wrapper })
      act(() => result.current.start())

      act(() => {
        captured!({
          run_id: 'r',
          thread_id: 't1',
          event_type: type,
          sequence: 0,
          timestamp: 1000,
          answer: null,
          error: 'Error',
          attempt: null,
          operation: null,
          tool_name: null,
          duration_seconds: null,
        } as RunEvent)
      })

      expect(result.current.status).toBe(expected)
    }
  })
})
