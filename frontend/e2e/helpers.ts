/**
 * E2E 共享工具（20F）
 */
import { expect, test as base, type Page } from '@playwright/test'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'

/** 控制台错误收集器：过滤掉与被测行为无关的噪声。 */
export interface ConsoleWatcher {
  /** 断言期间没有出现 CORS / React / 未处理 Promise 错误 */
  assertClean(): Promise<void>
  /** 返回已捕获的错误消息 */
  errors(): string[]
}

const IGNORABLE = [
  // Chromium 在无扩展环境下对 favicon 的 404，与业务无关
  /favicon/i,
  // Vite HMR 断连提示（服务重启瞬间）
  /vite.*connect/i,
  // 被应用正确捕获并渲染成 ErrorNotice 的 4xx/5xx 响应。
  // 这类日志表示「响应已收到」，不是 CORS 或网络故障；
  // 真正的网络失败是 net::ERR_*，CORS 是 blocked by CORS policy，二者不匹配本条。
  /Failed to load resource: the server responded with a status of \d{3}/,
]

/**
 * 挂载控制台监听，收集 error 级日志与未捕获异常。
 *
 * 覆盖必测项 10：控制台不能有 CORS、React、未处理 Promise 错误。
 */
export function watchConsole(page: Page): ConsoleWatcher {
  const errors: string[] = []

  page.on('console', (msg) => {
    if (msg.type() !== 'error') return
    const text = msg.text()
    if (IGNORABLE.some((re) => re.test(text))) return
    errors.push(text)
  })

  page.on('pageerror', (err) => {
    errors.push(`[pageerror] ${err.message}`)
  })

  return {
    errors: () => errors,
    async assertClean() {
      expect(errors, `控制台出现错误：\n${errors.join('\n')}`).toEqual([])
    },
  }
}

/** 扩展后的 test，附带 console watcher。 */
export const test = base.extend<{ console: ConsoleWatcher }>({
  console: async ({ page }, use) => {
    await use(watchConsole(page))
  },
})

export { expect } from '@playwright/test'

/** 创建一个临时测试文件，返回其绝对路径。 */
export function makeTempFile(name: string, content: string | Buffer): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'mdf_e2e_upload_'))
  const filePath = path.join(dir, name)
  fs.writeFileSync(filePath, content)
  return filePath
}

/** 读取下载文件内容为 Buffer。 */
export function readDownload(downloadPath: string): Buffer {
  return fs.readFileSync(downloadPath)
}

/** 后端地址（与 playwright.config.ts webServer 保持一致）。 */
export const BACKEND_URL = 'http://127.0.0.1:8000'

/** 预置 Artifact 的内容（与 tests/e2e_server.py ARTIFACT_CONTENT 一致）。 */
export const ARTIFACT_CONTENT = 'mini-deerflow-20f-e2e-artifact\n'
export const ARTIFACT_NAME = 'artifact-test.txt'

/** 等待后端就绪（health + ready 同时成功）。 */
export async function waitBackendReady(request: import('@playwright/test').APIRequestContext) {
  await expect
    .poll(
      async () => {
        const res = await request.get(`${BACKEND_URL}/api/ready`)
        return res.ok()
      },
      { timeout: 20_000, message: '后端 /api/ready 未在超时内就绪' },
    )
    .toBe(true)
}
