/**
 * App Shell 组件测试
 *
 * 覆盖：
 * - 渲染三栏骨架
 * - 顶部状态栏存在
 * - ThreadSidebar 渲染（无 Router 包装，App 内部已有）
 * - 右侧占位区渲染
 * - 不调用 20C～20F API
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import App from './App'

// mock health API
vi.mock('../shared/api/health', () => ({
  getHealth: vi.fn().mockResolvedValue({ status: 'ok', service: 'mini-deerflow' }),
  getReady: vi.fn().mockResolvedValue({ status: 'ready', runtime: true }),
}))

// mock Thread API（ThreadSidebar 使用）
vi.mock('../features/threads/api', () => ({
  listThreads: vi.fn().mockResolvedValue({ threads: [] }),
  createThread: vi.fn().mockResolvedValue({ thread_id: 'mock-1', messages: [] }),
  getThread: vi.fn().mockResolvedValue({ thread_id: 'mock-1', messages: [] }),
  deleteThread: vi.fn().mockResolvedValue(undefined),
}))

// mock Files API（FilePanel 使用）
vi.mock('../features/files/api', () => ({
  listUploads: vi.fn().mockResolvedValue({ entries: [], total: 0 }),
  listArtifacts: vi.fn().mockResolvedValue({ entries: [], total: 0 }),
  uploadFile: vi.fn(),
  deleteFile: vi.fn(),
  downloadFile: vi.fn(),
  downloadArtifact: vi.fn(),
  encodeFilePath: vi.fn((p: string) => p),
}))

function makeClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } })
}

function TestWrapper({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={makeClient()}>
      {children}
    </QueryClientProvider>
  )
}

describe('App Shell', () => {
  it('渲染顶部状态栏', () => {
    render(<App />, { wrapper: TestWrapper })
    expect(screen.getByRole('status')).toBeInTheDocument()
  })

  it('渲染 ThreadSidebar（会话标题 + 新建按钮）', () => {
    render(<App />, { wrapper: TestWrapper })
    expect(screen.getByRole('heading', { name: '会话' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '新建会话' })).toBeInTheDocument()
  })

  it('右侧文件面板渲染 Uploads / Artifacts Tabs', () => {
    render(<App />, { wrapper: TestWrapper })
    expect(screen.getByRole('tab', { name: 'Uploads' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Artifacts' })).toBeInTheDocument()
  })
})
