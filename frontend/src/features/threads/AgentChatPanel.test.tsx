/**
 * AgentChatPanel 组件测试（20D + UX-1 状态分离）
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { userEvent } from '@testing-library/user-event'
import { AgentChatPanel } from './AgentChatPanel'
import { TaskInspector } from '../inspector/TaskInspector'
import { clearRealTaskData, realTaskAdapter } from '../inspector/realTaskAdapter'
import type { StreamStatus } from './useAgentStream'
import type { ThreadDetail } from '../../shared/types/threads'

// Mock useAgentStream
const mockUseAgentStream = vi.hoisted(() => vi.fn(() => ({
  status: 'idle' as StreamStatus,
  events: [],
  // runId 需要能容纳字符串（冷启动时为 null，新 run 时有值）
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
      cancelRun: vi.fn(),
      cancelState: 'idle' as const,
      cancelError: null,
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

  // -------------------------------------------------------------------------
  // R7：停止按钮（真取消入口）在**可见的聊天界面**上
  // -------------------------------------------------------------------------

  it('运行中显示"停止"按钮', () => {
    mockUseAgentStream.mockReturnValue({
      ...mockUseAgentStream(),
      status: 'streaming' as const,
    })

    render(<AgentChatPanel thread={emptyThread} />)

    expect(screen.getByRole('button', { name: '停止当前任务' })).toBeInTheDocument()
  })

  it('空闲时不显示"停止"按钮', () => {
    render(<AgentChatPanel thread={emptyThread} />)
    expect(screen.queryByRole('button', { name: '停止当前任务' })).not.toBeInTheDocument()
  })

  it('点击"停止"调用 cancelRun（真取消，不是仅断流）', async () => {
    const user = userEvent.setup()
    const cancelRun = vi.fn()

    mockUseAgentStream.mockReturnValue({
      ...mockUseAgentStream(),
      status: 'streaming' as const,
      cancelRun,
    })

    render(<AgentChatPanel thread={emptyThread} />)
    await user.click(screen.getByRole('button', { name: '停止当前任务' }))

    expect(cancelRun).toHaveBeenCalledTimes(1)
  })

  it('取消进行中：按钮禁用并显示"正在停止…"', () => {
    mockUseAgentStream.mockReturnValue({
      ...mockUseAgentStream(),
      status: 'streaming' as const,
      cancelState: 'cancelling' as const,
    })

    render(<AgentChatPanel thread={emptyThread} />)

    const stopBtn = screen.getByRole('button', { name: '停止当前任务' })
    expect(stopBtn).toBeDisabled()
    expect(stopBtn.textContent).toContain('正在停止')
  })

  it('取消失败：如实提示后端任务可能仍在运行（不谎称已停止）', () => {
    mockUseAgentStream.mockReturnValue({
      ...mockUseAgentStream(),
      status: 'streaming' as const,
      cancelState: 'failed' as const,
      cancelError: '后端没有正在运行的 Run（可能已结束）',
    })

    render(<AgentChatPanel thread={emptyThread} />)

    expect(screen.getByText('取消失败')).toBeInTheDocument()
    expect(screen.getByText(/后端任务可能仍在运行/)).toBeInTheDocument()
    expect(screen.queryByText('已取消当前任务')).not.toBeInTheDocument()
  })

  it('后端确认取消后显示"已取消当前任务"', () => {
    mockUseAgentStream.mockReturnValue({
      ...mockUseAgentStream(),
      status: 'cancelled' as const,
      cancelState: 'cancelled' as const,
    })

    render(<AgentChatPanel thread={emptyThread} />)

    expect(screen.getByText('已取消当前任务')).toBeInTheDocument()
  })
})

/**
 * F6：冷挂载 / 刷新后恢复已保存的执行结果。
 *
 * 场景：用户跑完一轮真实执行（project_execute），刷新页面。此时
 * - 流是 idle、没有事件、runId 为 null（刷新后没有正在跑的 Run）
 * - thread.last_run_result 带着上一轮的真实结果 JSON
 *
 * 缺陷：恢复 last_run_result 的 effect 排在「建立本会话任务」的 effect
 * 之前，而 `ingestProjectExecuteResult` 在缓存没有该 thread 条目时直接
 * 返回 —— 冷挂载时先恢复就会把保存的结果丢掉，文件与 diff 全部消失。
 *
 * 这些用例**不依赖 StrictMode 的 effect 重放**（开发模式下重放会掩盖顺序
 * 问题），用的是普通挂载。
 */
