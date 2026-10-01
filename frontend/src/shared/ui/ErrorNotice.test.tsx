/**
 * ErrorNotice 组件测试（20F）
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { userEvent } from '@testing-library/user-event'
import { ErrorNotice } from './ErrorNotice'
import { ApiError } from '../api/client'

function makeApiError(overrides: Partial<ApiError> = {}): ApiError {
  return new ApiError({
    status: 500,
    code: 'INTERNAL_ERROR',
    message: 'Internal server error',
    ...overrides,
  })
}

describe('ErrorNotice', () => {
  it('显示稳定中文文案（按上下文）', () => {
    render(<ErrorNotice error={makeApiError({ status: 409 })} context="file" />)
    expect(screen.getByText('同名文件已存在')).toBeInTheDocument()
  })

  it('显示错误码', () => {
    render(<ErrorNotice error={makeApiError({ code: 'PAYLOAD_TOO_LARGE' })} />)
    expect(screen.getByText(/错误码：PAYLOAD_TOO_LARGE/)).toBeInTheDocument()
  })

  it('显示 Request ID', () => {
    render(<ErrorNotice error={makeApiError({ requestId: 'req-abc123' })} />)
    expect(screen.getByTestId('error-request-id')).toHaveTextContent('req-abc123')
  })

  it('无 Request ID 时不渲染该行', () => {
    render(<ErrorNotice error={makeApiError()} />)
    expect(screen.queryByTestId('error-request-id')).not.toBeInTheDocument()
  })

  it('无回调时不渲染任何按钮', () => {
    render(<ErrorNotice error={makeApiError()} />)
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('提供 onRetry 时渲染重试按钮并触发', async () => {
    const user = userEvent.setup()
    const onRetry = vi.fn()
    render(<ErrorNotice error={makeApiError()} onRetry={onRetry} />)
    await user.click(screen.getByRole('button', { name: '重试' }))
    expect(onRetry).toHaveBeenCalledTimes(1)
  })

  it('提供 onDismiss 时渲染关闭按钮并触发', async () => {
    const user = userEvent.setup()
    const onDismiss = vi.fn()
    render(<ErrorNotice error={makeApiError()} onDismiss={onDismiss} />)
    await user.click(screen.getByRole('button', { name: '关闭' }))
    expect(onDismiss).toHaveBeenCalledTimes(1)
  })

  it('同时提供时渲染两个按钮', () => {
    render(<ErrorNotice error={makeApiError()} onRetry={vi.fn()} onDismiss={vi.fn()} />)
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '关闭' })).toBeInTheDocument()
  })

  it('支持自定义按钮文案', () => {
    render(
      <ErrorNotice
        error={makeApiError()}
        onRetry={vi.fn()}
        retryLabel="重新加载"
        onDismiss={vi.fn()}
        dismissLabel="知道了"
      />,
    )
    expect(screen.getByRole('button', { name: '重新加载' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '知道了' })).toBeInTheDocument()
  })

  it('展示 title（可选）', () => {
    render(<ErrorNotice error={makeApiError()} title="加载失败" />)
    expect(screen.getByText('加载失败')).toBeInTheDocument()
  })

  it('不渲染泄漏内部信息的后端文案', () => {
    render(
      <ErrorNotice
        error={makeApiError({ status: 418, message: 'KeyError: secret_field' })}
        context="file"
      />,
    )
    expect(screen.queryByText(/KeyError/)).not.toBeInTheDocument()
    expect(screen.getByText('操作失败，请稍后重试')).toBeInTheDocument()
  })

  it('role=alert 便于无障碍读取', () => {
    render(<ErrorNotice error={makeApiError()} ariaLabel="上传错误" />)
    const notice = screen.getByRole('alert')
    expect(notice).toHaveAttribute('aria-label', '上传错误')
  })

  it('warning / info 语气可切换', () => {
    const { rerender } = render(<ErrorNotice error={makeApiError()} tone="warning" />)
    expect(screen.getByTestId('error-notice').className).toContain('warning')
    rerender(<ErrorNotice error={makeApiError()} tone="info" />)
    expect(screen.getByTestId('error-notice').className).toContain('info')
  })
})
