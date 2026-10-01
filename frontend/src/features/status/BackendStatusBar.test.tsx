/**
 * BackendStatusBar 组件测试
 *
 * 覆盖各状态下的界面渲染：连接中、就绪、启动中、离线、异常
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { BackendStatusBar } from './BackendStatusBar'

// mock useBackendStatus，返回指定状态
vi.mock('./useBackendStatus', () => ({
  useBackendStatus: vi.fn(),
}))
import { useBackendStatus } from './useBackendStatus'

const mock = vi.mocked(useBackendStatus)

function renderBar() {
  render(<BackendStatusBar />)
}

describe('BackendStatusBar — 各状态文案', () => {
  it('显示"连接中…"', () => {
    mock.mockReturnValueOnce({ status: 'checking', requestId: undefined })
    renderBar()
    expect(screen.getByText('连接中…')).toBeInTheDocument()
  })

  it('显示"就绪"且无重试按钮', () => {
    mock.mockReturnValueOnce({ status: 'ready', requestId: undefined })
    renderBar()
    expect(screen.getByText('就绪')).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('显示"后端启动中…"', () => {
    mock.mockReturnValueOnce({ status: 'not-ready', requestId: undefined })
    renderBar()
    expect(screen.getByText('后端启动中…')).toBeInTheDocument()
  })

  it('显示"后端离线"和重试按钮', () => {
    mock.mockReturnValueOnce({ status: 'offline', requestId: undefined })
    renderBar()
    expect(screen.getByText('后端离线')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument()
  })

  it('显示"后端异常"和重试按钮', () => {
    mock.mockReturnValueOnce({
      status: 'error',
      requestId: 'req-abc123',
    })
    renderBar()
    expect(screen.getByText('后端异常')).toBeInTheDocument()
    expect(screen.getByText('req-abc123')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument()
  })

  it('error 状态显示 Request ID 供调试', () => {
    mock.mockReturnValueOnce({
      status: 'error',
      requestId: 'req-debug-xyz',
    })
    renderBar()
    expect(screen.getByText('req-debug-xyz')).toBeInTheDocument()
  })
})
