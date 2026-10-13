/**
 * useContextUsage — 上下文用量快照状态机
 *
 * 职责：
 * - 切换 `threadId` 时立刻清空快照与错误，并 **abort** 旧请求；
 * - 同一 thread 重复 refresh 时 abort 前一次未返回的请求；
 * - 响应返回时若已切走（或本次请求已被 abort），丢弃该响应——
 *   避免旧 Thread 的数据覆盖当前会话。
 *
 * 不做的事：
 * - 不做估算、不分桶——那是纯渲染逻辑（见 contextUsage.ts）；
 * - 不主动轮询——只跟随调用方的 onChange（如 SSE 流进入终态）。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchContextUsage } from './contextUsageApi'
import type {
  ContextUsageError,
  ContextUsageSnapshot,
} from '../../shared/types/contextUsage'

export interface ContextUsageState {
  snapshot: ContextUsageSnapshot | null
  error: ContextUsageError
  loading: boolean
}

const INITIAL: ContextUsageState = {
  snapshot: null,
  error: null,
  loading: false,
}

export function useContextUsage(threadId: string) {
  const [state, setState] = useState<ContextUsageState>(INITIAL)
  // 用「上一次渲染的 threadId」在渲染期判定切换——React 官方推荐的
  // 调整 state 模式；放在 effect 里 setState 会引发级联渲染。
  const [prevThreadId, setPrevThreadId] = useState(threadId)
  const abortRef = useRef<AbortController | null>(null)

  if (prevThreadId !== threadId) {
    setPrevThreadId(threadId)
    setState(INITIAL)
  }

  // 切换 / 卸载时先中止未返回的请求，再拉取**当前**会话的统计。
  // 顺序不能反：先 abort 保证旧 Thread 的响应永远进不来（不串数据），
  // 后 refresh 保证切回会话时能立刻看到它自己的快照。
  useEffect(() => {
    abortRef.current?.abort()
    abortRef.current = null
    void refresh()
    return () => {
      abortRef.current?.abort()
      abortRef.current = null
    }
    // refresh 已以 threadId 为依赖，等价于「threadId 变化时执行」
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadId])

  const refresh = useCallback(async () => {
    abortRef.current?.abort()
    const ctrl = new AbortController()
    abortRef.current = ctrl
    setState((prev) => ({ ...prev, loading: true }))

    // 「已被取代」= 我不再是当前在途请求（被新一次 refresh 顶掉、
    // 被切换 threadId 中止、或组件已卸载）。此时**不写任何状态**：
    // 由接管方负责（新请求会设 loading，切线程会走 INITIAL 重置）。
    const superseded = () => abortRef.current !== ctrl || ctrl.signal.aborted

    try {
      const resp = await fetchContextUsage(threadId, ctrl.signal)
      if (superseded()) return
      setState({ snapshot: resp.snapshot, error: null, loading: false })
    } catch (err) {
      if (superseded()) return
      // 取消是正常语义：不记为错误，但必须结束 loading（否则永久卡住）。
      if (err instanceof Error && err.name === 'AbortError') {
        setState((prev) => ({ ...prev, loading: false }))
        return
      }
      const message = err instanceof Error ? err.message : '未知错误'
      setState({
        snapshot: null,
        error: { kind: 'fetch_failed', message },
        loading: false,
      })
    } finally {
      if (abortRef.current === ctrl) abortRef.current = null
    }
  }, [threadId])

  return { ...state, refresh }
}