/**
 * SSE Stream Client（20D）
 *
 * 使用 fetch + ReadableStream 解析 SSE 事件流。
 *
 * 职责：
 * - 与 /api/threads/{thread_id}/runs/stream 建立 POST 连接
 * - 解析 SSE 格式（id: / event: / data:）
 * - 处理 CRLF（\r\n）、跨 chunk 事件、非法 JSON
 * - 从响应头读取 X-Run-ID
 * - 处理 HTTP 非 2xx 错误
 * - 遇到终态事件后立即结束读取
 *
 * 不处理：
 * - 自动重连
 * - token-level streaming
 * - 断线取消 Agent（当前后端不支持）
 */
import { API_BASE_URL } from './config'

// SSE 流不走 Vite proxy：Vite proxy 会缓冲响应导致无法实时推送，
// 直接连接后端（生产环境由 nginx 转发，不走 Vite）。
const SSE_BASE_URL = API_BASE_URL
import type { RunEvent, RunEventType } from '../types/events'

// ---------------------------------------------------------------------------
// 错误类型
// ---------------------------------------------------------------------------

export class StreamError extends Error {
  status: number
  requestId?: string

  constructor(opts: { status: number; message: string; requestId?: string }) {
    super(opts.message)
    this.name = 'StreamError'
    this.status = opts.status
    this.requestId = opts.requestId
  }
}

// ---------------------------------------------------------------------------
// SSE 解析状态机
// ---------------------------------------------------------------------------

/**
 * 将 SSE 文本解析为事件数组
 *
 * 支持：
 * - \n 和 \r\n 分隔行
 * - 多事件在同一个 chunk 中
 * - 注释行（: 开头的行）
 * - 连续两个换行符（\n\n 或 \r\n\r\n）结束一个事件
 *
 * 返回 { events, remaining }：已解析的事件和未完成的部分
 */
function parseSseChunk(
  text: string,
  buffer: string,
): { events: RunEvent[]; remaining: string } {
  const content = buffer + text
  const lines = content.split(/\r?\n/)

  const parsedEvents: RunEvent[] = []
  let remaining = ''

  // 当前正在解析的事件字段
  let currentEvent: {
    id: string
    event: string
    data: string
  } | null = null

  for (const line of lines) {
    // 空行：事件结束
    if (line === '' || line === '\r') {
      if (currentEvent !== null) {
        // 尝试解析 data 为 JSON
        try {
          const eventData = JSON.parse(currentEvent.data) as RunEvent
          parsedEvents.push(eventData)
        } catch {
          // 非法 JSON，跳过该事件
        }
        currentEvent = null
      }
      continue
    }

    // 注释行（: 开头），忽略
    if (line.startsWith(':')) {
      continue
    }

    // 字段行：id: / event: / data:
    const colonIndex = line.indexOf(':')
    if (colonIndex === -1) continue

    const field = line.slice(0, colonIndex).trim()
    // 字段值：跳过冒号后的空格
    const value = line.slice(colonIndex + 1).trim()

    if (field === 'id') {
      if (currentEvent === null) {
        currentEvent = { id: value, event: '', data: '' }
      } else {
        currentEvent.id = value
      }
    } else if (field === 'event') {
      if (currentEvent === null) {
        // event 行也可以创建新事件（不依赖 id 行）
        currentEvent = { id: '', event: value, data: '' }
      } else {
        currentEvent.event = value
      }
    } else if (field === 'data') {
      if (currentEvent === null) {
        currentEvent = { id: '', event: '', data: value }
      } else {
        // data 行可能多行，追加（用换行分隔）
        // 去掉 Windows 行尾的 \r，避免破坏 JSON 解析
        const cleanValue = value.replace(/\r$/, '')
        currentEvent.data = currentEvent.data ? `${currentEvent.data}\n${cleanValue}` : cleanValue
      }
    }
    // 其他字段忽略
  }

  // 如果最后一个事件不完整，保留在 remaining 中
  if (currentEvent !== null) {
    remaining = currentEvent.id + '\nevent: ' + currentEvent.event + '\ndata: ' + currentEvent.data + '\n'
  }

  return { events: parsedEvents, remaining }
}

// ---------------------------------------------------------------------------
// 核心连接函数
// ---------------------------------------------------------------------------

/**
 * SSE 流连接选项
 */
