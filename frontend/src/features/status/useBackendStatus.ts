/**
 * 后端连接状态 Feature Hook
 *
 * 职责：
 * - 使用 TanStack Query 分别查询 health 和 ready
 * - 组合两种状态，推导出 UI 展示状态（checking/ready/not-ready/offline/error）
 * - 不暴露具体异常类型给 UI 层
 */
import { useQuery } from '@tanstack/react-query'
import type { HealthResponse, ReadyResponse } from '../../shared/types'
import { getHealth, getReady } from '../../shared/api/health'
import type { ApiError } from '../../shared/api/client'

/** UI 展示用的聚合状态 */
export type BackendStatus =
  | 'checking'    // 初始 loading
  | 'ready'       // health + ready 均成功
  | 'not-ready'   // health 成功，但 ready 返回 503（进程在线、Runtime 未就绪）
  | 'offline'     // health 网络失败（断连、DNS 失败）
  | 'error'       // health 返回非 200（进程崩溃但有响应）

interface StatusResult {
  status: BackendStatus
  healthError?: ApiError
  requestId?: string
}

export function useBackendStatus(): StatusResult {
  // health：进程存活检查，不依赖 Runtime
  const health = useQuery<HealthResponse, ApiError>({
    queryKey: ['backend', 'health'],
    queryFn: getHealth,
    retry: false,       // 网络失败立即显示 offline，不无限重试
    staleTime: 30_000,  // 30s 内不重复请求
  })

  // ready：Runtime 就绪检查，依赖 health 成功后才有意义
  const ready = useQuery<ReadyResponse, ApiError>({
    queryKey: ['backend', 'ready'],
    queryFn: getReady,
    enabled: health.isSuccess,  // health 失败时不发 ready 请求
    retry: false,
    staleTime: 30_000,
  })

  // 三种失败路径
  if (health.isError) {
    if (health.error.status === 0) {
      return { status: 'offline', healthError: health.error }
    }
    return {
      status: 'error',
      healthError: health.error,
      requestId: health.error.requestId,
    }
  }

  if (health.isSuccess && ready.isError) {
    if (ready.error.status === 503) {
      // health 成功但 Runtime 未就绪：进程在线但初始化未完成
      return { status: 'not-ready', requestId: ready.error.requestId }
    }
    return {
      status: 'error',
      healthError: ready.error,
      requestId: ready.error.requestId,
    }
  }

  if (health.isSuccess && ready.isSuccess) {
    return { status: 'ready' }
  }

  // 初始 loading
  return { status: 'checking' }
}
