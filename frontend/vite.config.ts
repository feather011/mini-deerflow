import react from '@vitejs/plugin-react'
// 从 vitest/config 引入才能让 test 字段通过类型检查
import { defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [react()],
  // 开发期同源反代：浏览器看到 /api/* → Vite 转发到后端
  // —— 与生产 nginx.conf 的 proxy_pass 行为一致（不变路径前缀）。
  // SSE 必须关缓冲（对应 nginx 的 proxy_buffering off）。
  //
  // strictPort：固定端口避免端口冲突
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: process.env.VITE_API_TARGET || "http://127.0.0.1:8001",
        changeOrigin: true,
        ws: true,
        configure: (proxy) => {
          proxy.on("proxyReq", (proxyReq) => {
            proxyReq.setHeader(
              "X-Forwarded-Proto",
              "http",
            );
          });
        },
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
      reporter: ['text'] as const,
    },
  },
} satisfies Parameters<typeof defineConfig>[0])

