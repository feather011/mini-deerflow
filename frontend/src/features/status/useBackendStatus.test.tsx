/**
 * Backend Status Feature 单元测试
 *
 * 覆盖：
 * - 初始 Loading
 * - health + ready 均成功 → Ready
 * - health 成功、ready 返回 503 → Not Ready
 * - health 网络失败 → Offline
 * - health 返回非 200 → Error
 * - 错误状态含 Request ID
 */
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { useBackendStatus } from './useBackendStatus'
import * as healthModule from '../../shared/api/health'
import type { ApiError } from '../../shared/api/client'

// Mock health API
vi.mock('../../shared/api/health', () => ({
  getHealth: vi.fn(),
  getReady: vi.fn(),
}))

const getHealth = vi.mocked(healthModule.getHealth)
const getReady = vi.mocked(healthModule.getReady)

// ---------------------------------------------------------------------------
// 辅助 Wrapper
// ---------------------------------------------------------------------------
function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
}

// ---------------------------------------------------------------------------
// 测试
// ---------------------------------------------------------------------------

describe('useBackendStatus', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('初始状态为 checking', () => {
    getHealth.mockReturnValue(new Promise(() => {}))
    getReady.mockReturnValue(new Promise(() => {}))

    const { result } = renderHook(() => useBackendStatus(), { wrapper })
    expect(result.current.status).toBe('checking')
  })

  it('health + ready 均成功 → ready', async () => {
    getHealth.mockResolvedValue({ status: 'ok', service: 'mini-deerflow' })
    getReady.mockResolvedValue({ status: 'ready', runtime: true })

    const { result } = renderHook(() => useBackendStatus(), { wrapper })
    await waitFor(() => {
      expect(result.current.status).toBe('ready')
    })
  })

  it('health 成功、ready 返回 503 → not-ready', async () => {
    getHealth.mockResolvedValue({ status: 'ok', service: 'mini-deerflow' })
    const err503: ApiError = {
      name: 'ApiError',
      status: 503,
      code: 'SERVICE_UNAVAILABLE',
      message: 'Runtime not ready',
      requestId: 'req-503',
    } as ApiError
    getReady.mockRejectedValue(err503)

    const { result } = renderHook(() => useBackendStatus(), { wrapper })
    await waitFor(() => {
      expect(result.current.status).toBe('not-ready')
    })
    expect(result.current.requestId).toBe('req-503')
  })

  it('health 网络失败（status=0）→ offline', async () => {
    const networkErr: ApiError = {
      name: 'ApiError',
      status: 0,
      code: 'NETWORK_ERROR',
      message: '无法连接到后端服务',
    } as ApiError
    getHealth.mockRejectedValue(networkErr)

    const { result } = renderHook(() => useBackendStatus(), { wrapper })
    await waitFor(() => {
      expect(result.current.status).toBe('offline')
    })
  })

  it('health 返回非 200 → error', async () => {
    const serverErr: ApiError = {
      name: 'ApiError',
      status: 500,
      code: 'INTERNAL_ERROR',
      message: 'Internal server error',
      requestId: 'req-500',
    } as ApiError
    getHealth.mockRejectedValue(serverErr)

    const { result } = renderHook(() => useBackendStatus(), { wrapper })
    await waitFor(() => {
      expect(result.current.status).toBe('error')
    })
    expect(result.current.requestId).toBe('req-500')
  })
})
