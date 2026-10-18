/**
 * useConfirmations — TOOL-05 round 2 修复
 *
 * 设计：
 * - 轮询间隔 2s；仅在有 active run 时启动；切会话/卸载时停止
 * - 每次轮询或操作都用 generation 标签：旧 generation 的响应 setItems 会被丢弃
 *   防止慢响应在切线程后污染新线程
 * - fetch / approve / deny / execute 都用 AbortController，卸载/切线程时 abort
 * - 真实 activeRunId 从 getActiveRun 读出，传给卡片
 * - 卡片按 run_id 精确控制；不从缓存状态猜活性
 * - run 结束（activeRunId=null）也拉一次列表以核对终态，避免跳过结果恢复
 * - 断连不重放：fetch 错误保留旧数据 + 显示离线状态，**不**自动重发操作
 *
 * 返回：
 * - items: 本会话期间见过的所有 confirmation（按 request_id 索引）
 * - byRunId: 按 run_id 分组
 * - activeRunId: 真实活跃 run_id（可能 null）
 * - online: 轮询是否成功
 * - inFlight: 当前正在操作的 request_id 集合（防止重复点击）
 * - refresh / approve / deny / cancel / approveAndExecute
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
  /** 按 run_id 索引的 confirmation 列表。 */
  byRunId: Record<string, ConfirmationResponse[]>
  /** 真实活跃 run_id（由 getActiveRun 读出）。null 表示无活跃或查询失败。 */
  activeRunId: string | null
  /** 轮询是否成功。 */
  online: boolean
  /** 正在操作的 request_id 集合。 */
  inFlight: Set<string>
  refresh: () => Promise<void>
  approve: (id: string) => Promise<ConfirmationResponse | null>
  deny: (id: string) => Promise<ConfirmationResponse | null>
  cancel: (id: string) => Promise<ConfirmationResponse | null>
  approveAndExecute: (id: string) => Promise<{
    approved: ConfirmationResponse | null
    executed: ConfirmationResponse | null
  }>
}

