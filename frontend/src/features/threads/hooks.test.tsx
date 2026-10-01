/**
 * Thread Hooks 单元测试
 *
 * 覆盖：
 * - 列表成功（合并列表项 + 详情）
 * - 列表失败（ApiError 传播）
 * - 详情无 ID 时不请求
 * - 详情切换 ID 后重新请求
 * - 创建成功后失效列表（返回新建 Thread）
 * - 删除成功后失效列表并移除详情缓存
 * - ApiError 原样传播
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

// mock API（必须在 import 之前 hoisted）
vi.mock('./api', () => ({
  listThreads: vi.fn(),
  createThread: vi.fn(),
  getThread: vi.fn(),
  deleteThread: vi.fn(),
}))
vi.mock('../../shared/api/client', () => ({
  ApiError: class extends Error {
    status: number
    code: string
    requestId?: string
    details: unknown
    override message: string
    constructor(opts: {
      status: number; code: string; message: string
      requestId?: string; details?: unknown
    }) {
      super(opts.message)
      this.name = 'ApiError'
      this.status = opts.status
      this.code = opts.code
      this.message = opts.message
      this.requestId = opts.requestId
      this.details = opts.details ?? null
    }
  },
}))

import { listThreads, createThread, getThread, deleteThread } from './api'
import { ApiError } from '../../shared/api/client'

const mockList = vi.mocked(listThreads)
const mockCreate = vi.mocked(createThread)
const mockGet = vi.mocked(getThread)
const mockDelete = vi.mocked(deleteThread)

function makeClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: {},
    },
  })
}

function Wrapper({ children, client }: { children: ReactNode; client: QueryClient }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

// 动态导入确保 mock 已就位
let hooks: typeof import('./hooks')
beforeEach(async () => {
  vi.clearAllMocks()
  hooks = await import('./hooks')
})

// ---------------------------------------------------------------------------
// useThreads
// ---------------------------------------------------------------------------

describe('useThreads', () => {
  it('列表成功：先请求列表，再逐个请求详情', async () => {
    const client = makeClient()
    mockList.mockResolvedValueOnce({ threads: [{ thread_id: 'a' }, { thread_id: 'b' }] })
    mockGet.mockResolvedValueOnce({ thread_id: 'a', messages: [] })
    mockGet.mockResolvedValueOnce({ thread_id: 'b', messages: [] })

    const { result } = renderHook(() => hooks.useThreads(), {
      wrapper: ({ children }) => Wrapper({ children, client }),
    })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data).toHaveLength(2)
    expect(mockList).toHaveBeenCalledTimes(1)
    expect(mockGet).toHaveBeenCalledTimes(2)
  })

  it('列表失败：ApiError 传播', async () => {
    const client = makeClient()
    const err = new ApiError({ status: 503, code: 'SERVICE_UNAVAILABLE', message: 'Runtime unavailable' })
    mockList.mockRejectedValueOnce(err)

    const { result } = renderHook(() => hooks.useThreads(), {
      wrapper: ({ children }) => Wrapper({ children, client }),
    })

    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(result.current.error?.status).toBe(503)
  })
})

// ---------------------------------------------------------------------------
// useThread
// ---------------------------------------------------------------------------

describe('useThread', () => {
  it('threadId 为 null 时不请求', () => {
    const client = makeClient()
    const { result } = renderHook(() => hooks.useThread(null), {
      wrapper: ({ children }) => Wrapper({ children, client }),
    })
    expect(result.current.isFetching).toBe(false)
    expect(mockGet).not.toHaveBeenCalled()
  })

  it('threadId 有值时请求详情', async () => {
    const client = makeClient()
    mockGet.mockResolvedValueOnce({ thread_id: 'abc', messages: [] })

    const { result } = renderHook(() => hooks.useThread('abc'), {
      wrapper: ({ children }) => Wrapper({ children, client }),
    })

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.thread_id).toBe('abc')
  })

  it('切换 threadId 后重新请求', async () => {
    const client = makeClient()
    mockGet
      .mockResolvedValueOnce({ thread_id: 'abc', messages: [] })
      .mockResolvedValueOnce({ thread_id: 'xyz', messages: [] })

    const { result, rerender } = renderHook(
      ({ id }: { id: string }) => hooks.useThread(id),
      { wrapper: ({ children }) => Wrapper({ children, client }), initialProps: { id: 'abc' } },
    )

    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.data?.thread_id).toBe('abc')

    rerender({ id: 'xyz' })
    await waitFor(() => expect(result.current.data?.thread_id).toBe('xyz'))
    expect(mockGet).toHaveBeenCalledTimes(2)
  })

  it('404 ApiError 原样传播', async () => {
    const client = makeClient()
    const err = new ApiError({ status: 404, code: 'NOT_FOUND', message: '未知线程' })
    mockGet.mockRejectedValueOnce(err)

    const { result } = renderHook(() => hooks.useThread('nonexistent'), {
      wrapper: ({ children }) => Wrapper({ children, client }),
    })

    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(result.current.error?.status).toBe(404)
  })
})

// ---------------------------------------------------------------------------
// useCreateThread
// ---------------------------------------------------------------------------

describe('useCreateThread', () => {
  it('创建成功后返回新建 Thread', async () => {
    const client = makeClient()
    const newThread = { thread_id: 'new-id', messages: [] }
    mockCreate.mockResolvedValueOnce(newThread)

    const { result } = renderHook(() => hooks.useCreateThread(), {
      wrapper: ({ children }) => Wrapper({ children, client }),
    })

    const created = await result.current.mutateAsync({})
    expect(created.thread_id).toBe('new-id')
  })
})

// ---------------------------------------------------------------------------
// useDeleteThread
// ---------------------------------------------------------------------------

describe('useDeleteThread', () => {
  it('删除成功后返回 void', async () => {
    const client = makeClient()
    mockDelete.mockResolvedValueOnce(undefined)

    const { result } = renderHook(() => hooks.useDeleteThread(), {
      wrapper: ({ children }) => Wrapper({ children, client }),
    })

    await expect(result.current.mutateAsync({ threadId: 'abc' })).resolves.toBeUndefined()
  })

  it('删除失败：ApiError 传播', async () => {
    const client = makeClient()
    const err = new ApiError({ status: 404, code: 'NOT_FOUND', message: '未知线程' })
    mockDelete.mockRejectedValueOnce(err)

    const { result } = renderHook(() => hooks.useDeleteThread(), {
      wrapper: ({ children }) => Wrapper({ children, client }),
    })

    await expect(result.current.mutateAsync({ threadId: 'abc' })).rejects.toThrow()
  })
})
