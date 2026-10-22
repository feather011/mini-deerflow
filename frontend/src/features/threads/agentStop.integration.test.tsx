/**
 * R7 集成证据：UI「停止」动作 → 本地 mock 后端收到取消 → run.cancelled 终态。
 *
 * 走的是**真实链路**：AgentChatPanel → useAgentStream → streamClient(fetch)
 * → 本地 mock 后端；mock 后端在收到 POST /runs/stop 后推送 run.cancelled。
 * 不接任何付费模型。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { userEvent } from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { AgentChatPanel } from './AgentChatPanel'
import type { ThreadDetail } from '../../shared/types/threads'

// 辅助 Hook 的网络调用不走真实后端（本测试只关心 stop 链路）
vi.mock('./contextUsageApi', () => ({
  fetchContextUsage: vi.fn().mockResolvedValue(null),
}))

vi.mock('../../shared/api/confirmations', () => ({
  listConfirmations: vi.fn().mockResolvedValue([]),
  getActiveRun: vi.fn().mockResolvedValue({ run_id: null }),
  getConfirmation: vi.fn().mockResolvedValue(null),
  approveConfirmation: vi.fn().mockResolvedValue(null),
  denyConfirmation: vi.fn().mockResolvedValue(null),
  cancelConfirmation: vi.fn().mockResolvedValue(null),
  executeSnapshot: vi.fn().mockResolvedValue(null),
  executeConfirmation: vi.fn().mockResolvedValue(null),
}))

const THREAD_ID = 't-stop-1'

/** 本地 mock 后端：记录 stop 请求，并在收到后推送 run.cancelled */
class MockBackend {
  stopRequests: Array<{ url: string; body: unknown }> = []
  private pushSse: ((chunk: string) => void) | null = null
  private encoder = new TextEncoder()

  /** SSE 事件块（与后端 encode_sse 同格式） */
  sseChunk(eventType: string, sequence: number, extra: Record<string, unknown> = {}): string {
    const data = JSON.stringify({
      run_id: 'run-mock-1',
      thread_id: THREAD_ID,
      event_type: eventType,
      sequence,
      timestamp: 1000 + sequence,
      ...extra,
    })
    return `id: ${sequence}\nevent: ${eventType}\ndata: ${data}\n\n`
  }

  install(): void {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/runs/stop')) {
        let body: unknown = null
        try {
          body = init?.body ? JSON.parse(String(init.body)) : null
        } catch {
          body = null
        }
        this.stopRequests.push({ url, body })
        // 后端受理取消 → 推 run.cancelled 终态
        this.pushSse?.(
          this.sseChunk('run.cancelled', 2, { answer: null, error: null }),
        )
        return new Response(JSON.stringify({ status: 'stopped', thread_id: THREAD_ID }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      if (url.includes('/runs/stream')) {
        const body = new ReadableStream({
          start: (controller) => {
            this.pushSse = (chunk: string) => controller.enqueue(this.encoder.encode(chunk))
          },
        })
        const headers = new Headers({
          'Content-Type': 'text/event-stream',
          'X-Run-ID': 'run-mock-1',
        })
        const response = new Response(body, { status: 200, headers })
        // run.started 立即下发
        queueMicrotask(() => {
          this.pushSse?.(this.sseChunk('run.started', 1))
        })
        return response
      }
      // 其余请求一律 200 空 JSON（本测试不关心）
      return new Response('{}', {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    })
    vi.stubGlobal('fetch', fetchMock)
  }

  cleanup(): void {
    vi.unstubAllGlobals()
  }
}

describe('AgentChatPanel 停止（真取消）集成', () => {
  const backend = new MockBackend()
  let queryClient: QueryClient

  const thread: ThreadDetail = {
    thread_id: THREAD_ID,
    messages: [],
  }

  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )

  beforeEach(() => {
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    backend.stopRequests = []
    backend.install()
  })

  afterEach(() => {
    backend.cleanup()
  })

  it('点击停止 → 后端收到取消 → 终态 run.cancelled → 界面显示已取消', async () => {
    const user = userEvent.setup()
    render(<AgentChatPanel thread={thread} />, { wrapper })

    // 发送一条消息，建立 SSE 流
    await user.type(screen.getByRole('textbox'), '跑个任务')
    await user.click(screen.getByRole('button', { name: '发送消息' }))

    // 运行中出现「停止」按钮
    const stopBtn = await screen.findByRole('button', { name: '停止当前任务' })
    await user.click(stopBtn)

    // 1) 后端确实收到了取消请求（绑定当前 thread）
    await waitFor(() => {
      expect(backend.stopRequests.length).toBe(1)
    })
    const stopRequest = backend.stopRequests[0]
    expect(stopRequest).toBeDefined()
    expect(stopRequest?.url).toContain(
      `/api/threads/${THREAD_ID}/runs/stop`,
    )

    // 2) 后端推来的 run.cancelled 终态被如实呈现
    await waitFor(() => {
      expect(screen.getByText('已取消当前任务')).toBeInTheDocument()
    })
  })

  it('取消请求失败时如实提示"后端任务可能仍在运行"', async () => {
    const user = userEvent.setup()
    // 让 stop 接口返回 404（后端没有正在运行的 Run）
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        if (url.includes('/runs/stop')) {
          return new Response(JSON.stringify({ detail: '没有正在运行的 Run' }), {
            status: 404,
            headers: { 'Content-Type': 'application/json' },
          })
        }
        if (url.includes('/runs/stream')) {
          const encoder = new TextEncoder()
          const body = new ReadableStream({
            start: (controller) => {
              controller.enqueue(
                encoder.encode(
                  `id: 1\nevent: run.started\ndata: ${JSON.stringify({
                    run_id: 'r',
                    thread_id: THREAD_ID,
                    event_type: 'run.started',
                    sequence: 1,
                    timestamp: 1,
                  })}\n\n`,
                ),
              )
            },
          })
          return new Response(body, {
            status: 200,
            headers: { 'Content-Type': 'text/event-stream', 'X-Run-ID': 'r' },
          })
        }
        return new Response('{}', {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }),
    )

    render(<AgentChatPanel thread={thread} />, { wrapper })

    await user.type(screen.getByRole('textbox'), '跑个任务')
    await user.click(screen.getByRole('button', { name: '发送消息' }))

    const stopBtn = await screen.findByRole('button', { name: '停止当前任务' })
    await user.click(stopBtn)

    // 如实报告失败：不得显示"已取消"
    await waitFor(() => {
      expect(screen.getByText('取消失败')).toBeInTheDocument()
    })
    expect(screen.getByText(/后端任务可能仍在运行/)).toBeInTheDocument()
    expect(screen.queryByText('已取消当前任务')).not.toBeInTheDocument()
  })
})
