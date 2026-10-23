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
import { ApiError } from '../../shared/api/client'

// Mock streamClient
const mockConnectAgentStream = vi.hoisted(() => vi.fn())

vi.mock('../../shared/api/streamClient', () => ({
  connectAgentStream: mockConnectAgentStream,
}))

// Mock 后端 stop 接口（真取消）
const mockStopAgentRun = vi.hoisted(() => vi.fn())

vi.mock('./runsApi', () => ({
  stopAgentRun: mockStopAgentRun,
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
    mockStopAgentRun.mockResolvedValue({ status: 'stopped', thread_id: 't1' })
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

  // -------------------------------------------------------------------------
  // R7：真取消（stop → 后端取消 → run.cancelled）
  // -------------------------------------------------------------------------

  it('cancelRun 调用后端 stop 接口（绑定当前 thread）', async () => {
    const { result } = renderHook(() => useAgentStream({ threadId: 't1', message: 'hi' }), { wrapper })
    act(() => result.current.start())

    await act(async () => {
      await result.current.cancelRun()
    })

    expect(mockStopAgentRun).toHaveBeenCalledWith('t1')
    expect(result.current.cancelState).toBe('cancelling')
  })

  it('cancelRun 防重复：连点只发一次请求', async () => {
    const { result } = renderHook(() => useAgentStream({ threadId: 't1', message: 'hi' }), { wrapper })
    act(() => result.current.start())

    await act(async () => {
      await Promise.all([result.current.cancelRun(), result.current.cancelRun()])
      await result.current.cancelRun()
    })

    expect(mockStopAgentRun).toHaveBeenCalledTimes(1)
  })

  it('取消成功后收到 run.cancelled 终态 → cancelState=cancelled', async () => {
    let captured: ((event: RunEvent) => void) | null = null
    mockConnectAgentStream.mockImplementation((_tid, _msg, opts) => {
      captured = opts.onTerminal ?? null
      return { runId: Promise.resolve('r'), cancel: vi.fn() }
    })

    const { result } = renderHook(() => useAgentStream({ threadId: 't1', message: 'hi' }), { wrapper })
    act(() => result.current.start())
    await act(async () => {
      await result.current.cancelRun()
    })

    act(() => {
      captured!({
        run_id: 'r',
        thread_id: 't1',
        event_type: 'run.cancelled',
        sequence: 1,
        timestamp: 1000,
        answer: null,
        error: null,
        attempt: null,
        operation: null,
        tool_name: null,
        duration_seconds: null,
      } as RunEvent)
    })

    expect(result.current.status).toBe('cancelled')
    expect(result.current.cancelState).toBe('cancelled')
    expect(result.current.cancelError).toBeNull()
  })

  it('取消失败（后端无正在运行的 Run）：如实报 failed，且不断流谎称已停止', async () => {
    const mockCancel = vi.fn()
    mockConnectAgentStream.mockReturnValue({
      runId: Promise.resolve('run-1'),
      cancel: mockCancel,
    })
    mockStopAgentRun.mockRejectedValue(
      new ApiError({
        status: 404,
        code: 'NOT_FOUND',
        message: '请求失败（404）',
      }),
    )

    const { result } = renderHook(() => useAgentStream({ threadId: 't1', message: 'hi' }), { wrapper })
    act(() => result.current.start())
    const abortsBefore = mockCancel.mock.calls.length

    await act(async () => {
      await result.current.cancelRun()
    })

    expect(result.current.cancelState).toBe('failed')
    expect(result.current.cancelError).toContain('没有正在运行的 Run')
    // 关键：不能把"仅断流"当成"任务已停止" —— 取消失败不得 abort 流
    expect(mockCancel.mock.calls.length).toBe(abortsBefore)
    expect(result.current.status).not.toBe('cancelled')
    expect(result.current.status).not.toBe('stopped')
  })

  it('取消失败后允许重试', async () => {
    mockStopAgentRun.mockRejectedValueOnce(
      new ApiError({ status: 0, code: 'NETWORK_ERROR', message: '无法连接到后端服务' }),
    )

    const { result } = renderHook(() => useAgentStream({ threadId: 't1', message: 'hi' }), { wrapper })
    act(() => result.current.start())

    await act(async () => {
      await result.current.cancelRun()
    })
    expect(result.current.cancelState).toBe('failed')

    await act(async () => {
      await result.current.cancelRun()
    })
    expect(mockStopAgentRun).toHaveBeenCalledTimes(2)
    expect(result.current.cancelState).toBe('cancelling')
  })

  it('stop（仅断流）不调用后端 stop 接口', () => {
    const { result } = renderHook(() => useAgentStream({ threadId: 't1', message: 'hi' }), { wrapper })
    act(() => result.current.start())
    act(() => result.current.stop())

    expect(mockStopAgentRun).not.toHaveBeenCalled()
    expect(result.current.status).toBe('stopped')
  })

  // -------------------------------------------------------------------------
  // 根 StrictMode：开发模式会重放 effect（setup → cleanup → setup）。
  // 若"已挂载"标记只在初始化设置、cleanup 不恢复 setup 也恢复，
  // 重放后组件会被永久当成已卸载 —— 取消中状态与取消失败提示再也更新不出来。
  // -------------------------------------------------------------------------

  describe('根 StrictMode', () => {
    const strictWrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    )
    const strictOptions = { wrapper: strictWrapper, reactStrictMode: true } as const

    it('取消请求被受理后进入 cancelling（按钮禁用依据）', async () => {
      const { result } = renderHook(
        () => useAgentStream({ threadId: 't1', message: '', autoStart: false }),
        strictOptions,
      )
      act(() => result.current.start('fixture task'))

      await act(async () => {
        await result.current.cancelRun()
      })

      expect(mockStopAgentRun).toHaveBeenCalledWith('t1')
      expect(result.current.cancelState).toBe('cancelling')
      expect(result.current.cancelError).toBeNull()
    })

    it('收到后端 run.cancelled 终态后进入 cancelled', async () => {
      let captured: ((event: RunEvent) => void) | null = null
      mockConnectAgentStream.mockImplementation((_tid, _msg, opts) => {
        captured = opts.onTerminal ?? null
        return { runId: Promise.resolve('r'), cancel: vi.fn() }
      })

      const { result } = renderHook(
        () => useAgentStream({ threadId: 't1', message: '', autoStart: false }),
        strictOptions,
      )
      act(() => result.current.start('fixture task'))
      await act(async () => {
        await result.current.cancelRun()
      })

      act(() => {
        captured!({
          run_id: 'r',
          thread_id: 't1',
          event_type: 'run.cancelled',
          sequence: 1,
          timestamp: 1000,
          answer: null,
          error: null,
          attempt: null,
          operation: null,
          tool_name: null,
          duration_seconds: null,
        } as RunEvent)
      })

      expect(result.current.status).toBe('cancelled')
      expect(result.current.cancelState).toBe('cancelled')
      expect(result.current.cancelError).toBeNull()
    })

    it('后端无正在运行的 Run（404）时失败提示可见', async () => {
      mockStopAgentRun.mockRejectedValue(
        new ApiError({ status: 404, code: 'NOT_FOUND', message: '请求失败（404）' }),
      )

      const { result } = renderHook(
        () => useAgentStream({ threadId: 't1', message: '', autoStart: false }),
        strictOptions,
      )
      act(() => result.current.start('fixture task'))

      await act(async () => {
        await result.current.cancelRun()
      })

      expect(result.current.cancelState).toBe('failed')
      expect(result.current.cancelError).toContain('没有正在运行的 Run')
    })

    it('网络失败后失败提示可见且允许重试', async () => {
      mockStopAgentRun.mockRejectedValueOnce(
        new ApiError({ status: 0, code: 'NETWORK_ERROR', message: '无法连接到后端服务' }),
      )

      const { result } = renderHook(
        () => useAgentStream({ threadId: 't1', message: '', autoStart: false }),
        strictOptions,
      )
      act(() => result.current.start('fixture task'))

      await act(async () => {
        await result.current.cancelRun()
      })
      expect(result.current.cancelState).toBe('failed')
      expect(result.current.cancelError).toContain('无法连接到后端服务')

      await act(async () => {
        await result.current.cancelRun()
      })
      expect(mockStopAgentRun).toHaveBeenCalledTimes(2)
      expect(result.current.cancelState).toBe('cancelling')
    })

    it('真实卸载仍只断流、不取消后端任务', async () => {
      const mockCancel = vi.fn()
      mockConnectAgentStream.mockReturnValue({
        runId: Promise.resolve('run-1'),
        cancel: mockCancel,
      })

      const { result, unmount } = renderHook(
        () => useAgentStream({ threadId: 't1', message: '', autoStart: false }),
        strictOptions,
      )
      act(() => result.current.start('fixture task'))

      unmount()

      expect(mockCancel).toHaveBeenCalled()
      expect(mockStopAgentRun).not.toHaveBeenCalled()
    })
  })
})
