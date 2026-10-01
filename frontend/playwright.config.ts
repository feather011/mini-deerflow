/**
 * Playwright E2E 配置（20F）
 *
 * 设计要点：
 * - 自动拉起确定性后端（tests/e2e_server.py：临时 workspace + 预置 Artifact + FakeModel）
 * - 自动拉起 Vite dev server，并把 API Base URL 注入为本地后端
 * - 两个服务都设 reuseExistingServer:false，保证每次跑都是干净环境，不依赖执行顺序
 *
 * 运行：
 *   npm run test:e2e
 */
import { defineConfig, devices } from '@playwright/test'

const BACKEND_PORT = 8000
const FRONTEND_PORT = 5173

export default defineConfig({
  testDir: './e2e',
  // 失败时保留 trace / screenshot / video，便于排查
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: [['list']],
  timeout: 30_000,
  expect: { timeout: 10_000 },

  use: {
    baseURL: `http://localhost:${FRONTEND_PORT}`,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    // 下载测试必须真实落盘校验内容
    acceptDownloads: true,
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],

  webServer: [
    {
      // E2E 专用后端：临时 workspace + 预置 Artifact + FakeModel（无 API Key）
      command: `uv run python -m tests.e2e_server --port ${BACKEND_PORT}`,
      cwd: '..',
      url: `http://127.0.0.1:${BACKEND_PORT}/api/health`,
      reuseExistingServer: false,
      timeout: 60_000,
      stdout: 'pipe',
      stderr: 'pipe',
    },
    {
      command: 'npm run dev',
      cwd: '.',
      url: `http://localhost:${FRONTEND_PORT}`,
      reuseExistingServer: false,
      timeout: 60_000,
      env: {
        VITE_API_BASE_URL: `http://127.0.0.1:${BACKEND_PORT}`,
      },
    },
  ],
})
