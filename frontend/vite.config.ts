import react from '@vitejs/plugin-react'
// 从 vitest/config 引入才能让 test 字段通过类型检查
import { defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    // e2e/ 归 Playwright 管，Vitest 不收集，避免两套 runner 抢同一批文件
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    coverage: {
      provider: 'v8' as const,
      reporter: ['text'] as const,
    },
  },
} satisfies Parameters<typeof defineConfig>[0])

