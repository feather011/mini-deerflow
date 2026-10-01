/**
 * AgentChatPanel 组件测试（20D）
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { userEvent } from '@testing-library/user-event'
import { AgentChatPanel } from './AgentChatPanel'
import type { StreamStatus } from './useAgentStream'
import type { ThreadDetail } from '../../shared/types/threads'

// Mock useAgentStream
// status 用 StreamStatus 联合类型，后续 mockReturnValue 才能切换到其他状态
const mockUseAgentStream = vi.hoisted(() => vi.fn(() => ({
  status: 'idle' as StreamStatus,
  events: [],
  runId: null,
  errorMessage: null,
  requestId: null,
  terminalEvent: null,
  start: vi.fn(),
  stop: vi.fn(),
})))

vi.mock('./useAgentStream', () => ({
  useAgentStream: mockUseAgentStream,
}))

describe('AgentChatPanel', () => {
  const emptyThread: ThreadDetail = {
    thread_id: 't1',
    messages: [],
  }

  const threadWithMessages: ThreadDetail = {
    thread_id: 't1',
    messages: [
      { role: 'user', content: 'Hello' },
      { role: 'assistant', content: 'Hi there!' },
    ],
  }

  beforeEach(() => {
    vi.clearAllMocks()
    mockUseAgentStream.mockReturnValue({
      status: 'idle' as const,
      events: [],
      runId: null,
      errorMessage: null,
      requestId: null,
      terminalEvent: null,
      start: vi.fn(),
      stop: vi.fn(),
    })
  })

  it('渲染消息列表和输入框', () => {
    render(<AgentChatPanel thread={emptyThread} />)
    expect(screen.getByRole('textbox')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '发送消息' })).toBeInTheDocument()
  })

  it('空消息列表显示提示', () => {
    render(<AgentChatPanel thread={emptyThread} />)
    expect(screen.getByText('还没有消息，发送第一条消息开始对话')).toBeInTheDocument()
  })

  it('显示历史消息', () => {
    render(<AgentChatPanel thread={threadWithMessages} />)
    expect(screen.getByText('Hello')).toBeInTheDocument()
    expect(screen.getByText('Hi there!')).toBeInTheDocument()
  })

  it('pending 时禁用输入框', async () => {
    const user = userEvent.setup()

    mockUseAgentStream.mockReturnValue({
      ...mockUseAgentStream(),
      status: 'streaming' as const,
    })

    render(<AgentChatPanel thread={emptyThread} />)

    // 输入并发送
    const input = screen.getByRole('textbox')
    await user.type(input, 'hello')
    await user.click(screen.getByRole('button', { name: '发送消息' }))

    expect(screen.getByRole('textbox')).toBeDisabled()
  })

  it('pending 时禁用发送按钮', async () => {
    const user = userEvent.setup()

    mockUseAgentStream.mockReturnValue({
      ...mockUseAgentStream(),
      status: 'streaming' as const,
    })

    render(<AgentChatPanel thread={emptyThread} />)

    // 输入并发送
    const input = screen.getByRole('textbox')
    await user.type(input, 'hello')
    await user.click(screen.getByRole('button', { name: '发送消息' }))

    expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled()
  })
})

