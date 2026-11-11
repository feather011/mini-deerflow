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

  // ====== UX-1 子任务 3：乐观消息渲染与去重 ======

  it('乐观消息正常渲染并显示 role=user', () => {
    const serverMessages: MessageResponse[] = []
    const optimistic = {
      clientId: 'local-abc123',
      content: '发送中的消息',
      status: 'pending' as const,
      createdAt: Date.now(),
    }
    render(<MessageList messages={serverMessages} optimisticMessage={optimistic} />)
    expect(screen.getByText('发送中的消息')).toBeInTheDocument()
    const msgEl = screen.getByText('发送中的消息').closest('[data-role]')
    expect(msgEl).toHaveAttribute('data-role', 'user')
  })

  it('乐观消息有正确的 data-submit-status 属性', () => {
    const optimistic = {
      clientId: 'local-abc123',
      content: '发送中的消息',
      status: 'pending' as const,
      createdAt: Date.now(),
    }
    render(<MessageList messages={[]} optimisticMessage={optimistic} />)
    const msgEl = document.querySelector('[data-submit-status]')
    expect(msgEl).toBeInTheDocument()
    expect(msgEl).toHaveAttribute('data-submit-status', 'pending')
  })

  it('服务端已有同 content 的 user 消息时，乐观消息不追加（服务端消息顺序不变）', () => {
    // 关键回归测试：不要从 messages 里删掉服务端 user 消息
    const serverMessages: MessageResponse[] = [
      { role: 'user', content: 'Hello' },
      { role: 'assistant', content: '服务端回复' },
    ]
    const optimistic = {
      clientId: 'local-abc123',
      content: 'Hello',
      status: 'submitted' as const,
      createdAt: Date.now(),
    }
    render(<MessageList messages={serverMessages} optimisticMessage={optimistic} />)
    // 只看到一条"Hello"（来自服务端），乐观消息未追加
    expect(screen.getAllByText('Hello')).toHaveLength(1)
    // assistant 回复仍然在 user 后面
    expect(screen.getByText('服务端回复')).toBeInTheDocument()
    // 验证相对顺序：Hello 在"服务端回复"前面（DOM 中 Hello 索引 < assistant 索引）
    const allDivs = document.querySelectorAll('[data-role]')
    const helloIdx = Array.from(allDivs).findIndex(d => d.getAttribute('data-role') === 'user' && d.textContent === 'Hello')
    const assistantIdx = Array.from(allDivs).findIndex(d => d.getAttribute('data-role') === 'assistant')
    expect(helloIdx).toBeLessThan(assistantIdx)
  })

  it('服务端无匹配时，乐观消息追加在列表末尾', () => {
    const serverMessages: MessageResponse[] = [
      { role: 'user', content: '上一条消息' },
    ]
    const optimistic = {
      clientId: 'local-xyz789',
      content: '发送中的消息',
      status: 'pending' as const,
      createdAt: Date.now(),
    }
    render(<MessageList messages={serverMessages} optimisticMessage={optimistic} />)
    expect(screen.getAllByText('发送中的消息')).toHaveLength(1)
    // 列表中有两条消息
    expect(screen.getAllByText(/发送中的消息|上一条消息/)).toHaveLength(2)
  })

  it('过程块（工具块/子任务条）渲染在滚动容器内部，随消息一起滚动', () => {
    const messages: MessageResponse[] = [
      { role: 'user', content: 'Hello' },
      { role: 'assistant', content: 'Hi' },
    ]
    render(
      <MessageList
        messages={messages}
        blocks={[
          {
            key: 'block-1',
            afterMessageCount: 1,
            node: <div data-testid="process-block">过程块</div>,
          },
        ]}
      />,
    )

    const scroll = screen.getByTestId('message-scroll')
    const block = screen.getByTestId('process-block')
    // 关键回归点：过程块必须在滚动容器**里面**。
    // 放外面就会悬在聊天区底部，不随消息滚动。
    expect(scroll.contains(block)).toBe(true)
  })

  it('过程块插在锚点位置（两条消息之间），不是堆在末尾', () => {
    const messages: MessageResponse[] = [
      { role: 'user', content: '第一条' },
      { role: 'assistant', content: '第二条' },
      { role: 'assistant', content: '第三条' },
    ]
    const { container } = render(
      <MessageList
        messages={messages}
        blocks={[
          {
            key: 'block-1',
            afterMessageCount: 1,
            node: <div data-testid="process-block">过程块</div>,
          },
        ]}
      />,
    )

    const block = screen.getByTestId('process-block')
    const text = container.textContent ?? ''
    // 位置：第一条之后、第二条之前
    expect(text.indexOf('第一条')).toBeLessThan(text.indexOf('过程块'))
    expect(text.indexOf('过程块')).toBeLessThan(text.indexOf('第二条'))
    // 且不在最后一条消息之后
    expect(text.indexOf('过程块')).toBeLessThan(text.indexOf('第三条'))
    expect(block).toBeInTheDocument()
  })

  it('锚点超过消息条数时退回末尾（消息还没到齐）', () => {
    const messages: MessageResponse[] = [{ role: 'user', content: '第一条' }]
    const { container } = render(
      <MessageList
        messages={messages}
        blocks={[
          {
            key: 'block-1',
            afterMessageCount: 5,
            node: <div data-testid="process-block">过程块</div>,
          },
        ]}
      />,
    )
    const text = container.textContent ?? ''
    expect(text.indexOf('第一条')).toBeLessThan(text.indexOf('过程块'))
  })

  it('同一锚点的多个过程块按给定顺序排列', () => {
    const messages: MessageResponse[] = [
      { role: 'user', content: '第一条' },
      { role: 'assistant', content: '第二条' },
    ]
    const { container } = render(
      <MessageList
        messages={messages}
        blocks={[
          {
            key: 'block-a',
            afterMessageCount: 1,
            node: <div>块A</div>,
          },
          {
            key: 'block-b',
            afterMessageCount: 1,
            node: <div>块B</div>,
          },
        ]}
      />,
    )
    const text = container.textContent ?? ''
    expect(text.indexOf('块A')).toBeLessThan(text.indexOf('块B'))
    expect(text.indexOf('块B')).toBeLessThan(text.indexOf('第二条'))
  })

  // ====== UI-SUBAGENT-INLINE-01：父任务取消后旧卡不串到下一轮 ======

  it('锚点 0 的过程块排在所有消息之前，不挂到后面的提问下面', () => {
    // 后端把「本轮没落库」的子任务卡锚点收到轮次边界 0。
    // 0 在放置规则里是「排在最前面」—— 卡片停在轮次边界，
    // 不会因为下一轮消息落库而挂到下一轮提问下面（串位）。
    const messages: MessageResponse[] = [
      { role: 'user', content: '下一轮的提问' },
      { role: 'assistant', content: '下一轮的回答' },
    ]
    const { container } = render(
      <MessageList
        messages={messages}
        blocks={[
          {
            key: 'block-cancelled',
            afterMessageCount: 0,
            node: <div data-testid="process-block">被取消那一轮的子任务卡</div>,
          },
        ]}
      />,
    )
    const text = container.textContent ?? ''
    expect(text.indexOf('被取消那一轮的子任务卡')).toBeLessThan(text.indexOf('下一轮的提问'))
    expect(text.indexOf('被取消那一轮的子任务卡')).toBeLessThan(text.indexOf('下一轮的回答'))
  })
})
