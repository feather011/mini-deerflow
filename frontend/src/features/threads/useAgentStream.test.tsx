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
    // autoStart=false：本用例断言的是「还没发任何消息时」的初始状态。
    // 开启 autoStart 时 message 非空会立刻建流，状态本就该是 connecting
    // —— 那是本轮修复要求的行为（不等待响应头），不是初始态。
    const { result } = renderHook(
      () => useAgentStream({ threadId: 't1', message: 'hi', autoStart: false }),
      { wrapper },
    )
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

  // -------------------------------------------------------------------------
  // 人工验收返修 · 第 2 项：取消后重试必须按**每次运行**重新初始化状态
  //
  // 复现的缺陷（截图可见）：已取消 → 再次发送 → 界面仍停留在上一轮的
  // 「已取消」，直到后端**响应头**回来才切换。根因有三处：
  //  1. `CONNECTING` 只在 `runId.then(...)` 里 dispatch，即等响应头；
  //  2. `CONNECTING` 不清 events / terminalEvent，上一轮的事件继续留着；
  //  3. 没有「运行世代」标记，旧连接迟到的 onEvent/onTerminal/onError
  //     会覆盖新一轮的状态。
  // -------------------------------------------------------------------------

  describe('取消后重试：按每次运行重新初始化', () => {
    interface CapturedConn {
      onEvent: (event: RunEvent) => boolean | void
      onTerminal: (event: RunEvent) => void
      onError: (error: Error) => void
    }

    /** 构造一条完整 RunEvent（字段与后端 streaming.py 对齐） */
    function ev(
      eventType: RunEvent['event_type'],
      sequence: number,
      extra: Partial<RunEvent> = {},
    ): RunEvent {
      return {
        run_id: 'run-x',
        thread_id: 't1',
        event_type: eventType,
        sequence,
        timestamp: 1000 + sequence,
        answer: null,
        error: null,
        attempt: null,
        operation: null,
        tool_name: null,
        duration_seconds: null,
        ...extra,
      }
    }

    /**
     * 每次 start() 抓一组回调单独持有，用来精确模拟
     * 「旧连接的迟到事件」与「新连接的事件」。
     *
     * runId 承诺**永不兑现** —— 等价于后端迟迟不回响应头。
     */
    function setupConnections() {
      const conns: CapturedConn[] = []
      mockConnectAgentStream.mockImplementation(
        (_threadId: string, _message: string, opts: CapturedConn) => {
          conns.push({
            onEvent: opts.onEvent,
            onTerminal: opts.onTerminal,
            onError: opts.onError,
          })
          return { runId: new Promise<string>(() => {}), cancel: vi.fn() }
        },
      )
      return conns
    }

    /** 取第 n 次 start() 建立的连接；缺索引时立刻报错而不是静默 undefined */
    function connAt(all: CapturedConn[], index: number): CapturedConn {
      const c = all[index]
      if (!c) throw new Error(`第 ${index + 1} 次 start() 没有建立连接`)
      return c
    }

    const renderStream = () =>
      renderHook(
        () => useAgentStream({ threadId: 't1', message: '', autoStart: false }),
        { wrapper },
      )

    it('点击发送立即进入 connecting，不等待响应头', () => {
      setupConnections()
      const { result } = renderStream()
      expect(result.current.status).toBe('idle')

      // runId 永不兑现（后端故意延迟响应头）
      act(() => result.current.start('第二个问题'))

      expect(result.current.status).toBe('connecting')
    })

    it('新一轮清空上一轮的事件、终态、累积文本与错误', () => {
      const conns = setupConnections()
      const { result } = renderStream()

      act(() => result.current.start('第一个问题'))
      act(() => void connAt(conns, 0).onEvent(ev('run.started', 0)))
      act(() => void connAt(conns, 0).onEvent(ev('model.token', 1, { delta: '上一轮的回答' })))
      act(() => void connAt(conns, 0).onTerminal(ev('run.failed', 2)))

      expect(result.current.status).toBe('failed')
      expect(result.current.events.length).toBeGreaterThan(0)
      expect(result.current.currentText).toBe('')

      act(() => result.current.start('第二个问题'))

      expect(result.current.status).toBe('connecting')
      expect(result.current.events).toEqual([])
      expect(result.current.terminalEvent).toBeNull()
      expect(result.current.errorMessage).toBeNull()
      expect(result.current.requestId).toBeNull()
      expect(result.current.currentText).toBe('')
    })

    it('新运行开始后，旧连接迟到的普通事件不得写入新运行', () => {
      const conns = setupConnections()
      const { result } = renderStream()

      act(() => result.current.start('第一个问题'))
      act(() => void connAt(conns, 0).onEvent(ev('run.started', 0)))

      act(() => result.current.start('第二个问题'))
      expect(result.current.events).toEqual([])

      // 旧连接此刻才收到本该在第一轮的事件
      act(() => void connAt(conns, 0).onEvent(ev('run.started', 0)))
      act(() => void connAt(conns, 0).onEvent(ev('model.token', 1, { delta: '旧的 token' })))

      expect(result.current.events).toEqual([])
      expect(result.current.status).toBe('connecting')
      expect(result.current.currentText).toBe('')
    })

    it('新运行开始后，旧连接迟到的取消终态不得覆盖新运行状态', () => {
      const conns = setupConnections()
      const { result } = renderStream()

      act(() => result.current.start('第一个问题'))
      act(() => void connAt(conns, 0).onEvent(ev('run.started', 0)))
      act(() => result.current.start('第二个问题'))

      // 第一轮迟到的 run.cancelled —— 新任务还在连接，不能被说成已取消
      act(() => void connAt(conns, 0).onTerminal(ev('run.cancelled', 9)))

      expect(result.current.status).toBe('connecting')
      expect(result.current.cancelState).toBe('idle')
      expect(result.current.terminalEvent).toBeNull()
    })

    it('新运行开始后，旧连接迟到的错误不得覆盖新运行状态', () => {
      const conns = setupConnections()
      const { result } = renderStream()

      act(() => result.current.start('第一个问题'))
      act(() => result.current.start('第二个问题'))

      act(() => void connAt(conns, 0).onError(new Error('旧连接断开了')))

      expect(result.current.status).toBe('connecting')
      expect(result.current.errorMessage).toBeNull()
    })

    it('取消 → 重新发送 → 连接 → 本轮完成（上一轮取消记录不得充当当前状态）', async () => {
      const conns = setupConnections()
      const { result } = renderStream()

      // 第一轮：发出去 → 取消 → 后端确认
      act(() => result.current.start('第一个问题'))
      await act(async () => {
        await result.current.cancelRun()
      })
      act(() => void connAt(conns, 0).onTerminal(ev('run.cancelled', 1)))
      expect(result.current.status).toBe('cancelled')
      expect(result.current.cancelState).toBe('cancelled')

      // 第二轮：立刻重新发送
      act(() => result.current.start('第二个问题'))
      expect(result.current.status).toBe('connecting')
      expect(result.current.cancelState).toBe('idle')

      // 第二轮：本轮自己的事件与终态正常生效
      act(() => void connAt(conns, 1).onEvent(ev('run.started', 0)))
      expect(result.current.status).toBe('streaming')
      act(() => void connAt(conns, 1).onTerminal(ev('run.completed', 1)))
      expect(result.current.status).toBe('completed')
      // 只有第二轮自己的两条事件，第一轮的事件没有残留
      expect(result.current.events.map((e) => e.event_type)).toEqual([
        'run.started',
        'run.completed',
      ])
    })

    // -----------------------------------------------------------------------
    // 复审收尾项：stop 请求的**迟到失败**必须被隔离
    //
    // 复现的缺陷（复审独立验证）：运行世代检查只保护了 SSE 回调
    // （onEvent / onTerminal / onError / runId.then），
    // 没保护 `cancelRun()` 里那条异步 HTTP 请求的 catch：
    //  1. A 已取消、B 已开始；A 的 stop 请求随后失败 → 把 **B** 的取消状态改成 failed；
    //  2. 已收到 A 的 run.cancelled，随后 stop HTTP 请求失败 → 把已确认取消改成取消失败。
    //
    // 「当前任务尚未结束、取消确实失败时显示错误」的正常分支由上面
    // 「取消失败（后端无正在运行的 Run）」与「取消失败后允许重试」两条覆盖，
    // 本组修复不得把它们一起关掉。
    // -----------------------------------------------------------------------
    describe('stop 请求的迟到失败必须被隔离', () => {
      /** 让 stop 请求悬而未决；返回「让它失败」的函数 */
      function pendingStop() {
        let rejectStop: (err: unknown) => void = () => {}
        mockStopAgentRun.mockImplementation(
          () =>
            new Promise((_res, rej) => {
              rejectStop = rej
            }),
        )
        return (err: unknown) => rejectStop(err)
      }

      const notFound = () =>
        new ApiError({ status: 404, code: 'NOT_FOUND', message: '请求失败（404）' })

      it('旧运行的 stop 请求迟到失败，不得改写新运行的取消状态', async () => {
        const conns = setupConnections()
        const failStop = pendingStop()

        const { result } = renderStream()

        // 第一轮：发出 stop，HTTP 请求悬而未决
        act(() => result.current.start('第一个问题'))
        act(() => {
          void result.current.cancelRun()
        })
        expect(result.current.cancelState).toBe('cancelling')

        // 第一轮被后端确认取消
        act(() => void connAt(conns, 0).onTerminal(ev('run.cancelled', 1)))
        expect(result.current.cancelState).toBe('cancelled')

        // 用户重新发送 → 第二轮开始，取消状态复位
        act(() => result.current.start('第二个问题'))
        expect(result.current.cancelState).toBe('idle')

        // 第一轮的 stop 请求此刻才失败（迟到）
        await act(async () => {
          failStop(notFound())
          await Promise.resolve()
        })

        // 第二轮仍在运行：不得被说成「取消失败」
        expect(result.current.cancelState).toBe('idle')
        expect(result.current.cancelError).toBeNull()
        expect(result.current.status).not.toBe('cancelled')
      })

      it('已收到后端取消终态后，迟到的 stop 失败不得把已确认取消改成取消失败', async () => {
        const conns = setupConnections()
        const failStop = pendingStop()

        const { result } = renderStream()

        act(() => result.current.start('第一个问题'))
        act(() => {
          void result.current.cancelRun()
        })
        expect(result.current.cancelState).toBe('cancelling')

        // 后端用 run.cancelled 终态确认了取消
        act(() => void connAt(conns, 0).onTerminal(ev('run.cancelled', 1)))
        expect(result.current.cancelState).toBe('cancelled')
        expect(result.current.status).toBe('cancelled')

        // stop 的 HTTP 请求此刻才失败 —— 不得覆盖已确认的取消事实
        await act(async () => {
          failStop(notFound())
          await Promise.resolve()
        })

        expect(result.current.cancelState).toBe('cancelled')
        expect(result.current.cancelError).toBeNull()
        expect(result.current.status).toBe('cancelled')
      })

      it('旧运行的 stop 迟到失败，不得解除新运行的「只发一次」守卫', async () => {
        // 本用例只关心 stop 请求的时序，不需要 SSE 回调
        setupConnections()
        let rejectStop: (err: unknown) => void = () => {}
        let call = 0
        mockStopAgentRun.mockImplementation(() => {
          call += 1
          if (call === 1) {
            return new Promise((_res, rej) => {
              rejectStop = rej
            })
          }
          return Promise.resolve({ status: 'stopped', thread_id: 't1' })
        })

        const { result } = renderStream()

        // 第一轮：stop 请求悬而未决
        act(() => result.current.start('第一个问题'))
        act(() => {
          void result.current.cancelRun()
        })

        // 第二轮开始
        act(() => result.current.start('第二个问题'))

        // 第二轮自己点停止 → 第 2 次请求
        act(() => {
          void result.current.cancelRun()
        })
        expect(mockStopAgentRun).toHaveBeenCalledTimes(2)

        // 第一轮的 stop 请求此刻才失败
        await act(async () => {
          rejectStop(notFound())
          await Promise.resolve()
        })

        // 守卫仍在：再点停止不得发出第 3 次请求
        act(() => {
          void result.current.cancelRun()
        })
        expect(mockStopAgentRun).toHaveBeenCalledTimes(2)
        expect(result.current.cancelState).toBe('cancelling')
      })
    })
  })
})