export interface StreamOptions {
  /** 中断信号 */
  signal?: AbortSignal
  /** 收到事件回调（返回 false 可提前结束） */
  onEvent?: (event: RunEvent) => boolean | void
  /** 收到终态事件回调 */
  onTerminal?: (event: RunEvent) => void
  /** HTTP 错误回调 */
  onError?: (error: StreamError) => void
}

/**
 * 建立 SSE 连接并开始接收事件
 *
 * @param threadId  - Thread ID
 * @param message  - 用户消息
 * @param options  - 连接选项
 * @returns 包含 runId 的 Promise，以及一个 cancel 函数（效果同 AbortController.abort）
 */
export function connectAgentStream(
  threadId: string,
  message: string,
  options: StreamOptions = {},
): { runId: Promise<string>; cancel: () => void } {
  const { signal, onEvent, onTerminal, onError } = options

  // Promise executor 同步执行，故 resolve 必在首次使用前赋值
  let runIdResolver!: (id: string) => void
  const runIdPromise = new Promise<string>((resolve) => {
    runIdResolver = resolve
  })

  const controller = new AbortController()

  // 如果外部传入 signal，监听其中断
  const externalSignalHandler = () => {
    controller.abort()
  }
  if (signal) {
    signal.addEventListener('abort', externalSignalHandler)
  }

  // 异步建立连接
  void (async () => {
    try {
      const response = await fetch(
        `${SSE_BASE_URL}/api/threads/${encodeURIComponent(threadId)}/runs/stream`,
        {
          method: 'POST',
          signal: controller.signal,
          headers: {
            'Content-Type': 'application/json',
            Accept: 'text/event-stream',
          },
          body: JSON.stringify({ message }),
        },
      )

      // 从响应头读取 X-Run-ID
      const runId = response.headers.get('X-Run-ID')
      if (runId) runIdResolver(runId)

      if (!response.ok) {
        // HTTP 错误：从响应体解析错误信息
        let text = ''
        try {
          const errReader = response.body?.getReader()
          if (errReader) {
            const { value } = await errReader.read()
            if (value) text = new TextDecoder().decode(value)
            errReader.releaseLock()
          }
        } catch {
          // 读取失败，使用默认消息
        }
        let requestId = response.headers.get('X-Request-ID') ?? undefined
        let message = `请求失败（${response.status}）`

        try {
          const body = JSON.parse(text)
          if (body?.error?.message) message = body.error.message
          if (body?.error?.request_id) requestId = body.error.request_id
        } catch {
          // 响应体不是 JSON，使用默认消息
        }

        const error = new StreamError({ status: response.status, message, requestId })
        onError?.(error)
        return
      }

      // 解析 SSE 流
      const reader = response.body?.getReader()
      if (!reader) {
        onError?.(new StreamError({ status: 0, message: '无法读取响应流' }))
        return
      }

      const decoder = new TextDecoder()
      let buffer = ''

      try {
        while (true) {
          const { done, value } = await reader.read()
          if (done) break

          const chunk = decoder.decode(value, { stream: true })
          const { events, remaining } = parseSseChunk(chunk, buffer)
          buffer = remaining

          for (const event of events) {
            // 检查是否为终态
            const eventType = event.event_type as RunEventType
            if (
              eventType === 'run.completed' ||
              eventType === 'run.failed' ||
              eventType === 'run.timed_out' ||
              eventType === 'run.cancelled'
            ) {
              onTerminal?.(event)
              // 终态后立即结束读取
              return
            }

            // 普通事件
            const shouldContinue = onEvent?.(event)
            if (shouldContinue === false) {
              // 用户要求提前结束
              return
            }
          }
        }
      } finally {
        reader.releaseLock()
      }
    } catch (err) {
      // AbortError 或网络错误
      if (err instanceof Error && err.name === 'AbortError') {
        // 正常中止，不报告错误
        return
      }
      onError?.(
        new StreamError({
          status: 0,
          message: err instanceof Error ? err.message : '连接失败',
        }),
      )
    } finally {
      // 清理事件监听
      if (signal) {
        signal.removeEventListener('abort', externalSignalHandler)
      }
    }
  })()

  // 返回 cancel 函数
  const cancel = () => controller.abort()

  return { runId: runIdPromise, cancel }
}
