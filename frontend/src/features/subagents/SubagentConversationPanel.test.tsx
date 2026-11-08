/**
 * SubagentConversationPanel — 右侧子 Agent 会话窗口
 *
 * 覆盖：按 ID 直读子代理隔离线程、渲染对话、返回按钮、空对话。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { SubagentConversationPanel } from './SubagentConversationPanel'

const mockGetThread = vi.hoisted(() => vi.fn())
vi.mock('../threads/api', () => ({
  getThread: mockGetThread,
}))

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

describe('SubagentConversationPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockGetThread.mockResolvedValue({
      thread_id: 'subagent-sa-abc123',
      project_id: null,
      last_run_result: null,
      subagent_runs: [],
      messages: [
        { role: 'user', content: '调研这个报错的三个方向' },
        { role: 'assistant', content: '子 Agent 结论：三个方向值得查' },
      ],
    })
  })

  it('按隔离线程 ID 读取子代理的对话', async () => {
    render(
      <SubagentConversationPanel
        taskId="sa-abc123"
        instruction="调研这个报错"
        onBack={() => {}}
      />,
      { wrapper },
    )

    await waitFor(() => {
      expect(mockGetThread).toHaveBeenCalledWith('subagent-sa-abc123', expect.anything())
    })
    expect(await screen.findByText('调研这个报错的三个方向')).toBeInTheDocument()
    expect(await screen.findByText('子 Agent 结论：三个方向值得查')).toBeInTheDocument()
  })

  it('展示指令作为标题', async () => {
    render(
      <SubagentConversationPanel
        taskId="sa-abc123"
        instruction="调研这个报错"
        onBack={() => {}}
      />,
      { wrapper },
    )

    expect(await screen.findByText('调研这个报错')).toBeInTheDocument()
  })

  it('返回按钮触发 onBack', async () => {
    const onBack = vi.fn()
    render(
      <SubagentConversationPanel
        taskId="sa-abc123"
        instruction="调研这个报错"
        onBack={onBack}
      />,
      { wrapper },
    )

    fireEvent.click(await screen.findByRole('button', { name: '返回' }))
    expect(onBack).toHaveBeenCalledTimes(1)
  })

  it('子任务没有对话时给出说明而不是空白', async () => {
    mockGetThread.mockResolvedValue({
      thread_id: 'subagent-sa-empty',
      messages: [],
    })
    render(
      <SubagentConversationPanel
        taskId="sa-empty"
        instruction="空任务"
        onBack={() => {}}
      />,
      { wrapper },
    )

    expect(
      await screen.findByText('这个子任务还没有留下对话记录。'),
    ).toBeInTheDocument()
  })
})
