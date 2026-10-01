/**
 * ThreadWorkspace 组件测试（20D）
 *
 * 覆盖各状态下的用户可见界面：
 * - 未选择 Thread
 * - 加载中
 * - 加载成功（显示聊天界面）
 * - 404（会话不存在）
 * - 其他错误
 * - Request ID 展示
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThreadWorkspace } from './ThreadWorkspace'

// mock 函数用 vi.hoisted() 声明，以便在 vi.mock factory 中引用
const mockUseThread = vi.hoisted(() => vi.fn())

vi.mock('./hooks', () => ({
  useThread: mockUseThread,
}))

// Mock AgentChatPanel 因为它依赖 useAgentStream
vi.mock('./AgentChatPanel', () => ({
  AgentChatPanel: () => <div data-testid="agent-chat-panel" />,
}))

function renderWorkspace(threadId: string | null) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={threadId ? [`/threads/${threadId}`] : ['/']}>
        <ThreadWorkspace threadId={threadId} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  mockUseThread.mockReset()
  mockUseThread.mockReturnValue({
    data: undefined,
    isLoading: false,
    isError: false,
    error: null,
  })
})

// ---------------------------------------------------------------------------
// 未选择
// ---------------------------------------------------------------------------

describe('ThreadWorkspace — 未选择', () => {
  it('threadId 为 null 显示引导文案', () => {
    renderWorkspace(null)
    expect(screen.getByText('选择或创建一个会话')).toBeInTheDocument()
    expect(screen.getByText('使用左侧栏管理会话')).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// 加载中
// ---------------------------------------------------------------------------

describe('ThreadWorkspace — 加载中', () => {
  it('显示加载文案', () => {
    mockUseThread.mockReturnValueOnce({
      data: undefined,
      isLoading: true,
      isError: false,
      error: null,
    })
    renderWorkspace('some-id')
    expect(screen.getByText('加载会话…')).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// 加载成功
// ---------------------------------------------------------------------------

describe('ThreadWorkspace — 加载成功', () => {
  it('显示聊天界面', () => {
    mockUseThread.mockReturnValueOnce({
      data: { thread_id: 'abc123', messages: [] },
      isLoading: false,
      isError: false,
      error: null,
    })
    renderWorkspace('abc123')
    expect(screen.getByTestId('agent-chat-panel')).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// 404
// ---------------------------------------------------------------------------

describe('ThreadWorkspace — 404', () => {
  it('显示"会话不存在或已被删除"', () => {
    mockUseThread.mockReturnValueOnce({
      data: undefined,
      isLoading: false,
      isError: true,
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      error: { name: 'ApiError', message: '未知线程', status: 404, code: 'NOT_FOUND', details: null } as any,
    })
    renderWorkspace('nonexistent')
    expect(screen.getByText('会话不存在或已被删除')).toBeInTheDocument()
  })

  it('404 提供返回会话列表按钮', () => {
    mockUseThread.mockReturnValueOnce({
      data: undefined,
      isLoading: false,
      isError: true,
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      error: { name: 'ApiError', message: '未知线程', status: 404, code: 'NOT_FOUND', details: null } as any,
    })
    renderWorkspace('nonexistent')
    expect(screen.getByRole('button', { name: '返回会话列表' })).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// 其他错误
// ---------------------------------------------------------------------------

describe('ThreadWorkspace — 其他错误', () => {
  it('显示稳定错误文案', () => {
    mockUseThread.mockReturnValueOnce({
      data: undefined,
      isLoading: false,
      isError: true,
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      error: { name: 'ApiError', message: 'Runtime unavailable', status: 503, code: 'SERVICE_UNAVAILABLE', details: null } as any,
    })
    renderWorkspace('some-id')
    expect(screen.getByText('加载失败')).toBeInTheDocument()
    // 统一错误展示（20F）：按状态码映射稳定文案，不直接透出后端自然语言
    expect(screen.getByText('会话服务暂不可用')).toBeInTheDocument()
    expect(screen.queryByText('Runtime unavailable')).not.toBeInTheDocument()
  })

  it('存在 Request ID 时展示请求编号', () => {
    mockUseThread.mockReturnValueOnce({
      data: undefined,
      isLoading: false,
      isError: true,
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      error: { name: 'ApiError', message: 'Server error', status: 500, code: 'INTERNAL_ERROR', requestId: 'req-debug-xyz', details: null } as any,
    })
    renderWorkspace('some-id')
    expect(screen.getByText('请求编号：req-debug-xyz')).toBeInTheDocument()
  })
})
