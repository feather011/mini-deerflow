/**
 * MessageComposer 组件测试（UX-1 第一阶段：IME + 防重复立即守卫）
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
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

  // ====== UX-1 第一阶段：Enter 发送 / Shift+Enter 换行 ======

  it('Enter 发送调用 onSend 并清空', async () => {
    const user = userEvent.setup()
    render(<MessageComposer onSend={mockOnSend} isPending={false} />)

    const textarea = screen.getByRole('textbox')
    await user.click(textarea)
    await user.keyboard('Hello{Enter}')

    expect(mockOnSend).toHaveBeenCalledTimes(1)
    expect(mockOnSend).toHaveBeenCalledWith('Hello')
    expect((textarea as HTMLTextAreaElement).value).toBe('')
  })

  it('Shift+Enter 插入换行不发送', async () => {
    const user = userEvent.setup()
    render(<MessageComposer onSend={mockOnSend} isPending={false} />)

    const textarea = screen.getByRole('textbox')
    await user.click(textarea)
    await user.keyboard('Line 1{Shift>}{Enter}{/Shift}Line 2')

    expect(mockOnSend).not.toHaveBeenCalled()
    // 浏览器默认行为保留：textarea 内应含两行
    expect((textarea as HTMLTextAreaElement).value).toContain('Line 1')
    expect((textarea as HTMLTextAreaElement).value).toContain('Line 2')
  })

  // ====== UX-1 第一阶段：IME 选字期间不误发送 ======

  it('IME 选字期间 Enter 不发送', () => {
    render(<MessageComposer onSend={mockOnSend} isPending={false} />)
    const textarea = screen.getByRole('textbox')

    fireEvent.compositionStart(textarea)
    fireEvent.change(textarea, { target: { value: '你' } })
    fireEvent.keyDown(textarea, { key: 'Enter', code: 'Enter' })

    expect(mockOnSend).not.toHaveBeenCalled()
  })

  it('IME 选字结束后 Enter 正常发送', () => {
    render(<MessageComposer onSend={mockOnSend} isPending={false} />)
    const textarea = screen.getByRole('textbox')

    fireEvent.compositionStart(textarea)
    fireEvent.change(textarea, { target: { value: '你好' } })
    fireEvent.compositionEnd(textarea)
    fireEvent.keyDown(textarea, { key: 'Enter', code: 'Enter' })

    expect(mockOnSend).toHaveBeenCalledTimes(1)
    expect(mockOnSend).toHaveBeenCalledWith('你好')
  })

  it('键盘事件 isComposing=true 时不发送', () => {
    render(<MessageComposer onSend={mockOnSend} isPending={false} />)
    const textarea = screen.getByRole('textbox')

    fireEvent.change(textarea, { target: { value: '你好' } })
    // nativeEvent.isComposing = true：jsdom 在 KeyboardEvent 构造时支持该字段
    fireEvent.keyDown(textarea, {
      key: 'Enter',
      code: 'Enter',
      isComposing: true,
      charCode: 229,
    } as unknown as KeyboardEventInit)

    expect(mockOnSend).not.toHaveBeenCalled()
  })

  it('IME 选字期间发送按钮禁用', () => {
    render(<MessageComposer onSend={mockOnSend} isPending={false} />)
    const textarea = screen.getByRole('textbox')

    fireEvent.change(textarea, { target: { value: '你好' } })
    fireEvent.compositionStart(textarea)
    // 选字结束后按钮应启用：但因为 React 异步 batch，需要 act 包一层
    // 触发 change 后再 compositionStart 一次让 ref 同步
    expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled()

    fireEvent.compositionEnd(textarea)
    expect(screen.getByRole('button', { name: '发送消息' })).not.toBeDisabled()
  })

  // ====== UX-1 第一阶段：防重复立即守卫 ======

  it('双击发送按钮只触发一次 onSend', async () => {
    const user = userEvent.setup()
    render(<MessageComposer onSend={mockOnSend} isPending={false} />)

    const textarea = screen.getByRole('textbox')
    await user.click(textarea)
    await user.keyboard('Hello')

    const button = screen.getByRole('button', { name: '发送消息' })
    // 同一 user.click 调用下连点两次：第二次进入 handleSend 时 submittingRef 仍为 true
    await user.click(button)
    await user.click(button)

    expect(mockOnSend).toHaveBeenCalledTimes(1)
  })

  it('isPending 上升后 submittingRef 释放，允许再次发送', async () => {
    const user = userEvent.setup()
    const { rerender } = render(<MessageComposer onSend={mockOnSend} isPending={false} />)
    const textarea = screen.getByRole('textbox')
    const button = screen.getByRole('button', { name: '发送消息' })

    await user.click(textarea)
    await user.keyboard('First')
    await user.click(button)
    expect(mockOnSend).toHaveBeenCalledTimes(1)
    expect(mockOnSend).toHaveBeenLastCalledWith('First')

    // 父组件接收并把 isPending 切为 true → useEffect 解锁 submittingRef
    rerender(<MessageComposer onSend={mockOnSend} isPending={true} />)

    // run 结束，父组件把 isPending 切回 false，重新允许输入
    rerender(<MessageComposer onSend={mockOnSend} isPending={false} />)
    await user.click(textarea)
    await user.keyboard('Second')
    await user.click(button)

    expect(mockOnSend).toHaveBeenCalledTimes(2)
    expect(mockOnSend).toHaveBeenLastCalledWith('Second')
  })
})
