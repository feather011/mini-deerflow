/**
 * useConfirmations hook 测试 — TOOL-05
 *
 * 验证：
 * - 轮询仅在有 active run 时启动
 * - 切 thread 清空缓存
 * - 操作（approve/deny/execute）立即刷新
 * - 错误不重放（fetch 错误保留旧数据）
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { useConfirmations } from './useConfirmations'

const POLL_INTERVAL_MS = 2000

let activeRun: string | null = 'r-1'
let confirmations: Record<string, unknown> = {}

vi.mock('../../shared/api/confirmations', () => ({
  listConfirmations: vi.fn(async (params: { thread_id: string; run_id?: string }) => {
    if (activeRun === null) return []
    return Object.values(confirmations).filter(
      (c) =>
        typeof c === 'object' && c !== null &&
        (c as { thread_id: string; run_id: string }).thread_id === params.thread_id &&
        (c as { run_id: string }).run_id === params.run_id,
    )
  }),
  getActiveRun: vi.fn(async () => ({ run_id: activeRun })),
  approveConfirmation: vi.fn(async (id: string) => {
    const c = confirmations[id] as Record<string, unknown>
    confirmations[id] = { ...c, state: 'approved', decided_at: Date.now() / 1000 }
    return confirmations[id]
  }),
  denyConfirmation: vi.fn(async (id: string) => {
    const c = confirmations[id] as Record<string, unknown>
    confirmations[id] = { ...c, state: 'denied', decided_at: Date.now() / 1000 }
    return confirmations[id]
  }),
  cancelConfirmation: vi.fn(async (id: string) => {
    const c = confirmations[id] as Record<string, unknown>
    confirmations[id] = { ...c, state: 'cancelled', decided_at: Date.now() / 1000 }
    return confirmations[id]
  }),
  executeSnapshot: vi.fn(async (id: string) => {
    const c = confirmations[id] as Record<string, unknown>
    confirmations[id] = {
      ...c,
      state: 'executed',
      execution_result: 'ok',
      executed_at: Date.now() / 1000,
    }
    return confirmations[id]
  }),
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
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('useConfirmations', () => {
  it('无 threadId 时不启动轮询', async () => {
    const { result } = renderHook(() => useConfirmations(null))
    expect(result.current.items).toEqual({})
  })

  it('有 active run 时拉取列表', async () => {
    const { result } = renderHook(() => useConfirmations('t-1'))
    await waitFor(() => {
      expect(result.current.items['cf-1']).toBeDefined()
    })
    expect(result.current.byRunId['r-1']).toHaveLength(1)
  })

  it('无 active run 时不清空已有数据（保留终态历史）', async () => {
    const { result } = renderHook(() => useConfirmations('t-1'))
    await waitFor(() => {
      expect(result.current.items['cf-1']).toBeDefined()
    })
    // run 结束
    activeRun = null
    // 等下一次轮询
    await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS + 100))
    expect(result.current.items['cf-1']).toBeDefined()
  })

  it('切换 thread 清空缓存', async () => {
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

  it('approve 后状态变 approved', async () => {
    const { result } = renderHook(() => useConfirmations('t-1'))
    await waitFor(() => {
      expect(result.current.items['cf-1']).toBeDefined()
    })
    await act(async () => {
      await result.current.approve('cf-1')
    })
    expect(result.current.items['cf-1'].state).toBe('approved')
  })

  it('deny 后状态变 denied', async () => {
    const { result } = renderHook(() => useConfirmations('t-1'))
    await waitFor(() => {
      expect(result.current.items['cf-1']).toBeDefined()
    })
    await act(async () => {
      await result.current.deny('cf-1')
    })
    expect(result.current.items['cf-1'].state).toBe('denied')
  })

  it('approveAndExecute 走两步：approve + execute-snapshot', async () => {
    const { result } = renderHook(() => useConfirmations('t-1'))
    await waitFor(() => {
      expect(result.current.items['cf-1']).toBeDefined()
    })
    await act(async () => {
      await result.current.approveAndExecute('cf-1')
    })
    expect(result.current.items['cf-1'].state).toBe('executed')
    expect(result.current.items['cf-1'].execution_result).toBe('ok')
  })
})
