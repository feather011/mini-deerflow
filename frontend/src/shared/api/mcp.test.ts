/**
 * MCP API fetch URL 测试 — TOOL-05 R1
 *
 * 不 mock mcp 模块：stub fetch 捕获真实 URL，断言 /api/mcp/* 路径
 * 与 enable / grant / revoke 端点及三主体枚举。
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import {
  listMcpServers,
  listMcpTools,
  setMcpToolEnabled,
  grantMcpTool,
  revokeMcpTool,
  MCP_PRINCIPALS,
} from './mcp'

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
      text: () => Promise.resolve(JSON.stringify({ servers: [], tools: [] })),
    })
  })
  return urls
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('mcpApi real fetch URLs', () => {
  it('三主体枚举与后端 ExecutorPrincipal 一致', () => {
    expect([...MCP_PRINCIPALS]).toEqual([
      'MAIN_AGENT',
      'MINI_SUBAGENT',
      'HARNESS_AGENT',
    ])
  })

  it('listMcpServers → /api/mcp/servers', async () => {
    const urls = stubFetch()
    await listMcpServers()
    expect(new URL(urls[0]!.url).pathname).toBe('/api/mcp/servers')
  })

  it('listMcpTools → /api/mcp/tools', async () => {
    const urls = stubFetch()
    await listMcpTools()
    expect(new URL(urls[0]!.url).pathname).toBe('/api/mcp/tools')
  })

  it('setMcpToolEnabled → POST /api/mcp/tools/{name}/enable', async () => {
    const urls = stubFetch()
    await setMcpToolEnabled('mcp__echo__echo', true)
    expect(new URL(urls[0]!.url).pathname).toBe(
      '/api/mcp/tools/mcp__echo__echo/enable',
    )
    expect(urls[0]!.method).toBe('POST')
    expect(JSON.parse(urls[0]!.body!)).toEqual({ enabled: true })
  })

  it('grantMcpTool → POST /api/mcp/tools/{name}/grant {principal}', async () => {
    const urls = stubFetch()
    await grantMcpTool('mcp__echo__echo', 'MINI_SUBAGENT')
    expect(new URL(urls[0]!.url).pathname).toBe(
      '/api/mcp/tools/mcp__echo__echo/grant',
    )
    expect(JSON.parse(urls[0]!.body!)).toEqual({ principal: 'MINI_SUBAGENT' })
  })

  it('revokeMcpTool → POST /api/mcp/tools/{name}/revoke {principal}', async () => {
    const urls = stubFetch()
    await revokeMcpTool('mcp__echo__echo', 'HARNESS_AGENT')
    expect(new URL(urls[0]!.url).pathname).toBe(
      '/api/mcp/tools/mcp__echo__echo/revoke',
    )
    expect(JSON.parse(urls[0]!.body!)).toEqual({ principal: 'HARNESS_AGENT' })
  })
})
