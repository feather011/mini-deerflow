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
import { useReducer, useEffect, useCallback, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { connectAgentStream, type StreamError } from '../../shared/api/streamClient'
import { ApiError } from '../../shared/api/client'
import { stopAgentRun } from './runsApi'
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
  | 'stopped'     // 用户手动停止接收（仅断流，后端仍在跑）
  | 'error'       // HTTP 错误或网络错误

/**
 * 取消（真取消）状态
 *
 * - idle      ：没有待处理的取消请求
 * - cancelling：已向后端发出 stop 请求，等待后端推 run.cancelled
 * - cancelled ：后端已确认取消（收到 run.cancelled 终态）
 * - failed    ：取消**失败**（后端没有正在运行的 Run / 网络错误）——
 *               此时后端任务可能仍在运行，UI 不得谎称"已停止"
 */
export type CancelState = 'idle' | 'cancelling' | 'cancelled' | 'failed'

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
  /** 当前 run 的累加 token 文本（用于流式渲染 assistant 消息） */
  currentText: string
}

/** 流 Action */
type StreamAction =
  | { type: 'CONNECTING'; runId: string }
  | { type: 'EVENT'; event: RunEvent }
  | { type: 'TOKEN_CHUNK'; delta: string }
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
        // 25C: 新 run 开始时重置累加文本
        currentText: '',
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

    case 'TOKEN_CHUNK':
      return {
        ...state,
        status: 'streaming',
        currentText: state.currentText + action.delta,
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
        // 25C: 终态时重置累加文本，为下一次 run 准备
        currentText: '',
      }
    }

    case 'ERROR':
      return {
        ...state,
        status: 'error',
        errorMessage: action.message,
        requestId: action.requestId ?? null,
        currentText: '',
      }

    case 'STOP':
      return {
        ...state,
        status: 'stopped',
        currentText: '',
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
  // 取消（真取消）状态
  const [cancelState, setCancelState] = useState<CancelState>('idle')
  const [cancelError, setCancelError] = useState<string | null>(null)
  // 防重复：一次 run 只允许发一次取消请求
  const cancelRequestedRef = useRef(false)
  const cancelStateRef = useRef<CancelState>('idle')
  const mountedRef = useRef(true)
  // 本 hook 是否持有"正在运行的 run"（用 ref 而不是 reducer 状态：
  // reducer 的 connecting 要等 runId promise 兑现才更新，不适合做门禁）
  const runActiveRef = useRef(false)

  const applyCancelState = useCallback((next: CancelState) => {
    cancelStateRef.current = next
    if (mountedRef.current) setCancelState(next)
  }, [])

  const [state, dispatch] = useReducer(streamReducer, {
    status: 'idle',
    runId: null,
    events: [],
    terminalEvent: null,
    errorMessage: null,
    requestId: null,
    eventLimit: MAX_EVENTS,
    currentText: '',
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
    // 新的一轮 run：取消状态复位
    cancelRequestedRef.current = false
    runActiveRef.current = true
    applyCancelState('idle')
    if (mountedRef.current) setCancelError(null)

    const { runId, cancel } = connectAgentStream(threadId, effectiveMessage, {
      onEvent: (event) => {
        // 25C: model.token 事件分发到 TOKEN_CHUNK（累加到 currentText）
        if (event.event_type === 'model.token' && event.delta) {
          dispatch({ type: 'TOKEN_CHUNK', delta: event.delta })
        } else {
          dispatch({ type: 'EVENT', event })
        }
      },
      onTerminal: (event) => {
        dispatch({ type: 'TERMINAL', event })
        runActiveRef.current = false
        if (event.event_type === 'run.cancelled') {
          // 后端确认取消：子进程已回收、锁已释放
          applyCancelState('cancelled')
        } else if (cancelStateRef.current === 'cancelling') {
          // 取消请求送达前任务已自己结束：如实报告，不谎称"已取消"
          applyCancelState('failed')
          if (mountedRef.current) {
            setCancelError('任务在取消请求生效前已结束')
          }
        }
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
  }, [threadId, message, queryClient, applyCancelState])

  // 停止接收（仅断流）：用于会话切换 / 组件卸载，**不取消后端任务**
  const stop = useCallback(() => {
    if (cancelRef.current) {
      cancelRef.current()
      cancelRef.current = null
    }
    dispatch({ type: 'STOP' })
  }, [])

  /**
   * 停止当前任务（真取消）：调用后端 stop 接口。
   *
   * - 成功后**不**断流：等后端推 run.cancelled 终态，UI 才知道任务真的停了
   * - 失败（404 = 后端没有正在运行的 Run / 网络错误）：状态置 failed，
   *   错误文案如实透出，**不**把"仅断流"说成"任务已停止"
   * - 重复点击只发一次请求（cancelRequestedRef）
   */
  const cancelRun = useCallback(async () => {
    if (cancelRequestedRef.current) return
    if (!runActiveRef.current) return
    cancelRequestedRef.current = true
    applyCancelState('cancelling')
    if (mountedRef.current) setCancelError(null)
    try {
      await stopAgentRun(threadId)
      // 后端已受理取消：保持 'cancelling'，等 run.cancelled 终态
    } catch (err) {
      cancelRequestedRef.current = false
      const status = err instanceof ApiError ? err.status : 0
      const message =
        status === 404
          ? '后端没有正在运行的 Run（可能已结束）'
          : err instanceof Error
            ? err.message
            : '取消失败'
      applyCancelState('failed')
      if (mountedRef.current) setCancelError(message)
    }
  }, [threadId, applyCancelState])

  // 自动开始：message 必须非空才建立 SSE 流，避免空消息触发"运行中"假状态
  useEffect(() => {
    const trimmed = message.trim()
    if (autoStart && state.status === 'idle' && trimmed) {
      start()
    }
  }, [autoStart, state.status, message, start])

  // 组件卸载时断开（仅断流，不取消后端任务）
  useEffect(() => {
    // setup 必须恢复"已挂载"标记：根 StrictMode 在开发模式会重放 effect
    // （setup → cleanup → setup）。若 setup 不恢复，cleanup 之后的组件会被
    // 永久当成已卸载 —— 取消中状态与取消失败提示再也更新不出来。
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      runActiveRef.current = false
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
    cancelRun,
    cancelState,
    cancelError,
  }
}
