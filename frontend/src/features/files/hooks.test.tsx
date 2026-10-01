/**
 * features/files/hooks.ts 测试（20E）
 *
 * 覆盖：
 * - useUploads / useArtifacts 调用正确的 query key 与 endpoint
 * - useUploadFile 上传成功后只失效 uploads 列表（不影响 artifacts）
 * - useDeleteFile 删除成功后只失效 uploads 列表
 * - 上传失败保留选择（不抛出）
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { useUploads, useArtifacts, useUploadFile, useDeleteFile } from './hooks'

// mock api 层
const mockListUploads = vi.fn()
const mockListArtifacts = vi.fn()
const mockUploadFile = vi.fn()
const mockDeleteFile = vi.fn()

vi.mock('./api', () => ({
  listUploads: (...args: unknown[]) => mockListUploads(...args),
  listArtifacts: (...args: unknown[]) => mockListArtifacts(...args),
  uploadFile: (...args: unknown[]) => mockUploadFile(...args),
  deleteFile: (...args: unknown[]) => mockDeleteFile(...args),
  downloadFile: vi.fn(),
  downloadArtifact: vi.fn(),
  encodeFilePath: (p: string) => p,
}))

beforeEach(() => {
  vi.clearAllMocks()
  mockListUploads.mockResolvedValue({ entries: [], total: 0 })
  mockListArtifacts.mockResolvedValue({ entries: [], total: 0 })
})

function makeWrapper(queryClient: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
}

// ---------------------------------------------------------------------------
// useUploads / useArtifacts
// ---------------------------------------------------------------------------

describe('useUploads', () => {
  it('调用 listUploads 并返回数据', async () => {
    mockListUploads.mockResolvedValueOnce({
      entries: [
        { path: 'uploads/a.txt', name: 'a.txt', size: 1, media_type: 'text/plain', kind: 'file' },
      ],
      total: 1,
    })

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { result } = renderHook(() => useUploads(), {
      wrapper: makeWrapper(queryClient),
    })

    // 等待 TanStack Query 完成异步获取
    for (let i = 0; i < 20 && result.current.data === undefined; i++) {
      await act(async () => {
        await new Promise((r) => setTimeout(r, 10))
      })
    }

    expect(result.current.data?.entries).toHaveLength(1)
    expect(mockListUploads).toHaveBeenCalled()
  })
})

describe('useArtifacts', () => {
  it('调用 listArtifacts', async () => {
    mockListArtifacts.mockResolvedValueOnce({
      entries: [
        { path: 'output.txt', name: 'output.txt', size: 2, media_type: 'text/plain', kind: 'file' },
      ],
      total: 1,
    })

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { result } = renderHook(() => useArtifacts(), {
      wrapper: makeWrapper(queryClient),
    })

    for (let i = 0; i < 20 && result.current.data === undefined; i++) {
      await act(async () => {
        await new Promise((r) => setTimeout(r, 10))
      })
    }

    expect(result.current.data?.entries[0]!.name).toBe('output.txt')
    expect(mockListArtifacts).toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
// useUploadFile — 缓存失效策略
// ---------------------------------------------------------------------------

describe('useUploadFile', () => {
  it('成功后只失效 uploads 列表，不影响 artifacts', async () => {
    mockUploadFile.mockResolvedValueOnce({
      path: 'uploads/new.txt',
      name: 'new.txt',
      size: 1,
      media_type: 'text/plain',
      kind: 'file',
    })

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    // 预热两个 query
    queryClient.setQueryData(['files', 'uploads'], { entries: [], total: 0 })
    queryClient.setQueryData(['files', 'artifacts'], { entries: [], total: 0 })

    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries')

    const { result } = renderHook(() => useUploadFile(), {
      wrapper: makeWrapper(queryClient),
    })

    const file = new File([''], 'new.txt', { type: 'text/plain' })
    await act(async () => {
      await result.current.mutateAsync({ file })
    })

    // 至少有一次 invalidate，且包含 uploads key
    expect(invalidateSpy).toHaveBeenCalled()
    const calls = invalidateSpy.mock.calls.map((c) => c[0])
    const uploadsCall = calls.find((c) =>
      JSON.stringify(c?.queryKey ?? null).includes('uploads'),
    )
    expect(uploadsCall).toBeDefined()

    // 关键断言：artifacts key 不在 invalidate 调用中
    const artifactsCall = calls.find((c) =>
      JSON.stringify(c?.queryKey ?? null).includes('artifacts'),
    )
    expect(artifactsCall).toBeUndefined()
  })

  it('失败时不失效任何缓存', async () => {
    mockUploadFile.mockRejectedValueOnce(new Error('boom'))

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries')

    const { result } = renderHook(() => useUploadFile(), {
      wrapper: makeWrapper(queryClient),
    })

    const file = new File([''], 'x.txt', { type: 'text/plain' })
    await act(async () => {
      try { await result.current.mutateAsync({ file }) } catch { /* expected */ }
    })

    expect(invalidateSpy).not.toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
// useDeleteFile — 缓存失效策略
// ---------------------------------------------------------------------------

describe('useDeleteFile', () => {
  it('成功后只失效 uploads 列表', async () => {
    mockDeleteFile.mockResolvedValueOnce(undefined)

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    queryClient.setQueryData(['files', 'uploads'], { entries: [], total: 0 })
    queryClient.setQueryData(['files', 'artifacts'], { entries: [], total: 0 })

    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries')

    const { result } = renderHook(() => useDeleteFile(), {
      wrapper: makeWrapper(queryClient),
    })

    await act(async () => {
      await result.current.mutateAsync({ path: 'a.txt' })
    })

    const calls = invalidateSpy.mock.calls.map((c) => c[0])
    const uploadsCall = calls.find((c) =>
      JSON.stringify(c?.queryKey ?? null).includes('uploads'),
    )
    expect(uploadsCall).toBeDefined()

    const artifactsCall = calls.find((c) =>
      JSON.stringify(c?.queryKey ?? null).includes('artifacts'),
    )
    expect(artifactsCall).toBeUndefined()
  })
})
