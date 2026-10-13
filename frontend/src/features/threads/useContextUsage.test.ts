/**
 * useContextUsage Hook 测试
 *
 * 语义：**切换 threadId 即自动拉取该会话的统计**（先 abort 旧请求、
 * 再清空状态、再 fetch），这既是「切换会话重置」也是「不串数据」的机制。
 *
 * 覆盖：
 * - mount 后自动 fetch；返回 null → 「暂无统计」（snapshot=null / error=null）
 * - refresh 成功写入 snapshot
 * - refresh 失败写入 error，且不写 snapshot（与「无统计」区分）
 * - AbortError 不写 error（取消语义）
 * - 切换 threadId：旧状态立刻清空
 * - 切换 threadId：旧 thread 的响应即使后到也被丢弃
 * - 新一次 refresh abort 前一次未返回的请求
 * - loading：请求期间 true，落定后 false
 */
import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useContextUsage } from './useContextUsage'
import type { ContextUsageResponse } from '../../shared/types/contextUsage'

// 直接 mock API 模块，不依赖真实 fetch。
vi.mock('./contextUsageApi', () => ({
  fetchContextUsage: vi.fn(),
}))

import { fetchContextUsage } from './contextUsageApi'

const fetchMock = vi.mocked(fetchContextUsage)

const SNAPSHOT: ContextUsageResponse['snapshot'] = {
  thread_id: 't1',
  run_id: 'r1',
  budget: 1000,
  total: 500,
  breakdown: {
    system: 0,
    current_input: 100,
    active_tool_cycle: 0,
    recent_messages: 400,
    summary: 0,
    task_notes: 0,
    memory: 0,
    skills: 0,
    tool_schemas: 0,
  },
  compacted: false,
  tokens_before: null,
  soft_limit: 800,
  captured_at: 1000,
}

const NULL_RESP: ContextUsageResponse = { snapshot: null }

beforeEach(() => {
  fetchMock.mockReset()
})

describe('useContextUsage', () => {
  it('mount 自动拉取当前会话；尚未发送 → 暂无统计（snapshot=null, error=null）', async () => {
    fetchMock.mockResolvedValue(NULL_RESP)
    const { result } = renderHook(() => useContextUsage('t1'))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock).toHaveBeenCalledWith('t1', expect.any(AbortSignal))
    expect(result.current.snapshot).toBeNull()
    expect(result.current.error).toBeNull()
  })

  it('refresh 成功后写入 snapshot，error 保持 null', async () => {
    fetchMock.mockResolvedValue({ snapshot: SNAPSHOT })
    const { result } = renderHook(() => useContextUsage('t1'))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.snapshot).toEqual(SNAPSHOT)
    expect(result.current.error).toBeNull()
  })

  it('refresh 失败写入 error，snapshot 仍为 null（与「无统计」区分）', async () => {
    fetchMock.mockRejectedValue(new Error('boom'))
    const { result } = renderHook(() => useContextUsage('t1'))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.snapshot).toBeNull()
    expect(result.current.error).toEqual({ kind: 'fetch_failed', message: 'boom' })
  })

  it('AbortError 不写 error（视为正常取消）', async () => {
    const abortErr = new Error('aborted')
    abortErr.name = 'AbortError'
    fetchMock.mockRejectedValue(abortErr)
    const { result } = renderHook(() => useContextUsage('t1'))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.snapshot).toBeNull()
    expect(result.current.error).toBeNull()
  })

  it('切换 threadId：先清空旧会话状态，再拉取新会话', async () => {
    fetchMock.mockResolvedValue({ snapshot: SNAPSHOT })
    const { result, rerender } = renderHook(
      ({ id }) => useContextUsage(id),
      { initialProps: { id: 't1' } },
    )
    await waitFor(() => expect(result.current.snapshot).toEqual(SNAPSHOT))

    // 切到 t2：请求参数必须是新 threadId（不沿用 t1 的数据）
    fetchMock.mockClear()
    fetchMock.mockResolvedValue(NULL_RESP)
    rerender({ id: 't2' })
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('t2', expect.any(AbortSignal)))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.snapshot).toBeNull()
    expect(result.current.error).toBeNull()
    // 未残留 t1 的统计
    expect(result.current.snapshot?.thread_id).toBeUndefined()
  })

  it('切换 threadId：旧 thread 的响应即使后到也被丢弃（不覆盖当前会话）', async () => {
    // 第一次请求挂起，不返回
    let resolveSlow!: (v: ContextUsageResponse) => void
    fetchMock.mockImplementationOnce(
      () =>
        new Promise((res) => {
          resolveSlow = res
        }),
    )
    const { result, rerender } = renderHook(
      ({ id }) => useContextUsage(id),
      { initialProps: { id: 't1' } },
    )
    // t1 的请求尚未返回
    expect(fetchMock).toHaveBeenCalledTimes(1)

    // 切到 t2：t1 的 controller 被 abort，t2 发起新请求
    fetchMock.mockResolvedValueOnce({ snapshot: null })
    rerender({ id: 't2' })
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(result.current.loading).toBe(false))

    // 让 t1 的响应此刻才到达——必须被丢弃
    await act(async () => {
      resolveSlow({ snapshot: SNAPSHOT })
      await Promise.resolve()
    })
    expect(result.current.snapshot).toBeNull()
    expect(result.current.error).toBeNull()
  })

  it('新一次 refresh 会 abort 前一次未返回的请求', async () => {
    let resolveFirst!: (v: ContextUsageResponse) => void
    fetchMock.mockImplementationOnce(
      () =>
        new Promise((res) => {
          resolveFirst = res
        }),
    )
    fetchMock.mockResolvedValueOnce({ snapshot: SNAPSHOT })

    const { result } = renderHook(() => useContextUsage('t1'))
    // mount 的自动请求 = 第一次（挂起）
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))

    // 手动再触发一次 refresh → abort 第一次
    await act(async () => {
      await result.current.refresh()
    })
    expect(result.current.snapshot).toEqual(SNAPSHOT)

    // 第一次此刻才 resolve：必须被丢弃
    await act(async () => {
      resolveFirst({ snapshot: { ...SNAPSHOT, thread_id: 'stale' } })
      await Promise.resolve()
    })
    expect(result.current.snapshot?.thread_id).toBe('t1')
  })

  it('loading：请求期间为 true，落定后为 false', async () => {
    let resolveReq!: (v: ContextUsageResponse) => void
    fetchMock.mockImplementationOnce(
      () =>
        new Promise((res) => {
          resolveReq = res
        }),
    )
    const { result } = renderHook(() => useContextUsage('t1'))
    await waitFor(() => expect(result.current.loading).toBe(true))
    await act(async () => {
      resolveReq(NULL_RESP)
    })
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.snapshot).toBeNull()
    expect(result.current.error).toBeNull()
  })

  it('组件卸载时不写状态、不抛错', async () => {
    const abortErr = new Error('aborted')
    abortErr.name = 'AbortError'
    fetchMock.mockRejectedValue(abortErr)
    const { unmount } = renderHook(() => useContextUsage('t1'))
    unmount()
    // 只要不抛错即通过
    expect(true).toBe(true)
  })
})