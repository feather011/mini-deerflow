/**
 * Agent Run API 测试
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { runAgent } from './runsApi'

// Mock apiPost
const mockApiPost = vi.hoisted(() => vi.fn())
vi.mock('../../shared/api/client', () => ({
  apiPost: mockApiPost,
}))

beforeEach(() => {
  vi.clearAllMocks()
})

describe('runAgent', () => {
  it('POST /api/threads/{threadId}/runs，携带 message body', async () => {
    const mockResponse = {
      run_id: 'run-123',
      thread_id: 'thread-abc',
      status: 'succeeded' as const,
      answer: 'Hello!',
      error: null,
    }
    mockApiPost.mockResolvedValueOnce(mockResponse)

    const result = await runAgent('thread-abc', { message: 'Hello' })

    expect(mockApiPost).toHaveBeenCalledWith(
      '/api/threads/thread-abc/runs',
      expect.objectContaining({
        body: { message: 'Hello' },
      }),
    )
    expect(result).toEqual(mockResponse)
  })

  it('threadId 包含特殊字符时进行 URL 编码', async () => {
    mockApiPost.mockResolvedValueOnce({
      run_id: 'r1',
      thread_id: 'a/b c',
      status: 'succeeded',
      answer: null,
      error: null,
    })

    await runAgent('a/b c', { message: 'hi' })

    expect(mockApiPost).toHaveBeenCalledWith(
      '/api/threads/a%2Fb%20c/runs',
      expect.any(Object),
    )
  })

  it('传递 AbortSignal', async () => {
    const abortController = new AbortController()
    mockApiPost.mockResolvedValueOnce({
      run_id: 'r1',
      thread_id: 't1',
      status: 'succeeded',
      answer: null,
      error: null,
    })

    await runAgent('t1', { message: 'hi' }, abortController.signal)

    expect(mockApiPost).toHaveBeenCalledWith(
      expect.any(String),
      expect.objectContaining({ signal: abortController.signal }),
    )
  })

  it('Run 失败时返回 failed 状态和 error 信息', async () => {
    const mockResponse = {
      run_id: 'run-456',
      thread_id: 'thread-xyz',
      status: 'failed' as const,
      answer: null,
      error: 'Agent execution failed',
    }
    mockApiPost.mockResolvedValueOnce(mockResponse)

    const result = await runAgent('thread-xyz', { message: 'fail' })

    expect(result.status).toBe('failed')
    expect(result.error).toBe('Agent execution failed')
  })

  it('ApiError 原样传播', async () => {
    const error = new Error('Request failed')
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(error as any).status = 500
    mockApiPost.mockRejectedValueOnce(error)

    await expect(runAgent('t1', { message: 'hi' })).rejects.toThrow('Request failed')
  })

  it('404 线程不存在时抛出 ApiError', async () => {
    const error = new Error('未知线程: invalid')
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(error as any).status = 404
    mockApiPost.mockRejectedValueOnce(error)

    await expect(runAgent('invalid', { message: 'hi' })).rejects.toThrow('未知线程: invalid')
  })

  it('409 已有 Run 进行中时抛出 ApiError', async () => {
    const error = new Error('线程已有 Run 进行中')
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(error as any).status = 409
    mockApiPost.mockRejectedValueOnce(error)

    await expect(runAgent('t1', { message: 'hi' })).rejects.toThrow('线程已有 Run 进行中')
  })

  it('429 Budget 超限时抛出 ApiError', async () => {
    const error = new Error('Budget exceeded')
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(error as any).status = 429
    mockApiPost.mockRejectedValueOnce(error)

    await expect(runAgent('t1', { message: 'hi' })).rejects.toThrow('Budget exceeded')
  })

  it('503 Run 已取消时抛出 ApiError', async () => {
    const error = new Error('Run cancelled')
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(error as any).status = 503
    mockApiPost.mockRejectedValueOnce(error)

    await expect(runAgent('t1', { message: 'hi' })).rejects.toThrow('Run cancelled')
  })

  it('504 Run 超时时抛出 ApiError', async () => {
    const error = new Error('Agent timeout')
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(error as any).status = 504
    mockApiPost.mockRejectedValueOnce(error)

    await expect(runAgent('t1', { message: 'hi' })).rejects.toThrow('Agent timeout')
  })
})
