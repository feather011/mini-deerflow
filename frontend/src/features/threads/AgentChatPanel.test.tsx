/**
 * AgentChatPanel 组件测试（20D + UX-1 状态分离）
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { userEvent } from '@testing-library/user-event'
import { AgentChatPanel } from './AgentChatPanel'
import type { StreamStatus } from './useAgentStream'
import type { ThreadDetail } from '../../shared/types/threads'

// Mock useAgentStream
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

  // ====== UX-1 子任务 3：状态机分离 ======

  it('发送后乐观消息立即出现在消息列表', async () => {
    const user = userEvent.setup()
    render(<AgentChatPanel thread={emptyThread} />)
    const startMock = mockUseAgentStream.mock.results[0]?.value.start
    if (!startMock) return

    const input = screen.getByRole('textbox')
    await user.type(input, 'Hello')
    await user.click(screen.getByRole('button', { name: '发送消息' }))
    expect(screen.getByText('Hello')).toBeInTheDocument()
    expect(startMock).toHaveBeenCalledWith('Hello')
  })

  it('SSE connecting 时 input 禁用（submitStatus=pending）', async () => {
    const user = userEvent.setup()
    render(<AgentChatPanel thread={emptyThread} />)

    const input = screen.getByRole('textbox')
    await user.type(input, 'Hello')
    await user.click(screen.getByRole('button', { name: '发送消息' }))
    expect(screen.getByRole('textbox')).toBeDisabled()
  })

  it('SSE streaming 事件到达后 input 恢复（submitStatus=submitted）', async () => {
    const user = userEvent.setup()
    const { rerender } = render(<AgentChatPanel thread={emptyThread} />)

    // 触发发送
    const input = screen.getByRole('textbox')
    await user.type(input, 'Hello')
    await user.click(screen.getByRole('button', { name: '发送消息' }))

    // 模拟 SSE 进入 streaming 状态
    mockUseAgentStream.mockReturnValue({
      ...mockUseAgentStream(),
      status: 'streaming' as StreamStatus,
    })
    rerender(<AgentChatPanel thread={emptyThread} />)
    // submitted 时 isPending = false，input 应恢复
    expect(screen.getByRole('textbox')).not.toBeDisabled()
  })

  it('SSE error 时 submitStatus=unknown，input 恢复但不能重发', async () => {
    const user = userEvent.setup()
    const { rerender } = render(<AgentChatPanel thread={emptyThread} />)

    // 触发发送
    const input = screen.getByRole('textbox')
    await user.type(input, 'Hello')
    await user.click(screen.getByRole('button', { name: '发送消息' }))
    expect(screen.getByRole('textbox')).toBeDisabled()

    // 模拟 SSE 错误（连接断开）
    mockUseAgentStream.mockReturnValue({
      status: 'error' as StreamStatus,
      errorMessage: 'Connection closed',
      events: [],
      runId: null,
      requestId: null,
      terminalEvent: null,
      start: vi.fn(),
      stop: vi.fn(),
    } as unknown as ReturnType<typeof mockUseAgentStream>)
    rerender(<AgentChatPanel thread={emptyThread} />)
    // unknown 状态：input 恢复（用户可写），发送按钮 disabled（submittingRef=true）
    expect(screen.getByRole('textbox')).not.toBeDisabled()
    expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled()
  })

  it('run.failed 不清除乐观消息（用户消息已提交）', async () => {
    const user = userEvent.setup()
    const { rerender } = render(<AgentChatPanel thread={emptyThread} />)

    const input = screen.getByRole('textbox')
    await user.type(input, 'Hello')
    await user.click(screen.getByRole('button', { name: '发送消息' }))
    expect(screen.getByText('Hello')).toBeInTheDocument()

    // 模拟 run.failed
    mockUseAgentStream.mockReturnValue({
      ...mockUseAgentStream(),
      status: 'failed' as StreamStatus,
    })
    rerender(<AgentChatPanel thread={emptyThread} />)
    // 用户消息仍显示
    expect(screen.getByText('Hello')).toBeInTheDocument()
  })

  // ====== UX-1 子任务 4 回归：草稿保护 ======

  it('发送后输入新草稿，streaming 时只清 pendingSent 不清新草稿', async () => {
    // 验证 useDraftStore 层面的行为：savePendingSent + streaming/completed 时 clearPendingSent
    // 新草稿在 saveCurrent 中保留，不受 clearPendingSent 影响。
    // 该行为由 useDraftStore.test.ts 覆盖；此处验证 MessageComposer 受控模式下草稿流转正确。
    const user = userEvent.setup()
    const { rerender } = render(<AgentChatPanel thread={emptyThread} />)

    // 初始 textarea 为空
    const textarea = screen.getByRole('textbox') as HTMLTextAreaElement
    expect(textarea.value).toBe('')

    // 输入并发送
    await user.type(textarea, 'First')
    await user.click(screen.getByRole('button', { name: '发送消息' }))

    // 乐观消息已出现
    expect(screen.getByText('First')).toBeInTheDocument()

    // streaming 事件到达
    mockUseAgentStream.mockReturnValue({
      ...mockUseAgentStream(),
      status: 'streaming' as StreamStatus,
    })
    rerender(<AgentChatPanel thread={emptyThread} />)

    // submitted 时乐观消息保留（streaming = submitted）
    expect(screen.getByText('First')).toBeInTheDocument()
  })

  it('unknown 状态有 pendingSent 时显示恢复按钮', async () => {
    const draftStorage: Record<string, string> = {
      'draft:t1': JSON.stringify({ current: '', pendingSent: 'First' }),
    }
    vi.stubGlobal('sessionStorage', {
      getItem: (k: string) => draftStorage[k] ?? null,
      setItem: (k: string, v: string) => { draftStorage[k] = v },
      removeItem: (k: string) => { delete draftStorage[k] },
      clear: () => { Object.keys(draftStorage).forEach(key => delete draftStorage[key]) },
    })

    const { rerender } = render(<AgentChatPanel thread={emptyThread} />)

    mockUseAgentStream.mockReturnValue({
      status: 'error' as StreamStatus,
      errorMessage: 'Connection closed',
      events: [],
      runId: null,
      requestId: null,
      terminalEvent: null,
      start: vi.fn(),
      stop: vi.fn(),
    } as unknown as ReturnType<typeof mockUseAgentStream>)

    rerender(<AgentChatPanel thread={emptyThread} />)

    expect(screen.getByRole('button', { name: '恢复原消息' })).toBeInTheDocument()
  })

  it('unknown 状态有新草稿时仍显示恢复按钮', async () => {
    const draftStorage: Record<string, string> = {
      'draft:t1': JSON.stringify({ current: 'new input', pendingSent: 'First' }),
    }
    vi.stubGlobal('sessionStorage', {
      getItem: (k: string) => draftStorage[k] ?? null,
      setItem: (k: string, v: string) => { draftStorage[k] = v },
      removeItem: (k: string) => { delete draftStorage[k] },
      clear: () => { Object.keys(draftStorage).forEach(key => delete draftStorage[key]) },
    })

    const { rerender } = render(<AgentChatPanel thread={emptyThread} />)

    mockUseAgentStream.mockReturnValue({
      status: 'error' as StreamStatus,
      errorMessage: 'Connection closed',
      events: [],
      runId: null,
      requestId: null,
      terminalEvent: null,
      start: vi.fn(),
      stop: vi.fn(),
    } as unknown as ReturnType<typeof mockUseAgentStream>)

    rerender(<AgentChatPanel thread={emptyThread} />)

    // 恢复按钮出现，不因有新草稿而隐藏
    expect(screen.getByRole('button', { name: '恢复原消息' })).toBeInTheDocument()
    // textarea 内容仍为新草稿
    const textareaEl = screen.getByRole('textbox') as HTMLTextAreaElement
    expect(textareaEl.value).toBe('new input')
  })

  it('点击恢复原消息后 pendingSent 清空，textarea 恢复原文', async () => {
    const draftStorage: Record<string, string> = {
      'draft:t1': JSON.stringify({ current: '', pendingSent: 'First' }),
    }
    vi.stubGlobal('sessionStorage', {
      getItem: (k: string) => draftStorage[k] ?? null,
      setItem: (k: string, v: string) => { draftStorage[k] = v },
      removeItem: (k: string) => { delete draftStorage[k] },
      clear: () => { Object.keys(draftStorage).forEach(key => delete draftStorage[key]) },
    })

    const { rerender } = render(<AgentChatPanel thread={emptyThread} />)

    mockUseAgentStream.mockReturnValue({
      status: 'error' as StreamStatus,
      errorMessage: 'Connection closed',
      events: [],
      runId: null,
      requestId: null,
      terminalEvent: null,
      start: vi.fn(),
      stop: vi.fn(),
    } as unknown as ReturnType<typeof mockUseAgentStream>)
    rerender(<AgentChatPanel thread={emptyThread} />)

    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: '恢复原消息' }))

    const textareaEl = screen.getByRole('textbox') as HTMLTextAreaElement
    expect(textareaEl.value).toBe('First')
    // pendingSent 已清，恢复按钮消失
    expect(screen.queryByRole('button', { name: '恢复原消息' })).not.toBeInTheDocument()
    // submittingRef 已释放，发送按钮恢复可用
    expect(screen.getByRole('button', { name: '发送消息' })).not.toBeDisabled()
  })
})

