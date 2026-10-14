/**
 * useAgentStream — SSE 流状态管理 Hook（20D）
 *
 * 职责：
 * - 建立 SSE 连接（通过 streamClient）
 * - 用 useReducer 管理流状态（不用 TanStack Query）
 * - 按 sequence 排序事件（处理乱序）
 * - 事件数量上限
 * - 收到终态后失效当前 Thread 缓存
 * - 组件卸载 / Thread 切换时断开连接
 *
 * 状态归属：
 * - 历史消息 → TanStack Query / ThreadDetail
 * - SSE 事件 → useReducer
 * - 输入框 → useState（在组件层）
 */
import { useReducer, useEffect, useCallback, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { connectAgentStream, type StreamError } from '../../shared/api/streamClient'
import type { RunEvent, RunEventType } from '../../shared/types/events'
import { threadKeys } from './queryKeys'

// ---------------------------------------------------------------------------
// 类型
// ---------------------------------------------------------------------------

/** 流状态 */
export type StreamStatus =
  | 'idle'       // 空闲，未开始
  | 'connecting' // 连接中
  | 'streaming'   // 接收事件中
  | 'completed'   // 正常结束（run.completed）
  | 'failed'      // 失败（run.failed）
  | 'timed_out'   // 超时（run.timed_out）
  | 'cancelled'   // 取消（run.cancelled）
  | 'stopped'     // 用户手动停止接收
  | 'error'       // HTTP 错误或网络错误

/** 流状态 */
export interface StreamState {
  /** 当前状态 */
  status: StreamStatus
  /** Run ID（从响应头读取） */
  runId: string | null
  /** 已接收的事件（按 sequence 排序） */
  events: RunEvent[]
  /** 最后一个终态事件 */
  terminalEvent: RunEvent | null
  /** 错误信息（status=error 时） */
  errorMessage: string | null
  /** 请求 ID（HTTP 错误时） */
  requestId: string | null
  /** 事件总数上限（超过后不再添加） */
  eventLimit: number
}

/** 流 Action */
type StreamAction =
  | { type: 'CONNECTING'; runId: string }
  | { type: 'EVENT'; event: RunEvent }
  | { type: 'TERMINAL'; event: RunEvent }
  | { type: 'ERROR'; message: string; requestId?: string }
  | { type: 'STOP' }

/** 事件数量上限 */
const MAX_EVENTS = 1000

// ---------------------------------------------------------------------------
// Reducer
// ---------------------------------------------------------------------------

function streamReducer(state: StreamState, action: StreamAction): StreamState {
  switch (action.type) {
    case 'CONNECTING':
      return {
        ...state,
        status: 'connecting',
        runId: action.runId,
        errorMessage: null,
        requestId: null,
      }

    case 'EVENT': {
      // 检查事件数量上限
      if (state.events.length >= state.eventLimit) {
        return state
      }

      // 按 sequence 插入（保持顺序）
      const newEvents = [...state.events, action.event]
      // 简单排序（假设大多数情况是顺序的）
      newEvents.sort((a, b) => a.sequence - b.sequence)

      return {
        ...state,
        status: 'streaming',
        events: newEvents,
      }
    }

    case 'TERMINAL': {
      const eventType = action.event.event_type as RunEventType
      const terminalStatus: StreamStatus =
        eventType === 'run.completed'
          ? 'completed'
          : eventType === 'run.failed'
            ? 'failed'
            : eventType === 'run.timed_out'
              ? 'timed_out'
              : 'cancelled'

      return {
        ...state,
        status: terminalStatus,
        events: [...state.events, action.event].sort((a, b) => a.sequence - b.sequence),
        terminalEvent: action.event,
      }
    }

    case 'ERROR':
      return {
        ...state,
        status: 'error',
        errorMessage: action.message,
        requestId: action.requestId ?? null,
      }

    case 'STOP':
      return {
        ...state,
        status: 'stopped',
      }

    default:
      return state
  }
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export interface UseAgentStreamOptions {
  /** Thread ID */
  threadId: string
  /** 用户消息 */
  message: string
  /** 是否自动开始（默认 true） */
  autoStart?: boolean
}

/**
 * SSE 流 Hook
 *
 * @example
 * const { status, events, runId, errorMessage, requestId, start, stop } = useAgentStream({
 *   threadId: 't1',
 *   message: 'Hello',
 * })
 */
export function useAgentStream({ threadId, message, autoStart = true }: UseAgentStreamOptions) {
  const queryClient = useQueryClient()
  const cancelRef = useRef<(() => void) | null>(null)

  const [state, dispatch] = useReducer(streamReducer, {
    status: 'idle',
    runId: null,
    events: [],
    terminalEvent: null,
    errorMessage: null,
    requestId: null,
    eventLimit: MAX_EVENTS,
  })

  // 开始流
  // messageOverride：调用方在同一个事件循环里刚 setState 再调 start 时，
  // 闭包捕获的 message 仍是旧值，必须把本次要发送的消息显式传进来，
  // 否则实际会发出空消息（后端 422）。
  const start = useCallback((messageOverride?: string) => {
    // 空消息不建立 SSE 流，避免"运行中"假状态
    const effectiveMessage = messageOverride ?? message
    if (!effectiveMessage.trim()) return

    // 如果已有连接，先断开
    if (cancelRef.current) {
      cancelRef.current()
      cancelRef.current = null
    }

    const { runId, cancel } = connectAgentStream(threadId, effectiveMessage, {
      onEvent: (event) => {
        dispatch({ type: 'EVENT', event })
      },
      onTerminal: (event) => {
        dispatch({ type: 'TERMINAL', event })
        // 终态后失效当前 Thread 缓存
        void queryClient.invalidateQueries({ queryKey: threadKeys.detail(threadId) })
      },
      onError: (error: StreamError) => {
        dispatch({
          type: 'ERROR',
          message: error.message,
          requestId: error.requestId,
        })
      },
    })

    // 监听 runId 解析
    runId.then((id) => {
      dispatch({ type: 'CONNECTING', runId: id })
    })

    cancelRef.current = cancel
  }, [threadId, message, queryClient])

  // 停止接收
  const stop = useCallback(() => {
    if (cancelRef.current) {
      cancelRef.current()
      cancelRef.current = null
    }
    dispatch({ type: 'STOP' })
  }, [])

  // 自动开始：message 必须非空才建立 SSE 流，避免空消息触发"运行中"假状态
  useEffect(() => {
    const trimmed = message.trim()
    if (autoStart && state.status === 'idle' && trimmed) {
      start()
    }
  }, [autoStart, state.status, message, start])

  // 组件卸载时断开
  useEffect(() => {
    return () => {
      if (cancelRef.current) {
        cancelRef.current()
        cancelRef.current = null
      }
    }
  }, [])

  return {
    ...state,
    start,
    stop,
  }
}
