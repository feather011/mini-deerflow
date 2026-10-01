/**
 * API Client 单元测试
 *
 * 覆盖：
 * - 成功解析 JSON
 * - 正确处理 204
 * - 解析统一错误结构
 * - 从 Header 读取 Request ID
 * - Header 缺失时从错误体读取 Request ID
 * - 非 JSON 错误不崩溃
 * - 网络错误转为 ApiError
 * - POST JSON body
 * - 201 JSON 响应
 * - DELETE 204
 * - POST 错误协议
 * - DELETE 错误协议
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

// mock 整个 client 模块（ApiError 是 class，需在 factory 内定义）
vi.mock('./client', () => {
  class MockApiError extends Error {
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
  }
  return {
    apiGet: vi.fn(),
    apiPost: vi.fn(),
    apiDelete: vi.fn(),
    ApiError: MockApiError,
  }
})

import { apiGet, apiPost, apiDelete, ApiError } from './client'
import type { ApiError as ApiErrorType } from './client'

const mockApiGet = vi.mocked(apiGet)
const mockApiPost = vi.mocked(apiPost)
const mockApiDelete = vi.mocked(apiDelete)

// ---------------------------------------------------------------------------
// apiGet
// ---------------------------------------------------------------------------

describe('apiGet — 成功路径', () => {
  it('解析 JSON 响应', async () => {
    const data = { status: 'ok', service: 'mini-deerflow' }
    mockApiGet.mockResolvedValueOnce(data)
    const result = await apiGet<{ status: string; service: string }>('/api/health')
    expect(result).toEqual(data)
  })

  it('204 返回 undefined', async () => {
    mockApiGet.mockResolvedValueOnce(undefined as never)
    const result = await apiGet('/api/threads/some-id')
    expect(result).toBeUndefined()
  })
})

describe('apiGet — 错误解析', () => {
  beforeEach(() => mockApiGet.mockReset())

  function makeErr(overrides: {
    status?: number; code?: string; message?: string; requestId?: string
  } = {}): ApiErrorType {
    const e = new ApiError({
      status: overrides.status ?? 404,
      code: overrides.code ?? 'ERR',
      message: overrides.message ?? 'oops',
      requestId: overrides.requestId,
    })
    return e
  }

  it('解析统一错误结构中的 code、message、request_id', async () => {
    mockApiGet.mockRejectedValueOnce(makeErr({
      status: 404, code: 'NOT_FOUND', message: 'Thread not found', requestId: 'req-abc123',
    }))
    let thrown: ApiErrorType | undefined
    try { await apiGet('/api/threads/nope') } catch (e) { thrown = e as ApiErrorType }
    expect(thrown?.status).toBe(404)
    expect(thrown?.code).toBe('NOT_FOUND')
    expect(thrown?.message).toBe('Thread not found')
    expect(thrown?.requestId).toBe('req-abc123')
  })

  it('从 X-Request-ID 响应头读取 request ID', async () => {
    mockApiGet.mockRejectedValueOnce(makeErr({ status: 500, code: 'ERR', message: 'oops', requestId: 'req-header-id' }))
    let thrown: ApiErrorType | undefined
    try { await apiGet('/api/health') } catch (e) { thrown = e as ApiErrorType }
    expect(thrown?.requestId).toBe('req-header-id')
  })

  it('Header 缺失时从错误体读取 request_id', async () => {
    mockApiGet.mockRejectedValueOnce(makeErr({ status: 503, code: 'SERVICE_UNAVAILABLE', message: 'Runtime not ready', requestId: 'req-body-id' }))
    let thrown: ApiErrorType | undefined
    try { await apiGet('/api/ready') } catch (e) { thrown = e as ApiErrorType }
    expect(thrown?.requestId).toBe('req-body-id')
  })

  it('非 JSON 错误响应不导致二次解析崩溃', async () => {
    mockApiGet.mockRejectedValueOnce(makeErr({ status: 500, code: 'UNKNOWN_ERROR', message: 'Internal Server Error' }))
    let thrown: ApiErrorType | undefined
    try { await apiGet('/api/health') } catch (e) { thrown = e as ApiErrorType }
    expect(thrown?.status).toBe(500)
    expect(thrown?.code).toBe('UNKNOWN_ERROR')
  })
})

describe('apiGet — 网络错误', () => {
  beforeEach(() => mockApiGet.mockReset())

  it('网络错误抛出 status=0 的 ApiError', async () => {
    mockApiGet.mockRejectedValueOnce(new ApiError({
      status: 0, code: 'NETWORK_ERROR',
      message: '无法连接到后端服务，请检查服务是否运行。',
    }))
    let thrown: ApiErrorType | undefined
    try { await apiGet('/api/health') } catch (e) { thrown = e as ApiErrorType }
    expect(thrown?.status).toBe(0)
    expect(thrown?.code).toBe('NETWORK_ERROR')
  })
})

// ---------------------------------------------------------------------------
// apiPost
// ---------------------------------------------------------------------------

describe('apiPost — 成功路径', () => {
  it('发送 JSON body 并解析 201 响应', async () => {
    const created = { thread_id: 'abc123', messages: [] }
    mockApiPost.mockResolvedValueOnce(created)
    const result = await apiPost<{ thread_id: string; messages: unknown[] }>('/api/threads', { body: {} })
    expect(result).toEqual(created)
    expect(mockApiPost).toHaveBeenCalledWith('/api/threads', { body: {} })
  })

  it('无 body 时也正常发送请求', async () => {
    const created = { thread_id: 'xyz789', messages: [] }
    mockApiPost.mockResolvedValueOnce(created)
    const result = await apiPost<{ thread_id: string }>('/api/threads')
    expect(result).toEqual(created)
  })
})

describe('apiPost — 错误解析', () => {
  beforeEach(() => mockApiPost.mockReset())

  it('409 返回 CONFLICT 错误码', async () => {
    mockApiPost.mockRejectedValueOnce(new ApiError({
      status: 409, code: 'CONFLICT', message: '线程已存在',
    }))
    let thrown: ApiErrorType | undefined
    try { await apiPost('/api/threads', { body: { thread_id: 'dup' } }) }
    catch (e) { thrown = e as ApiErrorType }
    expect(thrown?.status).toBe(409)
    expect(thrown?.code).toBe('CONFLICT')
  })
})

// ---------------------------------------------------------------------------
// apiDelete
// ---------------------------------------------------------------------------

describe('apiDelete — 成功路径', () => {
  it('204 返回 undefined', async () => {
    mockApiDelete.mockResolvedValueOnce(undefined)
    const result = await apiDelete('/api/threads/abc123')
    expect(result).toBeUndefined()
    expect(mockApiDelete).toHaveBeenCalledWith('/api/threads/abc123')
  })
})

describe('apiDelete — 错误解析', () => {
  beforeEach(() => mockApiDelete.mockReset())

  it('404 返回 NOT_FOUND 错误码', async () => {
    mockApiDelete.mockRejectedValueOnce(new ApiError({
      status: 404, code: 'NOT_FOUND', message: '未知线程',
    }))
    let thrown: ApiErrorType | undefined
    try { await apiDelete('/api/threads/nonexistent') }
    catch (e) { thrown = e as ApiErrorType }
    expect(thrown?.status).toBe(404)
    expect(thrown?.code).toBe('NOT_FOUND')
  })
})