export function useConfirmations(
  threadId: string | null | undefined,
): UseConfirmationsResult {
  const [items, setItems] = useState<Record<string, ConfirmationResponse>>({})
  const [activeRunId, setActiveRunId] = useState<string | null>(null)
  const [online, setOnline] = useState<boolean>(true)
  const [inFlight, setInFlight] = useState<Set<string>>(() => new Set())
  const inFlightRef = useRef<Set<string>>(inFlight)
  inFlightRef.current = inFlight

  const generationRef = useRef<number>(0)
  const threadIdRef = useRef<string | null>(threadId ?? null)
  threadIdRef.current = threadId ?? null
  const abortRef = useRef<AbortController | null>(null)

  // 切线程/卸载时调用：递增 generation 令旧响应 setState 失效 + abort 在途请求
  const stopAll = useCallback(() => {
    generationRef.current += 1
    if (abortRef.current) {
      abortRef.current.abort()
      abortRef.current = null
    }
  }, [])

  const fetchAndMerge = useCallback(async (): Promise<string | null> => {
    const tid = threadIdRef.current
    if (!tid) return null
    const myGen = generationRef.current
    const controller = new AbortController()
    abortRef.current?.abort()
    abortRef.current = controller
    try {
      const probe = await getActiveRun(tid)
      if (generationRef.current !== myGen) return null
      const active = probe.run_id
      setOnline(true)
      setActiveRunId(active)
      if (active === null) {
        // 无活跃 run：也拉一次列表以核对已缓存记录的终态
        // （覆盖"运行结束时已显示 pending 实际已 executed/cancelled"）
        try {
          const list = await listConfirmations({ thread_id: tid })
          if (generationRef.current !== myGen) return null
          setItems((prev) => {
            const next = { ...prev }
            for (const c of list) {
              next[c.request_id] = c
            }
            return next
          })
        } catch {
          setOnline(false)
        }
        return null
      }
      const list = await listConfirmations({ thread_id: tid, run_id: active })
      if (generationRef.current !== myGen) return null
      setItems((prev) => {
        const next = { ...prev }
        for (const c of list) {
          // 仅接受当前 thread / run 的记录
          if (c.thread_id === tid && c.run_id === active) {
            next[c.request_id] = c
          }
        }
        return next
      })
      setOnline(true)
      return active
    } catch (err) {
      // abort 静默忽略
      if ((err as { name?: string })?.name === 'AbortError') return null
      setOnline(false)
      return null
    } finally {
      // 释放 controller（仅当仍是自己的）
      if (abortRef.current === controller) {
        abortRef.current = null
      }
    }
  }, [])

  useEffect(() => {
    if (!threadId) return
    // 切线程：清空状态 + 停旧轮询 + 升 generation
    stopAll()
    setItems({})
    setActiveRunId(null)
    setOnline(true)
    const myGen = ++generationRef.current

    void fetchAndMerge()
    const handle = setInterval(() => {
      if (generationRef.current !== myGen) return
      void fetchAndMerge()
    }, POLL_INTERVAL_MS)
    return () => {
      // 卸载：升 generation + abort
      stopAll()
      clearInterval(handle)
    }
  }, [threadId, fetchAndMerge, stopAll])

  const refresh = useCallback(async () => {
    await fetchAndMerge()
  }, [fetchAndMerge])

  /** 内部：把操作结果（含终态）合并到 items；带 generation 校验 */
  /** R3：变更响应必须带发起时的世代号；旧世代（切会话/卸载后）丢弃，不写入新会话。 */
  const mergeOne = useCallback(
    (resp: ConfirmationResponse, gen: number) => {
      if (gen !== generationRef.current) return // 过期世代：不写入
      setItems((prev) => {
        const next = { ...prev }
        next[resp.request_id] = resp
        return next
      })
    },
    [],
  )

  /**
   * 同步获取 in-flight 标记（返回 false 表示已在飞，调用方应立即放弃）。
   * 关键：先写 ref 再 setState — setState 是异步批处理，同一 tick 内的第二次
   * 点击读 ref 必须立刻看到第一次的结果，否则双击会重复发送（R4）。
   */
  const tryAddInFlight = useCallback((id: string): boolean => {
    if (inFlightRef.current.has(id)) return false
    const next = new Set(inFlightRef.current)
    next.add(id)
    inFlightRef.current = next
    setInFlight(next)
    return true
  }, [])
  const removeInFlight = useCallback((id: string) => {
    const next = new Set(inFlightRef.current)
    if (!next.has(id)) return
    next.delete(id)
    inFlightRef.current = next
    setInFlight(next)
  }, [])

  const approve = useCallback(
    async (id: string) => {
      if (!tryAddInFlight(id)) return null
      const gen = generationRef.current
      try {
        const resp = await approveConfirmation(id)
        mergeOne(resp, gen)
        await refresh()
        return resp
      } catch {
        setOnline(false)
        return null
      } finally {
        removeInFlight(id)
      }
    },
    [tryAddInFlight, removeInFlight, mergeOne, refresh],
  )

  const deny = useCallback(
    async (id: string) => {
      if (!tryAddInFlight(id)) return null
      const gen = generationRef.current
      try {
        const resp = await denyConfirmation(id)
        mergeOne(resp, gen)
        await refresh()
        return resp
      } catch {
        setOnline(false)
        return null
      } finally {
        removeInFlight(id)
      }
    },
    [tryAddInFlight, removeInFlight, mergeOne, refresh],
  )

  const cancel = useCallback(
    async (id: string) => {
      if (!tryAddInFlight(id)) return null
      const gen = generationRef.current
      try {
        const resp = await cancelConfirmation(id)
        mergeOne(resp, gen)
        await refresh()
        return resp
      } catch {
        setOnline(false)
        return null
      } finally {
        removeInFlight(id)
      }
    },
    [tryAddInFlight, removeInFlight, mergeOne, refresh],
  )

  const approveAndExecute = useCallback(
    async (id: string): Promise<{
      approved: ConfirmationResponse | null
      executed: ConfirmationResponse | null
    }> => {
      if (!tryAddInFlight(id)) {
        return { approved: null, executed: null }
      }
      const current = items[id]
      if (!current) {
        removeInFlight(id)
        return { approved: null, executed: null }
      }
      const gen = generationRef.current
      try {
        const approved = await approveConfirmation(id)
        mergeOne(approved, gen)
        const executed = await executeSnapshot(id, {
          run_id: approved.run_id,
          thread_id: approved.thread_id,
          tool_name: approved.tool_name,
          arguments_hash: approved.arguments_hash,
        })
        mergeOne(executed, gen)
        await refresh()
        return { approved, executed }
      } catch {
        setOnline(false)
        try {
          await getActiveRun(threadIdRef.current ?? '')
        } catch {
          /* ignore */
        }
        return { approved: null, executed: null }
      } finally {
        removeInFlight(id)
      }
    },
    [items, tryAddInFlight, removeInFlight, mergeOne, refresh],
  )

  const byRunId: Record<string, ConfirmationResponse[]> = {}
  for (const c of Object.values(items)) {
    const arr = byRunId[c.run_id] ?? []
    arr.push(c)
    byRunId[c.run_id] = arr
  }

  return {
    items,
    byRunId,
    activeRunId,
    online,
    inFlight,
    refresh,
    approve,
    deny,
    cancel,
    approveAndExecute,
  }
}
