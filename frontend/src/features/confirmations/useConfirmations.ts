/**
 * useConfirmations — TOOL-05 确认卡数据源
 *
 * 设计：
 * - 轮询间隔 2s；仅在有 activeRunId 时启动；切会话/卸载时停止
 * - 用户操作（approve/deny/execute）后立即 refresh 绕过 2s 间隔
 * - 断连不重放：fetch 错误保留旧数据 + 显示离线状态，**不**自动重发操作
 *
 * 返回：
 * - items: 本会话期间见过的所有 confirmation（终态保留）
 * - approve / deny / cancel / executeAndExecute: 触发操作 + refresh
 * - online: 轮询是否成功（true = 在线）
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  approveConfirmation,
  cancelConfirmation,
  denyConfirmation,
  executeSnapshot,
  listConfirmations,
  getActiveRun,
} from '../../shared/api/confirmations'
import type { ConfirmationResponse } from '../../shared/types/confirmations'

const POLL_INTERVAL_MS = 2000

export interface UseConfirmationsResult {
  /** 按 request_id 索引的所有 confirmation（含终态）。 */
  items: Record<string, ConfirmationResponse>
  /** 按 run_id 索引的 confirmation 列表（便于按 run 分组展示）。 */
  byRunId: Record<string, ConfirmationResponse[]>
  /** 轮询是否成功（false = 离线/网络错误）。 */
  online: boolean
  /** 强制立即刷新。 */
  refresh: () => Promise<void>
  approve: (id: string) => Promise<ConfirmationResponse | null>
  deny: (id: string) => Promise<ConfirmationResponse | null>
  cancel: (id: string) => Promise<ConfirmationResponse | null>
  /**
   * "批准并执行"复合操作：
   * 1) 调 approve
   * 2) 调 execute-snapshot（用 confirmation 自身的绑定字段）
   * 失败时不重放。
   */
  approveAndExecute: (id: string) => Promise<{
    approved: ConfirmationResponse | null
    executed: ConfirmationResponse | null
  }>
}

export function useConfirmations(
  threadId: string | null | undefined,
): UseConfirmationsResult {
  const [items, setItems] = useState<Record<string, ConfirmationResponse>>({})
  const [online, setOnline] = useState<boolean>(true)
  const activeRunIdRef = useRef<string | null>(null)
  const stoppedRef = useRef<boolean>(false)

  const fetchAndMerge = useCallback(async (): Promise<string | null> => {
    if (!threadId) return null
    let active: string | null
    try {
      const probe = await getActiveRun(threadId)
      active = probe.run_id
      setOnline(true)
    } catch {
      setOnline(false)
      return null
    }
    activeRunIdRef.current = active
    if (active === null) return null
    try {
      const list = await listConfirmations({ thread_id: threadId, run_id: active })
      setItems((prev) => {
        const next = { ...prev }
        for (const c of list) {
          next[c.request_id] = c
        }
        return next
      })
      setOnline(true)
      return active
    } catch {
      setOnline(false)
      return active
    }
  }, [threadId])

  useEffect(() => {
    if (!threadId) return
    stoppedRef.current = false
    setItems({})
    activeRunIdRef.current = null
    void fetchAndMerge()
    const handle = setInterval(() => {
      if (stoppedRef.current) return
      void fetchAndMerge()
    }, POLL_INTERVAL_MS)
    return () => {
      stoppedRef.current = true
      clearInterval(handle)
    }
  }, [threadId, fetchAndMerge])

  const refresh = useCallback(async () => {
    await fetchAndMerge()
  }, [fetchAndMerge])

  const approve = useCallback(
    async (id: string) => {
      try {
        const resp = await approveConfirmation(id)
        setItems((prev) => ({ ...prev, [id]: resp }))
        await refresh()
        return resp
      } catch {
        setOnline(false)
        return null
      }
    },
    [refresh],
  )

  const deny = useCallback(
    async (id: string) => {
      try {
        const resp = await denyConfirmation(id)
        setItems((prev) => ({ ...prev, [id]: resp }))
        await refresh()
        return resp
      } catch {
        setOnline(false)
        return null
      }
    },
    [refresh],
  )

  const cancel = useCallback(
    async (id: string) => {
      try {
        const resp = await cancelConfirmation(id)
        setItems((prev) => ({ ...prev, [id]: resp }))
        await refresh()
        return resp
      } catch {
        setOnline(false)
        return null
      }
    },
    [refresh],
  )

  const approveAndExecute = useCallback(
    async (id: string) => {
      const c = items[id]
      if (!c) {
        return { approved: null, executed: null }
      }
      const approved = await approve(id)
      if (!approved) return { approved: null, executed: null }
      try {
        const executed = await executeSnapshot(id, {
          run_id: approved.run_id,
          thread_id: approved.thread_id,
          tool_name: approved.tool_name,
          arguments_hash: approved.arguments_hash,
        })
        setItems((prev) => ({ ...prev, [id]: executed }))
        await refresh()
        return { approved, executed }
      } catch {
        setOnline(false)
        return { approved, executed: null }
      }
    },
    [items, approve, refresh],
  )

  const byRunId: Record<string, ConfirmationResponse[]> = {}
  for (const c of Object.values(items)) {
    const arr = byRunId[c.run_id] ?? []
    arr.push(c)
    byRunId[c.run_id] = arr
  }

  return { items, byRunId, online, refresh, approve, deny, cancel, approveAndExecute }
}
