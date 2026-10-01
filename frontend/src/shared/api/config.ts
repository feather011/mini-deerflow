/**
 * API Base URL 配置模块
 *
 * 职责：
 * - 集中读取环境变量
 * - 规范化 URL（去除末尾斜杠、协议前缀）
 * - 提供默认值，避免业务组件直接读取环境变量
 *
 * 读取顺序：
 * 1. Vite/Vitest 环境变量 VITE_API_BASE_URL
 * 2. Node 环境变量 API_BASE_URL（Vitest setupFiles 注入）
 * 3. 硬编码默认值
 */

function normalizeBaseUrl(raw: string | undefined): string {
  const fallback = 'http://127.0.0.1:8000'
  const base = raw?.trim() || fallback
  return base.replace(/\/$/, '')
}

// Vite 环境变量通过 import.meta.env 访问
const viteEnv = (import.meta.env as Record<string, string | undefined>)
  .VITE_API_BASE_URL

// 兼容 Vitest：在 vitest.setup.ts 中通过 import.meta.env.VITE_API_BASE_URL 注入
const vitestEnv = (import.meta.env as Record<string, string | undefined>)
  .VITE_API_BASE_URL

export const API_BASE_URL = normalizeBaseUrl(viteEnv ?? vitestEnv)

