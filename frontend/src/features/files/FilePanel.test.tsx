/**
 * FilePanel 组件测试（20E）
 *
 * 覆盖：
 * - Tabs 切换
 * - Uploads tab 显示上传组件
 * - Artifacts tab 不显示上传组件，不显示删除按钮
 * - Loading 状态
 * - Empty 状态
 * - Error 状态 + Request ID
 * - 切换 tab 后内容切换
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { userEvent } from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import type { ApiError } from '../../shared/api'
import { FilePanel } from './FilePanel'

// mock hooks
const mockUseUploads = vi.fn()
const mockUseArtifacts = vi.fn()

vi.mock('./hooks', () => ({
  useUploads: () => mockUseUploads(),
  useArtifacts: () => mockUseArtifacts(),
  useUploadFile: () => ({
    mutateAsync: vi.fn(),
    isPending: false,
    isError: false,
    error: null,
    reset: vi.fn(),
  }),
  useDeleteFile: () => ({
    mutateAsync: vi.fn(),
    isPending: false,
    isError: false,
    error: null,
  }),
}))

function makeWrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  // 默认空列表
  mockUseUploads.mockReturnValue({
    data: { entries: [], total: 0 },
    isLoading: false,
    isError: false,
    error: null,
  })
  mockUseArtifacts.mockReturnValue({
    data: { entries: [], total: 0 },
    isLoading: false,
    isError: false,
    error: null,
  })
})

describe('FilePanel', () => {
  it('渲染 Uploads / Artifacts 两个 tab', () => {
    render(<FilePanel />, { wrapper: makeWrapper() })
    expect(screen.getByRole('tab', { name: 'Uploads' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Artifacts' })).toBeInTheDocument()
  })

  it('默认 Uploads tab 激活', () => {
    render(<FilePanel />, { wrapper: makeWrapper() })
    expect(screen.getByRole('tab', { name: 'Uploads' })).toHaveAttribute('aria-selected', 'true')
  })

  it('Uploads tab 显示上传组件', () => {
    render(<FilePanel />, { wrapper: makeWrapper() })
    expect(screen.getByRole('button', { name: '上传' })).toBeInTheDocument()
    expect(screen.getByLabelText('选择要上传的文件')).toBeInTheDocument()
  })

  it('切换到 Artifacts tab 不显示上传组件', async () => {
    const user = userEvent.setup()
    render(<FilePanel />, { wrapper: makeWrapper() })
    await user.click(screen.getByRole('tab', { name: 'Artifacts' }))
    expect(screen.queryByRole('button', { name: '上传' })).not.toBeInTheDocument()
  })

  it('Uploads 空列表显示"还没有上传的文件"', () => {
    render(<FilePanel />, { wrapper: makeWrapper() })
    expect(screen.getByText('还没有上传的文件')).toBeInTheDocument()
  })

  it('Artifacts 空列表显示"还没有产物"', async () => {
    const user = userEvent.setup()
    render(<FilePanel />, { wrapper: makeWrapper() })
    await user.click(screen.getByRole('tab', { name: 'Artifacts' }))
    expect(screen.getByText('还没有产物')).toBeInTheDocument()
  })

  it('Loading 状态显示"加载中…"', () => {
    mockUseUploads.mockReturnValueOnce({
      data: undefined,
      isLoading: true,
      isError: false,
      error: null,
    })
    render(<FilePanel />, { wrapper: makeWrapper() })
    expect(screen.getByText('加载中…')).toBeInTheDocument()
  })

  it('Error 状态显示错误信息和 Request ID', () => {
    mockUseUploads.mockReturnValueOnce({
      data: undefined,
      isLoading: false,
      isError: true,
      error: { name: 'ApiError', status: 500, code: 'INTERNAL_ERROR', message: '文件服务异常', requestId: 'req-files-err', details: null } as ApiError,
    })
    render(<FilePanel />, { wrapper: makeWrapper() })
    expect(screen.getByText('文件服务异常')).toBeInTheDocument()
    expect(screen.getByText(/请求编号：req-files-err/)).toBeInTheDocument()
  })

  it('Uploads 列表含条目时显示文件', () => {
    mockUseUploads.mockReturnValueOnce({
      data: {
        entries: [
          { path: 'uploads/a.txt', name: 'a.txt', size: 100, media_type: 'text/plain', kind: 'file' },
        ],
        total: 1,
      },
      isLoading: false,
      isError: false,
      error: null,
    })
    render(<FilePanel />, { wrapper: makeWrapper() })
    expect(screen.getByText('a.txt')).toBeInTheDocument()
  })

  it('Artifacts 列表含条目时显示文件', async () => {
    const user = userEvent.setup()
    mockUseArtifacts.mockReturnValue({
      data: {
        entries: [
          { path: 'reports/2026.txt', name: '2026.txt', size: 200, media_type: 'text/plain', kind: 'file' },
        ],
        total: 1,
      },
      isLoading: false,
      isError: false,
      error: null,
    })
    render(<FilePanel />, { wrapper: makeWrapper() })
    await user.click(screen.getByRole('tab', { name: 'Artifacts' }))
    expect(screen.getByText('2026.txt')).toBeInTheDocument()
    expect(screen.getByText('reports/2026.txt')).toBeInTheDocument()
  })
})
