/**
 * MessageList 组件测试
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MessageList } from './MessageList'
import type { MessageResponse } from '../../shared/types/threads'

describe('MessageList', () => {
  it('空消息列表显示提示文案', () => {
    render(<MessageList messages={[]} />)
    expect(screen.getByText('还没有消息，发送第一条消息开始对话')).toBeInTheDocument()
  })

  it('单条 user 消息显示角色标签和内容', () => {
    const messages: MessageResponse[] = [
      { role: 'user', content: 'Hello' },
    ]
    render(<MessageList messages={messages} />)
    expect(screen.getByText('你')).toBeInTheDocument()
    expect(screen.getByText('Hello')).toBeInTheDocument()
  })

  it('单条 assistant 消息显示 Agent 标签', () => {
    const messages: MessageResponse[] = [
      { role: 'assistant', content: 'Hi there!' },
    ]
    render(<MessageList messages={messages} />)
    expect(screen.getByText('Agent')).toBeInTheDocument()
    expect(screen.getByText('Hi there!')).toBeInTheDocument()
  })

  it('多条消息按顺序展示', () => {
    const messages: MessageResponse[] = [
      { role: 'user', content: 'First' },
      { role: 'assistant', content: 'Second' },
      { role: 'user', content: 'Third' },
    ]
    render(<MessageList messages={messages} />)

    // 验证顺序：First 出现在 Second 之前
    expect(screen.getByText('First')).toBeInTheDocument()
  })

  it('换行符正确渲染为多行', () => {
    const messages: MessageResponse[] = [
      { role: 'user', content: 'Line 1\nLine 2\nLine 3' },
    ]
    render(<MessageList messages={messages} />)
    expect(screen.getByText('Line 1')).toBeInTheDocument()
    expect(screen.getByText('Line 2')).toBeInTheDocument()
    expect(screen.getByText('Line 3')).toBeInTheDocument()
  })

  it('纯文本安全渲染，不使用 dangerouslySetInnerHTML', () => {
    const messages: MessageResponse[] = [
      { role: 'user', content: '<script>alert("xss")</script>' },
    ]
    render(<MessageList messages={messages} />)
    // 内容应作为纯文本显示，而不是作为 HTML 执行
    expect(screen.getByText('<script>alert("xss")</script>')).toBeInTheDocument()
    expect(document.querySelector('script')).toBeNull()
  })

  it('特殊字符正确转义', () => {
    const messages: MessageResponse[] = [
      { role: 'user', content: 'Hello &amp; goodbye' },
    ]
    render(<MessageList messages={messages} />)
    // 纯文本显示原始内容
    expect(screen.getByText('Hello &amp; goodbye')).toBeInTheDocument()
  })

  it('超长消息正常显示', () => {
    const longContent = 'A'.repeat(10000)
    const messages: MessageResponse[] = [
      { role: 'user', content: longContent },
    ]
    render(<MessageList messages={messages} />)
    expect(screen.getByText(longContent)).toBeInTheDocument()
  })

  it('user 消息有正确的 role 属性', () => {
    const messages: MessageResponse[] = [
      { role: 'user', content: 'test' },
    ]
    render(<MessageList messages={messages} />)
    const msgEl = screen.getByText('test').closest('[data-role]')
    expect(msgEl).toHaveAttribute('data-role', 'user')
  })

  it('assistant 消息有正确的 role 属性', () => {
    const messages: MessageResponse[] = [
      { role: 'assistant', content: 'response' },
    ]
    render(<MessageList messages={messages} />)
    const msgEl = screen.getByText('response').closest('[data-role]')
    expect(msgEl).toHaveAttribute('data-role', 'assistant')
  })

  it('tool 角色消息正常显示', () => {
    const messages: MessageResponse[] = [
      { role: 'tool', content: 'tool result' },
    ]
    render(<MessageList messages={messages} />)
    expect(screen.getByText('tool')).toBeInTheDocument()
    expect(screen.getByText('tool result')).toBeInTheDocument()
  })
})