describe('AgentChatPanel 冷挂载恢复已保存结果', () => {
  const persistedResult = JSON.stringify({
    ok: true,
    status: 'completed',
    files_written: ['hello.c'],
    changes: {
      files: [
        {
          path: 'hello.c',
          status: 'added',
          unified_diff:
            '--- /dev/null\n+++ b/hello.c\n@@ -0,0 +1,3 @@\n+#include <stdio.h>\n+int main(void){return 0;}\n+}',
        },
      ],
    },
  })

  const threadWithPersisted: ThreadDetail = {
    thread_id: 't-restore',
    messages: [{ role: 'user', content: '写一个 hello.c' }],
    last_run_result: persistedResult,
  }

  beforeEach(() => {
    clearRealTaskData()
  })

  afterEach(() => {
    clearRealTaskData()
  })

  it('冷挂载（流 idle、无事件、runId=null）后文件结果不丢', () => {
    render(<AgentChatPanel thread={threadWithPersisted} />)

    const files = realTaskAdapter.getTask('t-restore')?.files
    expect(files).toHaveLength(1)
    expect(files?.[0]?.path).toBe('hello.c')
    expect(files?.[0]?.change).toBe('A')
    expect(files?.[0]?.added).toBe(3)
    expect(files?.[0]?.diff).toHaveLength(3)
  })

  it('冷挂载后 diff 正文保留（消费链：面板里能看到具体内容）', () => {
    render(<AgentChatPanel thread={threadWithPersisted} />)

    render(
      <TaskInspector
        open
        onClose={vi.fn()}
        threadId="t-restore"
        adapter={realTaskAdapter}
      />,
    )

    expect(screen.getByText('hello.c')).toBeInTheDocument()

    const rows = Array.from(document.querySelectorAll('[data-line-type]'))
    // 每行含新旧行号两列 + 正文；只取正文那一段做断言
    const texts = rows.map((r) => (r.lastElementChild?.textContent ?? '').trim())
    expect(rows).toHaveLength(3)
    expect(texts).toEqual([
      '+#include <stdio.h>',
      '+int main(void){return 0;}',
      '+}',
    ])
  })

  it('冷挂载后验证状态保留（不谎报通过，也不谎报未推送）', () => {
    render(<AgentChatPanel thread={threadWithPersisted} />)

    const validation = realTaskAdapter.getTask('t-restore')?.validation
    expect(validation?.status).toBe('not_run')
    expect(validation?.command).toBeNull()
    expect(validation?.output).toBeNull()
  })

  it('开始新 run 时才清空上一轮结果（新 run 正确重置）', () => {
    const { rerender } = render(<AgentChatPanel thread={threadWithPersisted} />)
    expect(realTaskAdapter.getTask('t-restore')?.files).toHaveLength(1)

    // 新一轮 run：runId 出现且与旧值不同
    mockUseAgentStream.mockReturnValue({
      ...mockUseAgentStream(),
      status: 'streaming' as const,
      runId: 'run-new',
    })
    rerender(<AgentChatPanel thread={threadWithPersisted} />)

    expect(realTaskAdapter.getTask('t-restore')?.files).toEqual([])
  })

  it('跨会话隔离：另一个会话的保存结果不串到本会话', () => {
    const otherThread: ThreadDetail = {
      thread_id: 't-other',
      messages: [{ role: 'user', content: '别的会话' }],
      last_run_result: JSON.stringify({
        ok: true,
        status: 'completed',
        changes: { files: [{ path: 'other.c', status: 'added', unified_diff: '--- /dev/null\n+++ b/other.c\n@@ -0,0 +1,1 @@\n+int x;' }] },
      }),
    }

    render(
      <>
        <AgentChatPanel thread={threadWithPersisted} />
        <AgentChatPanel thread={otherThread} />
      </>,
    )

    expect(realTaskAdapter.getTask('t-restore')?.files[0]?.path).toBe('hello.c')
    expect(realTaskAdapter.getTask('t-other')?.files[0]?.path).toBe('other.c')
  })
})

/**
 * 人工验收返修 · 第 2 项：`processedResultSeq` 必须**按运行**重置。
 *
 * 复现的缺陷：第一轮 run.result 的 sequence 已经走到 30；第二轮后端重新
 * 从 1 计数，`event.sequence > processedResultSeq.current` 恒为 false →
 * 本轮结果被整段跳过，文件和 diff 都不更新。
 */
