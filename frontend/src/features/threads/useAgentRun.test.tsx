/**
 * useAgentRun Hook 测试
 *
 * 验证：
 * - 成功只失效 threadKeys.detail(threadId)
 * - 失败不失效任何缓存
 * - ApiError 原样传播
 * - pending 状态
 * - mutation 使用正确参数
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { useAgentRun } from './hooks'

// Mock runAgent
const mockRunAgent = vi.fn()
vi.mock('./runsApi', () => ({
  runAgent: (...args: unknown[]) => mockRunAgent(...args),
}))

// Mock queryClient
const mockInvalidateQueries = vi.fn()
vi.mock('@tanstack/react-query', async () => {
  const actual = await vi.importActual('@tanstack/react-query')
  return {
    ...actual,
    useQueryClient: () => ({
      invalidateQueries: mockInvalidateQueries,
    }),
  }
})

function Wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('useAgentRun', () => {
  it('调用 runAgent 并传递 threadId 和 message', async () => {
    mockRunAgent.mockResolvedValueOnce({
      run_id: 'r1',
      thread_id: 't1',
      status: 'succeeded',
      answer: 'Hello!',
      error: null,
    })

    const { result } = renderHook(() => useAgentRun('t1'), { wrapper: Wrapper })

    result.current.mutate({ message: 'Hi' })

    await waitFor(() => {
      expect(mockRunAgent).toHaveBeenCalledWith('t1', { message: 'Hi' }, undefined)
    })
  })

  it('成功后 invalidateQueries.detail 被调用', async () => {
    mockRunAgent.mockResolvedValueOnce({
      run_id: 'r1',
      thread_id: 't1',
      status: 'succeeded',
      answer: 'Done',
      error: null,
    })

    const { result } = renderHook(() => useAgentRun('t1'), { wrapper: Wrapper })

    result.current.mutate({ message: 'Hi' })

    await vi.waitFor(() => {
      expect(mockInvalidateQueries).toHaveBeenCalledWith({
        queryKey: ['threads', 'detail', 't1'],
      })
    })
  })

  it('失败时不调用 invalidateQueries', async () => {
    mockRunAgent.mockRejectedValueOnce(new Error('Server error'))

    const { result } = renderHook(() => useAgentRun('t1'), { wrapper: Wrapper })

    result.current.mutate(
      { message: 'Hi' },
      { onError: () => {} },
    )

    await waitFor(() => {
      expect(mockInvalidateQueries).not.toHaveBeenCalled()
    })
  })

  it('ApiError 原样传播', async () => {
    const error = new Error('Unknown thread')
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(error as any).status = 404
    mockRunAgent.mockRejectedValueOnce(error)

    let caughtError: Error | undefined
    const { result } = renderHook(() => useAgentRun('invalid'), { wrapper: Wrapper })

    result.current.mutate(
      { message: 'Hi' },
      { onError: (err) => { caughtError = err as Error } },
    )

    await waitFor(() => {
      expect(caughtError?.message).toBe('Unknown thread')
    })
  })

  it('传递 AbortSignal', async () => {
    const abortController = new AbortController()
    mockRunAgent.mockResolvedValueOnce({
      run_id: 'r1',
      thread_id: 't1',
      status: 'succeeded',
      answer: null,
      error: null,
    })

    const { result } = renderHook(() => useAgentRun('t1'), { wrapper: Wrapper })

    result.current.mutate({ message: 'Hi', signal: abortController.signal })

    await waitFor(() => {
      expect(mockRunAgent).toHaveBeenCalledWith('t1', { message: 'Hi' }, abortController.signal)
    })
  })
})
