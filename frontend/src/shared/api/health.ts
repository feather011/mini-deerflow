/**
 * Health / Ready API 调用函数
 *
 * 职责：
 * - 封装 GET /api/health 和 GET /api/ready
 * - 不处理错误，由调用方（TanStack Query）统一管理
 */
import type { HealthResponse, ReadyResponse } from '../types'
// shared/api 内部模块直接依赖源模块，避免通过 barrel 循环引用
import { apiGet } from './client'

export async function getHealth(): Promise<HealthResponse> {
  return apiGet<HealthResponse>('/api/health')
}

export async function getReady(): Promise<ReadyResponse> {
  return apiGet<ReadyResponse>('/api/ready')
}
