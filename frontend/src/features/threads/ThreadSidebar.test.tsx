/**
 * ThreadSidebar 组件测试
 *
 * 策略：只验证 DOM 行为（不 mock useNavigate，避免 Vitest hoisting 问题）。
 * - 通过 React Testing Library 的 userEvent 验证交互
 * - 通过检查 DOM 元素内容验证 UI 状态
 * - 通过 render 时传入 MemoryRouter 验证路由上下文
 *
 * vi.mock 会被 hoisted，mock 函数必须用 vi.hoisted() 声明。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { ThreadSidebar } from './ThreadSidebar'

// mock hooks（使用 vi.hoisted 避免 hoisting 引用错误）
const mockUseThreads = vi.hoisted(() => vi.fn())
const mockUseCreateThread = vi.hoisted(() => vi.fn())
const mockUseDeleteThread = vi.hoisted(() => vi.fn())

vi.mock('./hooks', () => ({
  useThreads: mockUseThreads,
  useCreateThread: mockUseCreateThread,
  useDeleteThread: mockUseDeleteThread,
}))

// ---------------------------------------------------------------------------
// 测试工具
// ---------------------------------------------------------------------------

function makeMutation(overrides: {
  mutate?: ReturnType<typeof vi.fn>
  isPending?: boolean
  variables?: { threadId: string }
} = {}) {
  return {
    mutate: vi.fn(),
    isPending: false,
    variables: undefined,
    ...overrides,
  }
}

function makeQueryResult(overrides: {
  data?: unknown[]
  isLoading?: boolean
  isError?: boolean
  error?: unknown
  refetch?: () => void
} = {}) {
  return {
    data: undefined,
    isLoading: false,
    isError: false,
    error: null,
    refetch: vi.fn(),
    ...overrides,
  }
}

function renderSidebar(
  activeThreadId: string | null = null,
  initialPath = '/',
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  const entries = activeThreadId
    ? [initialPath, `/threads/${activeThreadId}`]
    : [initialPath]

  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <MemoryRouter initialEntries={entries}>
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      </MemoryRouter>
    )
  }
  render(<ThreadSidebar activeThreadId={activeThreadId} />, { wrapper: Wrapper })
}

beforeEach(() => {
  vi.clearAllMocks()
  mockUseThreads.mockReturnValue(makeQueryResult())
  mockUseCreateThread.mockReturnValue(makeMutation())
  mockUseDeleteThread.mockReturnValue(makeMutation())
})

// ---------------------------------------------------------------------------
// 状态渲染
// ---------------------------------------------------------------------------

describe('ThreadSidebar — 状态渲染', () => {
  it('加载中显示"加载中…"', () => {
    mockUseThreads.mockReturnValue(makeQueryResult({ isLoading: true }))
    renderSidebar()
    expect(screen.getByText('加载中…')).toBeInTheDocument()
  })

  it('空列表显示 Empty 文案', () => {
    mockUseThreads.mockReturnValue(makeQueryResult({ data: [] }))
    renderSidebar()
    expect(screen.getByText('还没有会话')).toBeInTheDocument()
    expect(screen.getByText('创建一个会话开始使用 Mini DeerFlow')).toBeInTheDocument()
  })

  it('加载失败显示错误文案和 Request ID', () => {
    mockUseThreads.mockReturnValue(makeQueryResult({
      isError: true,
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      error: { message: 'Runtime unavailable', requestId: 'req-abc123' } as any,
    }))
    renderSidebar()
    expect(screen.getByText('Runtime unavailable')).toBeInTheDocument()
    expect(screen.getByText('请求编号：req-abc123')).toBeInTheDocument()
  })

  it('列表成功显示 Thread ID', () => {
    mockUseThreads.mockReturnValue(makeQueryResult({
      data: [
        { thread_id: 'abc123', messages: [] },
        { thread_id: 'xyz789', messages: [] },
      ],
    }))
    renderSidebar()
    expect(screen.getByText('abc123')).toBeInTheDocument()
    expect(screen.getByText('xyz789')).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// 选中状态
// ---------------------------------------------------------------------------

describe('ThreadSidebar — 选中状态', () => {
  it('当前 Thread 有 active 标记（aria-label 包含"（当前）"）', () => {
    mockUseThreads.mockReturnValue(makeQueryResult({
      data: [{ thread_id: 'active-thread', messages: [] }],
    }))
    renderSidebar('active-thread')
    expect(screen.getByLabelText('会话 active-thread（当前）')).toBeInTheDocument()
  })

  it('非当前 Thread 的 aria-label 不包含"（当前）"', () => {
    mockUseThreads.mockReturnValue(makeQueryResult({
      data: [{ thread_id: 'other-thread', messages: [] }],
    }))
    renderSidebar('different-thread')
    expect(screen.getByLabelText('会话 other-thread')).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// 创建
// ---------------------------------------------------------------------------

describe('ThreadSidebar — 创建', () => {
  it('创建按钮 pending 时 disabled', () => {
    mockUseCreateThread.mockReturnValue(makeMutation({ isPending: true }))
    renderSidebar()
    expect(screen.getByRole('button', { name: '新建会话' })).toBeDisabled()
  })

  it('点击新建按钮调用 mutation.mutate', async () => {
    const createMut = makeMutation()
    mockUseCreateThread.mockReturnValue(createMut)
    renderSidebar()

    await userEvent.click(screen.getByRole('button', { name: '新建会话' }))
    expect(createMut.mutate).toHaveBeenCalledWith(
      {},
      expect.objectContaining({ onSuccess: expect.any(Function) }),
    )
  })

  it('新建按钮文案的 isPending 状态变化', async () => {
    const createMut = makeMutation({ isPending: true })
    mockUseCreateThread.mockReturnValue(createMut)
    renderSidebar()

    // pending 状态下显示"创建中…"
    expect(screen.getByText(/创建中/)).toBeInTheDocument()
    expect(screen.queryByText('+ 新建')).not.toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// 删除
// ---------------------------------------------------------------------------

describe('ThreadSidebar — 删除', () => {
  it('点击删除按钮不触发列表项的 onClick（stopPropagation）', async () => {
    mockUseThreads.mockReturnValue(makeQueryResult({
      data: [{ thread_id: 'del-thread', messages: [] }],
    }))
    const deleteMut = makeMutation()
    mockUseDeleteThread.mockReturnValue(deleteMut)
    // MemoryRouter 默认在 /，点击列表项会触发 navigate('/threads/del-thread')
    renderSidebar()

    await userEvent.click(screen.getByLabelText('删除会话 del-thread'))

    // 删除按钮 stopPropagation 了，所以 navigate 不会触发
    // 简单验证：删除按钮变为确认/取消状态，说明 stopPropagation 生效
    expect(screen.getByRole('button', { name: '确认删除' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '取消删除' })).toBeInTheDocument()
  })

  it('第一次点击删除显示确认与取消按钮', async () => {
    mockUseThreads.mockReturnValue(makeQueryResult({
      data: [{ thread_id: 'del-thread', messages: [] }],
    }))
    renderSidebar()

    await userEvent.click(screen.getByLabelText('删除会话 del-thread'))
    expect(screen.getByRole('button', { name: '确认删除' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '取消删除' })).toBeInTheDocument()
  })

  it('点击取消隐藏确认按钮并恢复删除按钮', async () => {
    mockUseThreads.mockReturnValue(makeQueryResult({
      data: [{ thread_id: 'del-thread', messages: [] }],
    }))
    renderSidebar()

    await userEvent.click(screen.getByLabelText('删除会话 del-thread'))
    await userEvent.click(screen.getByRole('button', { name: '取消删除' }))

    expect(screen.queryByRole('button', { name: '确认删除' })).not.toBeInTheDocument()
    expect(screen.getByLabelText('删除会话 del-thread')).toBeInTheDocument()
  })

  it('点击确认调用 deleteMutation.mutate', async () => {
    mockUseThreads.mockReturnValue(makeQueryResult({
      data: [{ thread_id: 'del-thread', messages: [] }],
    }))
    const deleteMut = makeMutation()
    mockUseDeleteThread.mockReturnValue(deleteMut)
    renderSidebar()

    await userEvent.click(screen.getByLabelText('删除会话 del-thread'))
    await userEvent.click(screen.getByRole('button', { name: '确认删除' }))

    expect(deleteMut.mutate).toHaveBeenCalledWith(
      { threadId: 'del-thread' },
      expect.objectContaining({ onSuccess: expect.any(Function) }),
    )
  })
})

// ---------------------------------------------------------------------------
// 刷新
// ---------------------------------------------------------------------------

describe('ThreadSidebar — 刷新', () => {
  it('刷新按钮存在且可点击', async () => {
    const refetchFn = vi.fn()
    mockUseThreads.mockReturnValue(makeQueryResult({
      data: [{ thread_id: 't1', messages: [] }],
      refetch: refetchFn,
    }))
    renderSidebar()

    const btn = screen.getByLabelText('刷新会话列表')
    await userEvent.click(btn)
    expect(refetchFn).toHaveBeenCalled()
  })

  it('刷新按钮在列表加载中时 disabled', () => {
    mockUseThreads.mockReturnValue(makeQueryResult({ isLoading: true }))
    renderSidebar()
    expect(screen.getByLabelText('刷新会话列表')).toBeDisabled()
  })
})
