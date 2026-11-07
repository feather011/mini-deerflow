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
  // 未设置 → 开发环境回退到本机后端（.env.local 缺失时也能跑 dev server）
  if (raw === undefined) {
    return 'http://127.0.0.1:8000'
  }
  // 显式设置（含空串、"/"）→ 按同源处理：调用方传的路径**本身已带 /api 前缀**
  // （如 apiGet('/api/health')），base 必须留空，最终拼出 /api/health。
  //
  // 切勿把生产 base 设成 '/api'：那样会拼成 /api/api/health，
  // 后端没有这个路由 → 全部 404 → 页面白屏（实测踩过）。
  return raw.trim().replace(/\/$/, '')
}

// Vite 环境变量通过 import.meta.env 访问
const viteEnv = (import.meta.env as Record<string, string | undefined>)
  .VITE_API_TARGET

// 兼容旧名（旧名优先级低于 VITE_API_TARGET）
const legacyEnv = (import.meta.env as Record<string, string | undefined>)
  .VITE_API_BASE_URL

// 直连后端（不走 Vite proxy，避免 SSE 缓冲问题）
// 生产环境由 nginx/网关统一转发，前端无需感知
const _raw = viteEnv ?? legacyEnv ?? 'http://127.0.0.1:8000'
export const API_BASE_URL = normalizeBaseUrl(_raw)

