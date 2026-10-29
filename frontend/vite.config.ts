import { loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
// 从 vitest/config 引入才能让 test 字段通过类型检查
import { defineConfig } from 'vitest/config'

export default defineConfig(({ mode }) => {
  // 必须用 Vite 的 loadEnv 读，不能只读 process.env：
  // src/shared/api/config.ts 走的是 import.meta.env（.env 文件 + shell），
  // 这里若只认 process.env，两处会指向不同的后端 —— 表现为
  // 「SSE 直连 A 端口、/api 代理 B 端口」，很难排查。
  const env = loadEnv(mode, process.cwd(), 'VITE_')

  return {
    plugins: [react()],
    // strictPort=false：允许 fallback 到其他端口
    server: {
      host: '0.0.0.0', // 监听所有地址，兼容 IPv4/IPv6
      port: 5173,
      proxy: {
        '/api': {
          target: env.VITE_API_TARGET || 'http://127.0.0.1:8000',
        },
      },
    },
    test: {
      globals: true,
      environment: 'jsdom',
      setupFiles: ['./src/test/setup.ts'],
      // e2e/ 归 Playwright 管，Vitest 不收集，避免两套 runner 抢同一批文件
      include: ['src/**/*.{test,spec}.{ts,tsx}'],
      coverage: {
        provider: 'v8' as const,
        reporter: ['text' as const],
      },
    },
  } satisfies Parameters<typeof defineConfig>[0]
})