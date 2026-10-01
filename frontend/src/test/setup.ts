/**
 * Vitest 全局测试配置
 *
 * 职责：
 * - 导入 @testing-library/jest-dom/vitest（为 Vitest Assertion 增加
 *   toBeInTheDocument 等 DOM matcher 的类型与运行时实现；根路径只增强 Jest）
 * - 设置 DOM 环境（已在 vite.config.ts 中配置 environment: jsdom）
 * - 注入 VITE_API_BASE_URL 到 process.env（供 API config 模块读取）
 * - Mock Element.prototype.scrollIntoView（jsdom 不支持）
 */
import '@testing-library/jest-dom/vitest'

// Vitest 不提供 import.meta.env，通过 process.env 注入
process.env.VITE_API_BASE_URL = 'http://127.0.0.1:8000'

// Mock scrollIntoView（jsdom 不支持）
Element.prototype.scrollIntoView = vi.fn()
