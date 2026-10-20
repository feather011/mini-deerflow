/**
 * useConfirmations hook 测试 — TOOL-05 round 2
 *
 * 验证：
 * - 切 thread 清空 + generation 防旧响应污染
 * - 慢响应到达新会话 → 不写入
 * - 双重击不重复发送
 * - run 结束（activeRunId=null）也拉列表核对终态
 * - 真实 activeRunId 传给卡片
 * - 错误 → online=false；不重放
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { useConfirmations } from './useConfirmations'
// 提前 import 以便 beforeEach 中调用 mockImplementation
import {
  approveConfirmation,
  cancelConfirmation,
  denyConfirmation,
  executeSnapshot,
  getActiveRun,
  listConfirmations,
} from '../../shared/api/confirmations'

const POLL_INTERVAL_MS = 2000

let activeRun: string | null = 'r-1'
let confirmations: Record<string, unknown> = {}

async function listSpyImpl(params: { thread_id: string; run_id?: string }) {
  if (activeRun === null) {
    return Object.values(confirmations).filter(
      (c) =>
        typeof c === 'object' && c !== null &&
        (c as { thread_id: string }).thread_id === params.thread_id,
    )
  }
  return Object.values(confirmations).filter(
    (c) =>
      typeof c === 'object' && c !== null &&
      (c as { thread_id: string; run_id: string }).thread_id === params.thread_id &&
      (c as { run_id: string }).run_id === params.run_id,
  )
}
async function probeSpyImpl() {
  return { run_id: activeRun }
}
async function approveSpyImpl(id: string) {
  const c = confirmations[id] as Record<string, unknown>
  confirmations[id] = { ...c, state: 'approved', decided_at: Date.now() / 1000 }
  return confirmations[id]
}
async function denySpyImpl(id: string) {
  const c = confirmations[id] as Record<string, unknown>
  confirmations[id] = { ...c, state: 'denied', decided_at: Date.now() / 1000 }
  return confirmations[id]
}
async function cancelSpyImpl(id: string) {
  const c = confirmations[id] as Record<string, unknown>
  confirmations[id] = { ...c, state: 'cancelled', decided_at: Date.now() / 1000 }
  return confirmations[id]
}
async function executeSpyImpl(id: string) {
  const c = confirmations[id] as Record<string, unknown>
  confirmations[id] = {
    ...c,
    state: 'executed',
    execution_result: 'ok',
    executed_at: Date.now() / 1000,
  }
  return confirmations[id]
}

vi.mock('../../shared/api/confirmations', () => ({
  listConfirmations: vi.fn(listSpyImpl),
  getActiveRun: vi.fn(probeSpyImpl),
  approveConfirmation: vi.fn(approveSpyImpl),
  denyConfirmation: vi.fn(denySpyImpl),
  cancelConfirmation: vi.fn(cancelSpyImpl),
  executeSnapshot: vi.fn(executeSpyImpl),
}))

function makeConfirmation(over: Record<string, unknown> = {}) {
  return {
    request_id: 'cf-1',
    run_id: 'r-1',
    thread_id: 't-1',
    project_id: null,
    principal: 'MAIN_AGENT',
    tool_name: 'mcp__echo__echo',
    arguments_hash: 'h1',
    arguments_display: { m: 'x' },
    requested_at: Date.now() / 1000,
    expires_at: Date.now() / 1000 + 300,
    state: 'pending',
    decided_at: null,
    decided_by: null,
    executed_at: null,
    execution_result: null,
    ...over,
  }
}

beforeEach(() => {
  activeRun = 'r-1'
  confirmations = { 'cf-1': makeConfirmation() }
  // 重置每个 mock 的实现（vi.clearAllMocks 只清调用记录，不清实现）
  ;(approveConfirmation as unknown as { mockImplementation: (i: typeof approveSpyImpl) => void }).mockImplementation(approveSpyImpl)
  ;(denyConfirmation as unknown as { mockImplementation: (i: typeof denySpyImpl) => void }).mockImplementation(denySpyImpl)
  ;(cancelConfirmation as unknown as { mockImplementation: (i: typeof cancelSpyImpl) => void }).mockImplementation(cancelSpyImpl)
  ;(executeSnapshot as unknown as { mockImplementation: (i: typeof executeSpyImpl) => void }).mockImplementation(executeSpyImpl)
  ;(listConfirmations as unknown as { mockImplementation: (i: typeof listSpyImpl) => void }).mockImplementation(listSpyImpl)
  ;(getActiveRun as unknown as { mockImplementation: (i: typeof probeSpyImpl) => void }).mockImplementation(probeSpyImpl)
})

afterEach(() => {
  // vi.clearAllMocks 只清调用记录，impl 仍存；mockImplementationOnce 不需要清
  vi.clearAllMocks()
})

describe('useConfirmations', () => {
  it('无 threadId 时不启动轮询', () => {
    const { result } = renderHook(() => useConfirmations(null))
    expect(result.current.items).toEqual({})
  })

  it('有 active run 时拉取列表 + 真实 activeRunId', async () => {
    const { result } = renderHook(() => useConfirmations('t-1'))
    await waitFor(() => {
      expect(result.current.items['cf-1']).toBeDefined()
    })
    expect(result.current.byRunId['r-1']).toHaveLength(1)
    expect(result.current.activeRunId).toBe('r-1')
  })

  it('切 thread 清空缓存', async () => {
    const { result, rerender } = renderHook(
      ({ tid }) => useConfirmations(tid),
      { initialProps: { tid: 't-1' } },
    )
    await waitFor(() => {
      expect(result.current.items['cf-1']).toBeDefined()
    })
    rerender({ tid: 't-2' })
    await waitFor(() => {
      expect(Object.keys(result.current.items)).toEqual([])
    })
  })

  it('run 结束（activeRunId=null）也拉列表核对终态', async () => {
    const { result } = renderHook(() => useConfirmations('t-1'))
    await waitFor(() => {
      expect(result.current.items['cf-1']).toBeDefined()
    })
    // run 结束
    activeRun = null
    // 等轮询 + 列表
    await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS + 100))
    expect(result.current.activeRunId).toBeNull()
    // 旧记录保留（可显示终态）
    expect(result.current.items['cf-1']).toBeDefined()
  })

  it('慢响应到达新会话：旧 generation 的 setItems 被丢弃', async () => {
    const { listConfirmations } = await import('../../shared/api/confirmations')
    const realList = listConfirmations as unknown as { getMockImplementation: () => ((p: { thread_id: string; run_id?: string }) => Promise<unknown[]>) | undefined; mockImplementation: (impl: unknown) => void }
    const origImpl = realList.getMockImplementation()
    realList.mockImplementation(
      (params: { thread_id: string; run_id?: string }) =>
        new Promise((resolve) =>
          setTimeout(() => {
            if (origImpl) resolve(origImpl(params))
            else resolve([])
          }, 50),
        ),
    )

    const { result, rerender } = renderHook(
      ({ tid }) => useConfirmations(tid),
      { initialProps: { tid: 't-1' } },
    )
    rerender({ tid: 't-2' })
    // 等慢响应回来
    await new Promise((resolve) => setTimeout(resolve, 200))
    // t-2 加载自己的列表（无 t-1 记录）
    expect(Object.values(result.current.items).map((c) => c.request_id)).not.toContain('cf-1')

    // 还原
    realList.mockImplementation(origImpl ?? (() => Promise.resolve([])))
  })

  it('切会话期间的 approve 响应不写入新会话（世代保护）', async () => {
    const { result, rerender } = renderHook(
      ({ tid }) => useConfirmations(tid),
      { initialProps: { tid: 't-1' } },
    )
    await waitFor(() => {
      expect(result.current.items['cf-1']).toBeDefined()
    })

    // approve 慢响应（100ms 后返回 approved）
    // TS 断言：赋值发生在回调内，闭包外不被控制流窄化为 never
    let slowResolve!: (v: unknown) => void
    const approveSpy = approveConfirmation as unknown as {
      mockImplementation: (i: (id: string) => Promise<unknown>) => void
      mock: { calls: unknown[][] }
    }
    approveSpy.mockImplementation(
      () =>
        new Promise((resolve) => {
          slowResolve = resolve as (v: unknown) => void
        }),
    )

    // 发起操作（不等待）
    const pendingOp = result.current.approve('cf-1')
    await new Promise((r) => setTimeout(r, 20))

    // 切到 t-2（世代递增）
    rerender({ tid: 't-2' })
    await waitFor(() => {
      expect(Object.keys(result.current.items)).toEqual([])
    })

    // 旧世代的 approve 响应此时到达
    slowResolve({
      request_id: 'cf-1',
      run_id: 'r-1',
      thread_id: 't-1',
      project_id: null,
      principal: 'MAIN_AGENT',
      tool_name: 'mcp__echo__echo',
      arguments_hash: 'h1',
      arguments_display: { m: 'x' },
      requested_at: 1,
      expires_at: 9999999999,
      state: 'approved',
      decided_at: 2,
      decided_by: 'user',
      executed_at: null,
      execution_result: null,
    })
    await pendingOp
    await new Promise((r) => setTimeout(r, 50))

    // 关键：旧响应没有写入新会话
    expect(result.current.items['cf-1']).toBeUndefined()
    expect(Object.keys(result.current.items)).toEqual([])
  })

  it('双击同 request 第二次被守卫吞掉', async () => {
    const { result } = renderHook(() => useConfirmations('t-1'))
    await waitFor(() => {
      expect(result.current.items['cf-1']).toBeDefined()
    })

    const { approveConfirmation } = await import('../../shared/api/confirmations')
    const approveSpy = approveConfirmation as unknown as { mockImplementation: (impl: () => Promise<unknown>) => void; mock: { calls: unknown[][] } }

    // 第一次返回慢 promise（不 resolve）；实现看请求次数
    let firstStarted = false
    approveSpy.mockImplementation(
      () =>
        new Promise<unknown>(() => {
          firstStarted = true
        }),
    )

    // 触发第一次（不 await）
    void result.current.approveAndExecute('cf-1')
    // 给点时间让第一个 promise 真正进入 spy
    await new Promise((r) => setTimeout(r, 10))
    expect(firstStarted).toBe(true)
    expect(approveSpy.mock.calls.length).toBe(1)

    // 立即触发第二次（inFlight 守卫直接返回 unknown）
    const p2 = result.current.approveAndExecute('cf-1')
    const result2 = await p2
    expect(result2.kind).toBe('unknown')
    // approve 只被调一次（inFlight 守卫）
    expect(approveSpy.mock.calls.length).toBe(1)
  })

  it('approve 后状态变 approved', async () => {
    const { result } = renderHook(() => useConfirmations('t-1'))
    await waitFor(() => {
      expect(result.current.items['cf-1']).toBeDefined()
    })
    await act(async () => {
      await result.current.approve('cf-1')
    })
    const c = result.current.items['cf-1']
    expect(c).toBeDefined()
    expect(c!.state).toBe('approved')
  })

  it('deny 后状态变 denied', async () => {
    const { result } = renderHook(() => useConfirmations('t-1'))
    await waitFor(() => {
      expect(result.current.items['cf-1']).toBeDefined()
    })
    await act(async () => {
      await result.current.deny('cf-1')
    })
    const c = result.current.items['cf-1']
    expect(c).toBeDefined()
    expect(c!.state).toBe('denied')
  })

  it('approveAndExecute 走两步', async () => {
    const { result } = renderHook(() => useConfirmations('t-1'))
    await waitFor(() => {
      expect(result.current.items['cf-1']).toBeDefined()
    })
    await act(async () => {
      await result.current.approveAndExecute('cf-1')
    })
    const c = result.current.items['cf-1']
    expect(c).toBeDefined()
    expect(c!.state).toBe('executed')
    expect(c!.execution_result).toBe('ok')
  })

  it('网络错误时 online=false 但旧数据保留', async () => {
    const { result } = renderHook(() => useConfirmations('t-1'))
    await waitFor(() => {
      expect(result.current.items['cf-1']).toBeDefined()
    })
    const { getActiveRun } = await import('../../shared/api/confirmations')
    const probe = getActiveRun as unknown as ReturnType<typeof vi.fn>
    probe.mockRejectedValueOnce(new Error('network'))
    await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS + 100))
    expect(result.current.online).toBe(false)
    expect(result.current.items['cf-1']).toBeDefined()
  })
})