describe('AgentChatPanel 每轮重置 run.result 的处理序号', () => {
  const thread: ThreadDetail = {
    thread_id: 't-seq',
    messages: [{ role: 'user', content: '第一轮' }],
  }

  const payload = (file: string) =>
    JSON.stringify({
      ok: true,
      status: 'completed',
      changes: {
        files: [
          {
            path: file,
            status: 'added',
            unified_diff: `--- /dev/null\n+++ b/${file}\n@@ -0,0 +1,1 @@\n+int ${file.length};`,
          },
        ],
      },
    })

  const resultEvent = (sequence: number, file: string) => ({
    run_id: 'run-x',
    thread_id: 't-seq',
    event_type: 'run.result' as const,
    sequence,
    timestamp: 1000,
    answer: null,
    error: null,
    attempt: null,
    operation: null,
    tool_name: 'project_execute',
    duration_seconds: null,
    tool_result: payload(file),
  })

  beforeEach(() => {
    clearRealTaskData()
  })

  afterEach(() => {
    clearRealTaskData()
  })

  it('第二轮的 run.result 序号更小也必须被 ingest（不能因上一轮序号大而漏掉）', async () => {
    const user = userEvent.setup()
    const { rerender } = render(<AgentChatPanel thread={thread} />)

    // 第一轮：sequence 已经走到 30
    mockUseAgentStream.mockReturnValue({
      ...mockUseAgentStream(),
      status: 'streaming' as const,
      runId: 'run-1',
      events: [resultEvent(30, 'first.c')],
    } as unknown as ReturnType<typeof mockUseAgentStream>)
    rerender(<AgentChatPanel thread={thread} />)
    expect(realTaskAdapter.getTask('t-seq')?.files.map((f) => f.path)).toEqual(['first.c'])

    // 用户真实点击发送，开启第二轮
    await user.type(screen.getByRole('textbox'), '再来一次')
    await user.click(screen.getByRole('button', { name: '发送消息' }))

    // 第二轮：后端 sequence 从 1 重新计数
    mockUseAgentStream.mockReturnValue({
      ...mockUseAgentStream(),
      status: 'streaming' as const,
      runId: 'run-2',
      events: [resultEvent(1, 'second.c')],
    } as unknown as ReturnType<typeof mockUseAgentStream>)
    rerender(<AgentChatPanel thread={thread} />)

    expect(realTaskAdapter.getTask('t-seq')?.files.map((f) => f.path)).toEqual(['second.c'])
  })
})

/**
 * 人工验收返修 · 第 3 项：「知道了」必须真的能把取消提示关掉。
 *
 * 复现的缺陷：`isCancelled` 是由 streamStatus / cancelState 派生的，
 * `onDismiss` 只动提交状态与草稿，没有任何「已关闭」标记 → 点完按钮，
 * 提示下一帧原样重新渲染。
 */
describe('AgentChatPanel 取消提示可关闭', () => {
  const cancelledStream = (runId: string) =>
    ({
      ...mockUseAgentStream(),
      status: 'cancelled' as const,
      cancelState: 'cancelled' as const,
      runId,
    }) as unknown as ReturnType<typeof mockUseAgentStream>

  const thread: ThreadDetail = { thread_id: 't1', messages: [] }

  beforeEach(() => {
    mockUseAgentStream.mockReturnValue(cancelledStream('run-a'))
  })

  it('后端确认取消时显示提示', () => {
    render(<AgentChatPanel thread={thread} />)
    expect(screen.getByText('已取消当前任务')).toBeInTheDocument()
  })

  it('点击「知道了」后当前提示消失', async () => {
    const user = userEvent.setup()
    render(<AgentChatPanel thread={thread} />)
    expect(screen.getByText('已取消当前任务')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '知道了' }))

    expect(screen.queryByText('已取消当前任务')).toBeNull()
  })

  it('关闭提示不改变后端取消事实（不谎称已完成，也不自动重试）', async () => {
    const user = userEvent.setup()
    const startMock = vi.fn()
    mockUseAgentStream.mockReturnValue({
      ...cancelledStream('run-a'),
      start: startMock,
    })
    const { rerender } = render(<AgentChatPanel thread={thread} />)

    await user.click(screen.getByRole('button', { name: '知道了' }))
    expect(screen.queryByText('已取消当前任务')).toBeNull()

    // 重新渲染时状态仍是 cancelled —— 提示不得自己复活
    rerender(<AgentChatPanel thread={thread} />)
    expect(screen.queryByText('已取消当前任务')).toBeNull()

    // 没有被改成"完成"，也没有触发任何新的发送
    expect(screen.queryByText('Agent 执行完成')).toBeNull()
    expect(startMock).not.toHaveBeenCalled()
  })

  it('新一轮再次被取消时，显示它自己的通知', async () => {
    const user = userEvent.setup()
    const { rerender } = render(<AgentChatPanel thread={thread} />)

    await user.click(screen.getByRole('button', { name: '知道了' }))
    expect(screen.queryByText('已取消当前任务')).toBeNull()

    // 第二次运行也以取消收场
    mockUseAgentStream.mockReturnValue(cancelledStream('run-b'))
    rerender(<AgentChatPanel thread={thread} />)

    expect(screen.getByText('已取消当前任务')).toBeInTheDocument()
  })

  it('「知道了」只关当前运行的提示，不影响其它提示（取消失败提示仍在）', async () => {
    const user = userEvent.setup()
    mockUseAgentStream.mockReturnValue({
      ...cancelledStream('run-a'),
      cancelState: 'failed' as const,
      cancelError: '后端没有正在运行的 Run（可能已结束）',
    })
    render(<AgentChatPanel thread={thread} />)

    expect(screen.getByText('已取消当前任务')).toBeInTheDocument()
    expect(screen.getByText('取消失败')).toBeInTheDocument()

    await user.click(
      within(screen.getByLabelText('已取消提示')).getByRole('button', { name: '知道了' }),
    )

    expect(screen.queryByText('已取消当前任务')).toBeNull()
    expect(screen.getByText('取消失败')).toBeInTheDocument()
  })
})
