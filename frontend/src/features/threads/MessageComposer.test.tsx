/**
 * MessageComposer 组件测试
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MessageComposer } from './MessageComposer'

describe('MessageComposer', () => {
  const mockOnSend = vi.fn()

  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('渲染输入框和发送按钮', () => {
    render(<MessageComposer onSend={mockOnSend} isPending={false} />)
    expect(screen.getByRole('textbox')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '发送消息' })).toBeInTheDocument()
  })

  it('空白消息发送按钮禁用', () => {
    render(<MessageComposer onSend={mockOnSend} isPending={false} />)
    expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled()
  })

  it('pending 时禁用', () => {
    render(<MessageComposer onSend={mockOnSend} isPending={true} />)
    expect(screen.getByRole('textbox')).toBeDisabled()
    expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled()
  })

  it('pending 时显示运行中占位', () => {
    render(<MessageComposer onSend={mockOnSend} isPending={true} />)
    expect(screen.getByPlaceholderText('Agent 正在运行…')).toBeInTheDocument()
  })

  it('点击发送调用 onSend 并清空', async () => {
    const user = userEvent.setup()
    render(<MessageComposer onSend={mockOnSend} isPending={false} />)

    const textarea = screen.getByRole('textbox')
    await user.click(textarea)
    await user.keyboard('Hello')

    expect(screen.getByRole('button', { name: '发送消息' })).not.toBeDisabled()
  })

  it('字符计数不显示当输入为空', () => {
    render(<MessageComposer onSend={mockOnSend} isPending={false} />)
    expect(screen.queryByText(/\d+ \/ \d+/)).not.toBeInTheDocument()
  })
})
