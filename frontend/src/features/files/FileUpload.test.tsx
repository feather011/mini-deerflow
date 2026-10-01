/**
 * FileUpload 组件测试（20E）
 *
 * 覆盖：
 * - 未选择文件时按钮 disabled
 * - 选择文件后可点击上传
 * - 上传 pending 时 input 和 button 都 disabled
 * - 上传成功后清空选择
 * - 上传失败保留选择以便重试
 * - 错误信息 + Request ID 显示
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { userEvent } from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import type { ApiError } from '../../shared/api'

// 用 vi.hoisted 共享 mock 状态
const mockState = vi.hoisted(() => ({
  isPending: false,
  isError: false,
  error: null as ApiError | null,
}))

const mockMutateAsync = vi.fn()
const mockReset = vi.fn()

vi.mock('./hooks', () => ({
  useUploadFile: () => ({
    mutateAsync: mockMutateAsync,
    isPending: mockState.isPending,
    isError: mockState.isError,
    error: mockState.error,
    reset: mockReset,
  }),
}))

import { FileUpload } from './FileUpload'

function makeWrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  mockState.isPending = false
  mockState.isError = false
  mockState.error = null
  mockMutateAsync.mockResolvedValue({} as never)
})

describe('FileUpload', () => {
  it('渲染文件选择 input 和上传按钮', () => {
    render(<FileUpload />, { wrapper: makeWrapper() })
    expect(screen.getByLabelText('选择要上传的文件')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '上传' })).toBeInTheDocument()
  })

  it('未选择文件时上传按钮 disabled', () => {
    render(<FileUpload />, { wrapper: makeWrapper() })
    expect(screen.getByRole('button', { name: '上传' })).toBeDisabled()
  })

  it('选择文件后上传按钮启用', async () => {
    const user = userEvent.setup()
    render(<FileUpload />, { wrapper: makeWrapper() })

    const file = new File(['hello'], 'hello.txt', { type: 'text/plain' })
    const input = screen.getByLabelText('选择要上传的文件')
    await user.upload(input, file)

    expect(screen.getByRole('button', { name: '上传' })).not.toBeDisabled()
    expect(screen.getByText(/已选择：hello\.txt/)).toBeInTheDocument()
  })

  it('点击上传调用 mutate 并清空选择', async () => {
    const user = userEvent.setup()
    render(<FileUpload />, { wrapper: makeWrapper() })

    const file = new File(['hi'], 'a.txt', { type: 'text/plain' })
    const input = screen.getByLabelText('选择要上传的文件')
    await user.upload(input, file)
    await user.click(screen.getByRole('button', { name: '上传' }))

    expect(mockMutateAsync).toHaveBeenCalledWith({ file })

    // 等待 mutate 完成后清空选择
    await new Promise((r) => setTimeout(r, 0))
    expect(screen.queryByText(/已选择：/)).not.toBeInTheDocument()
  })

  it('上传失败时保留文件选择以便重试', async () => {
    const user = userEvent.setup()
    mockMutateAsync.mockRejectedValueOnce(new Error('上传失败'))

    render(<FileUpload />, { wrapper: makeWrapper() })

    const file = new File(['x'], 'retry.txt', { type: 'text/plain' })
    const input = screen.getByLabelText('选择要上传的文件')
    await user.upload(input, file)
    await user.click(screen.getByRole('button', { name: '上传' }))

    // 等待 mutate 失败完成
    await new Promise((r) => setTimeout(r, 0))
    expect(screen.getByText(/已选择：retry\.txt/)).toBeInTheDocument()
  })

  it('pending 时按钮文案变为"上传中…"且 disabled', () => {
    mockState.isPending = true
    render(<FileUpload />, { wrapper: makeWrapper() })
    expect(screen.getByRole('button', { name: '上传中…' })).toBeDisabled()
    expect(screen.getByLabelText('选择要上传的文件')).toBeDisabled()
  })

  it('错误状态显示错误信息和 Request ID', () => {
    mockState.isError = true
    mockState.error = {
      name: 'ApiError',
      status: 409,
      code: 'CONFLICT',
      message: '同名文件已存在',
      requestId: 'req-upload-conflict',
      details: null,
    } as ApiError

    render(<FileUpload />, { wrapper: makeWrapper() })

    expect(screen.getByText('同名文件已存在')).toBeInTheDocument()
    expect(screen.getByText(/请求编号：req-upload-conflict/)).toBeInTheDocument()
  })

  it('413 错误显示"文件超出大小限制"', () => {
    mockState.isError = true
    mockState.error = {
      name: 'ApiError',
      status: 413,
      code: 'PAYLOAD_TOO_LARGE',
      message: '文件超出大小限制',
      requestId: undefined,
      details: null,
    } as ApiError

    render(<FileUpload />, { wrapper: makeWrapper() })
    expect(screen.getByText('文件超出大小限制')).toBeInTheDocument()
  })
})
