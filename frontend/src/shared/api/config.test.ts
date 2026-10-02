/**
 * API_BASE_URL 永久回归测试
 *
 * 背景：生产镜像曾用 `VITE_API_BASE_URL=/api` 构建，而调用方传的路径
 * **本身已带 /api 前缀**（`apiGet('/api/health')`），两者相加得到
 * `/api/api/health` → 后端无此路由 → 全部 404 → 页面白屏。
 *
 * 开发模式（base 是绝对地址 http://127.0.0.1:8000）与 Playwright E2E
 * 都测不到这个问题，因此必须用**针对 base 归一化的单测**把它钉死。
 *
 * 三条不变式：
 *   1. 生产同源配置 "/" 归一化为空串
 *   2. "/api/health" 最终仍然是 "/api/health"
 *   3. 任何路径都不会产生 "/api/api/"
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

/** 重新加载 config 模块，使其读取被 stub 的 import.meta.env。 */
async function loadApiBaseUrl(): Promise<string> {
  vi.resetModules()
  const mod = await import('./config')
  return mod.API_BASE_URL
}

/** 模拟 client.ts 的实际拼接方式：`${API_BASE_URL}${path}`。 */
function buildUrl(base: string, path: string): string {
  return `${base}${path}`
}

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('normalizeBaseUrl 的同源归一化', () => {
  it('生产配置 "/" 归一化为空串', async () => {
    vi.stubEnv('VITE_API_BASE_URL', '/')
    expect(await loadApiBaseUrl()).toBe('')
  })

  it('显式空串同样归一化为空串（而不是回退到本机后端）', async () => {
    vi.stubEnv('VITE_API_BASE_URL', '')
    expect(await loadApiBaseUrl()).toBe('')
  })

  it('带尾斜杠的绝对地址只去掉尾斜杠', async () => {
    vi.stubEnv('VITE_API_BASE_URL', 'http://127.0.0.1:8000/')
    expect(await loadApiBaseUrl()).toBe('http://127.0.0.1:8000')
  })

  it('未设置时回退到开发默认值', async () => {
    vi.stubEnv('VITE_API_BASE_URL', undefined)
    expect(await loadApiBaseUrl()).toBe('http://127.0.0.1:8000')
  })
})

describe('路径拼接不产生重复的 /api 前缀', () => {
  it('/api/health 最终仍是 /api/health', async () => {
    vi.stubEnv('VITE_API_BASE_URL', '/')
    const base = await loadApiBaseUrl()
    const url = buildUrl(base, '/api/health')

    expect(url).toBe('/api/health')
    expect(url).not.toContain('/api/api/')
  })

  it('所有实际调用的路径都保持原样', async () => {
    vi.stubEnv('VITE_API_BASE_URL', '/')
    const base = await loadApiBaseUrl()

    // 与 src/shared/api 下真实调用一一对应
    const paths = [
      '/api/health',
      '/api/ready',
      '/api/threads',
      '/api/files',
      '/api/artifacts',
      '/api/threads/some-id',
      '/api/threads/some-id/runs',
    ]
    for (const path of paths) {
      expect(buildUrl(base, path)).toBe(path)
      expect(buildUrl(base, path)).not.toContain('/api/api/')
    }
  })

  it('SSE 流式地址同样不重复（streamClient 自行拼 /api）', async () => {
    vi.stubEnv('VITE_API_BASE_URL', '/')
    const base = await loadApiBaseUrl()
    const url = buildUrl(base, '/api/threads/t1/runs/stream')

    expect(url).toBe('/api/threads/t1/runs/stream')
    expect(url).not.toContain('/api/api/')
  })

  it('反例：把生产 base 写成 /api 会得到 /api/api/（本测试要防的事故）', async () => {
    vi.stubEnv('VITE_API_BASE_URL', '/api')
    const base = await loadApiBaseUrl()
    // base = '/api' 时必然重复——这正是当初白屏的根因，
    // 记录在此以便将来有人再想把 base 设成 '/api' 时能立刻看到后果。
    expect(buildUrl(base, '/api/health')).toBe('/api/api/health')
  })
})
