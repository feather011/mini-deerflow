/**
 * Thread API 单元测试
 *
 * 覆盖：
 * - list 路径正确
 * - create 请求正确
 * - detail 对 thread_id 编码
 * - delete 对 thread_id 编码
 * - 204 正确处理
 * - 错误原样传播
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import {
  listThreads,
  createThread,
  getThread,
  deleteThread,
} from './api'

// mock 底层 API Client
vi.mock('../../shared/api/client', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiDelete: vi.fn(),
}))

import { apiGet, apiPost, apiDelete } from '../../shared/api/client'

const mockGet = vi.mocked(apiGet)
const mockPost = vi.mocked(apiPost)
const mockDelete = vi.mocked(apiDelete)

// ---------------------------------------------------------------------------
// 路径验证
// ---------------------------------------------------------------------------

describe('listThreads', () => {
  beforeEach(() => mockGet.mockReset())

  it('调用 GET /api/threads', async () => {
    mockGet.mockResolvedValueOnce({ threads: [] })
    await listThreads()
    expect(mockGet).toHaveBeenCalledWith('/api/threads', { signal: undefined })
  })

  it('传递 AbortSignal', async () => {
    const controller = new AbortController()
    mockGet.mockResolvedValueOnce({ threads: [] })
    await listThreads(controller.signal)
    expect(mockGet).toHaveBeenCalledWith('/api/threads', { signal: controller.signal })
  })
})

describe('createThread', () => {
  beforeEach(() => mockPost.mockReset())

  it('调用 POST /api/threads', async () => {
    mockPost.mockResolvedValueOnce({ thread_id: 'abc', messages: [] })
    await createThread()
    expect(mockPost).toHaveBeenCalledWith('/api/threads', { signal: undefined })
  })

  it('携带 thread_id body', async () => {
    mockPost.mockResolvedValueOnce({ thread_id: 'my-id', messages: [] })
    await createThread({ thread_id: 'my-id' })
    expect(mockPost).toHaveBeenCalledWith('/api/threads', {
      body: { thread_id: 'my-id' },
      signal: undefined,
    })
  })

  it('201 返回 ThreadDetail', async () => {
    const detail = { thread_id: 'abc', messages: [{ role: 'user', content: 'hi' }] }
    mockPost.mockResolvedValueOnce(detail)
    const result = await createThread()
    expect(result).toEqual(detail)
  })
})

describe('getThread — thread_id 编码', () => {
  beforeEach(() => mockGet.mockReset())

  it('普通 ID 直接拼接', async () => {
    mockGet.mockResolvedValueOnce({ thread_id: 'abc123', messages: [] })
    await getThread('abc123')
    expect(mockGet).toHaveBeenCalledWith('/api/threads/abc123', { signal: undefined })
  })

  it('包含空格的 ID 进行 URL 编码', async () => {
    mockGet.mockResolvedValueOnce({ thread_id: 'hello world', messages: [] })
    await getThread('hello world')
    expect(mockGet).toHaveBeenCalledWith('/api/threads/hello%20world', { signal: undefined })
  })

  it('包含斜杠的 ID 进行 URL 编码', async () => {
    mockGet.mockResolvedValueOnce({ thread_id: 'a/b', messages: [] })
    await getThread('a/b')
    expect(mockGet).toHaveBeenCalledWith('/api/threads/a%2Fb', { signal: undefined })
  })

  it('传递 AbortSignal', async () => {
    const controller = new AbortController()
    mockGet.mockResolvedValueOnce({ thread_id: 'abc', messages: [] })
    await getThread('abc', controller.signal)
    expect(mockGet).toHaveBeenCalledWith('/api/threads/abc', { signal: controller.signal })
  })
})

describe('deleteThread — thread_id 编码', () => {
  beforeEach(() => mockDelete.mockReset())

  it('DELETE /api/threads/{id}', async () => {
    mockDelete.mockResolvedValueOnce(undefined)
    await deleteThread('abc123')
    expect(mockDelete).toHaveBeenCalledWith('/api/threads/abc123', { signal: undefined })
  })

  it('204 返回 undefined', async () => {
    mockDelete.mockResolvedValueOnce(undefined)
    const result = await deleteThread('abc')
    expect(result).toBeUndefined()
  })

  it('ApiError 原样传播', async () => {
    const err = new Error('not found')
    mockDelete.mockRejectedValueOnce(err)
    await expect(deleteThread('nonexistent')).rejects.toThrow('not found')
  })
})
