/**
 * AgentChatPanel — 子任务卡渲染（Subagent 首版）
 *
 * 验证 G6 的前端一半：**页面刷新后卡片能恢复**。
 * 刷新后 SSE 事件没了，卡片只能来自线程详情里的持久化记录
 * （`thread.subagent_runs`）—— 本测试覆盖这条路径。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { AgentChatPanel } from '../threads/AgentChatPanel'
import type { StreamStatus } from '../threads/useAgentStream'
import type { ThreadDetail } from '../../shared/types/threads'
import type { RunEvent } from '../../shared/types/events'

const mockUseAgentStream = vi.hoisted(() => vi.fn(() => ({
  status: 'idle' as StreamStatus,
  events: [] as RunEvent[],
  runId: null as string | null,
  errorMessage: null,
  requestId: null,
  terminalEvent: null,
  start: vi.fn(),
  stop: vi.fn(),
  cancelRun: vi.fn(),
  cancelState: 'idle' as 'idle' | 'cancelling' | 'cancelled' | 'failed',
  cancelError: null as string | null,
})))

vi.mock('../threads/useAgentStream', () => ({
  useAgentStream: mockUseAgentStream,
}))

describe('AgentChatPanel — 子任务卡', () => {
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
      cancelRun: vi.fn(),
      cancelState: 'idle' as const,
      cancelError: null,
    })
  })

  it('没有子任务时不渲染卡片', () => {
    const thread: ThreadDetail = { thread_id: 't1', messages: [] }
    render(<AgentChatPanel thread={thread} />)
    expect(screen.queryByTestId('subagent-card-list')).not.toBeInTheDocument()
  })

  it('刷新后从持久化记录恢复子任务通知条（G6 前端一半）', () => {
    // 刷新后的状态：SSE 没有事件，只有 API 重读到的持久化记录
    const thread: ThreadDetail = {
      thread_id: 't1',
      messages: [
        { role: 'user', content: '帮我调研' },
        { role: 'assistant', content: '已完成调研' },
      ],
      subagent_runs: [
        {
          task_id: 'sa-abc123',
          instruction: '调研三个可能的解决方向',
          status: 'SUCCEEDED',
          output: '子 Agent 的结论：三个方向值得查',
          duration_seconds: 2.5,
        },
      ],
    }
    render(<AgentChatPanel thread={thread} />)

    const card = screen.getByTestId('subagent-card')
    expect(card).toBeInTheDocument()
    expect(card).toHaveAttribute('data-status', 'SUCCEEDED')
    expect(screen.getByTestId('subagent-instruction')).toHaveTextContent(
      '调研三个可能的解决方向',
    )
  })

  it('会话里只显示通知：结果正文不进会话卡', () => {
    // 契约（UI 轮）：会话窗口只放一条"调用运行 subagent 的通知"，
    // 子 Agent 的实际对话在右侧面板展示。卡片里不该再铺开结果正文。
    const thread: ThreadDetail = {
      thread_id: 't1',
      messages: [],
      subagent_runs: [
        {
          task_id: 'sa-abc123',
          instruction: '调研三个可能的解决方向',
          status: 'SUCCEEDED',
          output: '子 Agent 的结论：三个方向值得查',
          duration_seconds: 2.5,
        },
      ],
    }
    render(<AgentChatPanel thread={thread} />)

    expect(screen.queryByTestId('subagent-output')).not.toBeInTheDocument()
    expect(screen.getByTestId('subagent-card')).not.toHaveTextContent(
      '子 Agent 的结论',
    )
  })

  it('点击通知条：请求在右侧打开该子 Agent 的会话窗口', () => {
    const onOpenSubagent = vi.fn()
    const thread: ThreadDetail = {
      thread_id: 't1',
      messages: [],
      subagent_runs: [
        {
          task_id: 'sa-abc123',
          instruction: '调研三个可能的解决方向',
          status: 'SUCCEEDED',
          output: '子 Agent 的结论：三个方向值得查',
          duration_seconds: 2.5,
        },
      ],
    }
    render(<AgentChatPanel thread={thread} onOpenSubagent={onOpenSubagent} />)

    fireEvent.click(screen.getByTestId('subagent-card'))
    expect(onOpenSubagent).toHaveBeenCalledTimes(1)
    expect(onOpenSubagent.mock.calls[0]![0]).toMatchObject({
      task_id: 'sa-abc123',
      instruction: '调研三个可能的解决方向',
    })
  })

  it('失败的子任务如实展示失败状态', () => {
    const thread: ThreadDetail = {
      thread_id: 't1',
      messages: [],
      subagent_runs: [
        {
          task_id: 'sa-fail',
          instruction: '会失败的任务',
          status: 'FAILED',
          output: '',
          error_type: 'RuntimeError',
          error_message: '模型炸了',
        },
      ],
    }
    render(<AgentChatPanel thread={thread} />)

    const card = screen.getByTestId('subagent-card')
    expect(card).toHaveAttribute('data-status', 'FAILED')
    expect(screen.getByTestId('subagent-error')).toHaveTextContent('RuntimeError')
  })

  it('本轮 SSE 事件能实时建卡', () => {
    const liveEvents: RunEvent[] = [
      {
        run_id: 'r1',
        thread_id: 't1',
        event_type: 'subagent.started',
        sequence: 1,
        timestamp: 1736000000,
        answer: null,
        error: null,
        attempt: null,
        operation: 'subagent',
        tool_name: null,
        duration_seconds: null,
        subagent_task_id: 'sa-live',
        subagent_status: 'RUNNING',
        subagent_instruction: '实时任务',
      },
    ]
    mockUseAgentStream.mockReturnValue({
      status: 'streaming' as const,
      events: liveEvents,
      runId: 'r1',
      errorMessage: null,
      requestId: null,
      terminalEvent: null,
      start: vi.fn(),
      stop: vi.fn(),
      cancelRun: vi.fn(),
      cancelState: 'idle' as const,
      cancelError: null,
    })
    const thread: ThreadDetail = { thread_id: 't1', messages: [] }
    render(<AgentChatPanel thread={thread} />)

    const card = screen.getByTestId('subagent-card')
    expect(card).toHaveAttribute('data-status', 'RUNNING')
    expect(screen.getByTestId('subagent-instruction')).toHaveTextContent('实时任务')
  })
})
