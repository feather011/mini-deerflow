/**
 * confirmationsApi fetch URL 测试 — TOOL-05 R2
 *
 * 不 mock confirmationApi 模块：直接调用真实函数，stub global fetch
 * 捕获真实请求 URL，断言含 /api 前缀（否则真实后端 404）。
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import {
  listConfirmations,
  getActiveRun,
  approveConfirmation,
  denyConfirmation,
  executeSnapshot,
} from './confirmations'

function stubFetch() {
  const urls: { url: string; method: string; body?: string }[] = []
  vi.stubGlobal('fetch', (url: string, init?: RequestInit) => {
    urls.push({
      url: String(url),
      method: init?.method ?? 'GET',
      body: typeof init?.body === 'string' ? init.body : undefined,
    })
    return Promise.resolve({
      status: 200,
      ok: true,
      text: () =>
        Promise.resolve(
          JSON.stringify({ confirmations: [], run_id: null }),
        ),
    })
  })
  return urls
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('confirmationsApi real fetch URLs', () => {
  it('listConfirmations 请求含 /api/confirmations', async () => {
    const urls = stubFetch()
    await listConfirmations({ thread_id: 't-1', run_id: 'r-1', state: 'pending' })
    expect(urls).toHaveLength(1)
    const u = new URL(urls[0]!.url)
    expect(u.pathname).toBe('/api/confirmations')
    expect(u.searchParams.get('thread_id')).toBe('t-1')
    expect(u.searchParams.get('run_id')).toBe('r-1')
    expect(u.searchParams.get('state')).toBe('pending')
  })

  it('getActiveRun 请求含 /api/threads/{id}/active_run', async () => {
    const urls = stubFetch()
    await getActiveRun('t-abc')
    expect(urls[0]!.url).toContain('/api/threads/t-abc/active_run')
  })

  it('approveConfirmation POST /api/confirmations/{id}/approve', async () => {
    const urls = stubFetch()
    await approveConfirmation('cf-1', 'tester')
    expect(urls[0]!.url).toContain('/api/confirmations/cf-1/approve')
    expect(urls[0]!.method).toBe('POST')
    expect(JSON.parse(urls[0]!.body!)).toEqual({ decided_by: 'tester' })
  })

  it('denyConfirmation POST /api/confirmations/{id}/deny', async () => {
    const urls = stubFetch()
    await denyConfirmation('cf-1')
    expect(urls[0]!.url).toContain('/api/confirmations/cf-1/deny')
  })

  it('executeSnapshot POST /api/confirmations/{id}/execute-snapshot + 绑定体', async () => {
    const urls = stubFetch()
    await executeSnapshot('cf-1', {
      run_id: 'r-1',
      thread_id: 't-1',
      tool_name: 'mcp__echo__echo',
      arguments_hash: 'h1',
    })
    expect(urls[0]!.url).toContain('/api/confirmations/cf-1/execute-snapshot')
    expect(urls[0]!.method).toBe('POST')
    // 不携带原参数（服务端读快照）
    const body = JSON.parse(urls[0]!.body!)
    expect(body).not.toHaveProperty('arguments')
    expect(body.arguments_hash).toBe('h1')
  })
})
