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
 *
 * 注入键用 `VITE_API_TARGET`：那是 vite.config.ts 的 /api 代理与本模块
 * 共同读取的主变量，也是 .env.example 里写的那个。改用旧名注入会在
 * 本机 `.env.local` 配了 `VITE_API_TARGET` 时被静默遮蔽，测到的
 * 反而不是生产真正走的那条优先级链。
 *
 * env 由**参数**传入而不是用例先 stub：模块加载前必须先把两个键都清成
 * 未设置，否则开发机 `.env.local` 的真实取值会混进断言。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

type EnvStub = Record<string, string | undefined>

/** 重新加载 config 模块，使其只读取本用例给定的 env。 */
async function loadApiBaseUrl(env: EnvStub = {}): Promise<string> {
  vi.resetModules()
  vi.stubEnv('VITE_API_TARGET', undefined)
  vi.stubEnv('VITE_API_BASE_URL', undefined)
  for (const [key, value] of Object.entries(env)) {
    vi.stubEnv(key, value)
  }
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
    expect(await loadApiBaseUrl({ VITE_API_TARGET: '/' })).toBe('')
  })

  it('显式空串同样归一化为空串（而不是回退到本机后端）', async () => {
    expect(await loadApiBaseUrl({ VITE_API_TARGET: '' })).toBe('')
  })

  it('带尾斜杠的绝对地址只去掉尾斜杠', async () => {
    expect(await loadApiBaseUrl({ VITE_API_TARGET: 'http://127.0.0.1:8000/' })).toBe(
      'http://127.0.0.1:8000',
    )
  })

  it('未设置时回退到开发默认值', async () => {
    expect(await loadApiBaseUrl()).toBe('http://127.0.0.1:8000')
  })

  it('旧名 VITE_API_BASE_URL 在没有新名时仍然生效', async () => {
    expect(await loadApiBaseUrl({ VITE_API_BASE_URL: 'http://127.0.0.1:8001' })).toBe(
      'http://127.0.0.1:8001',
    )
  })

  it('新旧两个键同时存在时以新名为准（代理与 SSE 指向同一个后端）', async () => {
    expect(
      await loadApiBaseUrl({
        VITE_API_TARGET: 'http://127.0.0.1:8123',
        VITE_API_BASE_URL: 'http://127.0.0.1:8000',
      }),
    ).toBe('http://127.0.0.1:8123')
  })
})

describe('路径拼接不产生重复的 /api 前缀', () => {
  it('/api/health 最终仍是 /api/health', async () => {
    const base = await loadApiBaseUrl({ VITE_API_TARGET: '/' })
    const url = buildUrl(base, '/api/health')

    expect(url).toBe('/api/health')
    expect(url).not.toContain('/api/api/')
  })

  it('所有实际调用的路径都保持原样', async () => {
    const base = await loadApiBaseUrl({ VITE_API_TARGET: '/' })

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
    const base = await loadApiBaseUrl({ VITE_API_TARGET: '/' })
    const url = buildUrl(base, '/api/threads/t1/runs/stream')

    expect(url).toBe('/api/threads/t1/runs/stream')
    expect(url).not.toContain('/api/api/')
  })

  it('反例：把生产 base 写成 /api 会得到 /api/api/（本测试要防的事故）', async () => {
    const base = await loadApiBaseUrl({ VITE_API_TARGET: '/api' })
    // base = '/api' 时必然重复——这正是当初白屏的根因，
    // 记录在此以便将来有人再想把 base 设成 '/api' 时能立刻看到后果。
    expect(buildUrl(base, '/api/health')).toBe('/api/api/health')
  })

  it('绝对 base 时路径同样不被改写，且 SSE 直连后端而非同源代理', async () => {
    const base = await loadApiBaseUrl({ VITE_API_TARGET: 'http://127.0.0.1:8123' })

    expect(buildUrl(base, '/api/health')).toBe('http://127.0.0.1:8123/api/health')
    expect(buildUrl(base, '/api/threads/t1/runs/stream')).toBe(
      'http://127.0.0.1:8123/api/threads/t1/runs/stream',
    )
  })
})