/**
 * useConfirmations — TOOL-05 round 3
 *
 * R3（旧响应污染）：
 * - generationRef：切会话/卸载后旧响应（含错误路径）一律丢弃
 * - fetchSeqRef：同世代内查询顺序保护——旧查询晚到不覆盖新结果
 * - mergeClobber：executed 终态不被 pending/approved 回退
 * - AbortController.signal 实际传到 fetch（查询）
 *
 * R4（断连恢复）：
 * - 操作失败 → 先核对该 request（GET 无副作用，不重放）：
 *   - 后端仍 pending/approved（未领取）→ kind='retryable'（卡片释放守卫，允许明确重试）
 *   - 已 executed/终态 → kind='done'（如实展示）
 *   - 核对也失败 → kind='unknown'（保持未知，禁用重放）
 * - verify(id)：纯核对入口（无副作用）
 *
 * 返回：
 * - items / byRunId / activeRunId / online / inFlight
 * - approve / deny / cancel / approveAndExecute / verify：均返回 { kind }
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  approveConfirmation,
  cancelConfirmation,
  denyConfirmation,
  executeSnapshot,
  getActiveRun,
  getConfirmation,
  listConfirmations,
} from '../../shared/api/confirmations'
import type { ConfirmationResponse } from '../../shared/types/confirmations'

const POLL_INTERVAL_MS = 2000

/** 操作结论：done=已确认；retryable=后端未领取可重试；unknown=结果未知 */
export type OpKind = 'done' | 'retryable' | 'unknown'

/**
 * 合并守卫：executed 终态不被旧查询/回退状态覆盖（R3）。
 * 其余组合后到覆盖（deny/cancel/expired 互转由后端状态机保证）。
 */
function mergeClobber(
  prev: ConfirmationResponse | undefined,
  incoming: ConfirmationResponse,
): ConfirmationResponse {
  if (prev && prev.state === 'executed' && incoming.state !== 'executed') {
    return prev // 终态不回退
  }
  return incoming
}

function isAbortError(err: unknown): boolean {
  return (err as { name?: string })?.name === 'AbortError'
}

export interface UseConfirmationsResult {
  /** 按 request_id 索引的所有 confirmation（含终态）。 */
  items: Record<string, ConfirmationResponse>
  /** 按 run_id 索引的 confirmation 列表。 */
  byRunId: Record<string, ConfirmationResponse[]>
  /** 真实活跃 run_id（由 getActiveRun 读出）。 */
  activeRunId: string | null
  /** 轮询是否成功。 */
  online: boolean
  /** 正在操作的 request_id 集合。 */
  inFlight: Set<string>
  refresh: () => Promise<void>
  approve: (id: string) => Promise<{ kind: OpKind }>
  deny: (id: string) => Promise<{ kind: OpKind }>
  cancel: (id: string) => Promise<{ kind: OpKind }>
  approveAndExecute: (id: string) => Promise<{ kind: OpKind }>
  /** 纯核对（GET 无副作用）：拉最新状态并合并。 */
  verify: (id: string) => Promise<{ kind: OpKind }>
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
  /** R3：同世代查询顺序——最新一次查询序号；旧序号响应不写入 */
  const fetchSeqRef = useRef<number>(0)

  // 切线程/卸载：递增世代 + abort 在途查询
  const stopAll = useCallback(() => {
    generationRef.current += 1
    fetchSeqRef.current += 1 // 令在途查询的 seq 判定立即过期
    if (abortRef.current) {
      abortRef.current.abort()
      abortRef.current = null
    }
  }, [])

  /** 带顺序与世代双保护的合并（单条） */
  const mergeOne = useCallback(
    (resp: ConfirmationResponse, gen: number) => {
      if (gen !== generationRef.current) return // 旧世代：不写入
      setItems((prev) => {
        const next = { ...prev }
        next[resp.request_id] = mergeClobber(prev[resp.request_id], resp)
        return next
      })
    },
    [],
  )

  const fetchAndMerge = useCallback(async (): Promise<string | null> => {
    const tid = threadIdRef.current
    if (!tid) return null
    const myGen = generationRef.current
    const seq = ++fetchSeqRef.current
    const controller = new AbortController()
    abortRef.current?.abort()
    abortRef.current = controller
    const sig = controller.signal
    /** R3：世代 + 顺序双检查（含错误路径） */
    const stale = (): boolean =>
      generationRef.current !== myGen || seq !== fetchSeqRef.current
    const mergeList = (
      list: ConfirmationResponse[],
      runFilter: string | null,
    ): void => {
      setItems((prev) => {
        const next = { ...prev }
        for (const c of list) {
          // 仅接受当前 thread / run 的记录
          if (c.thread_id !== tid) continue
          if (runFilter !== null && c.run_id !== runFilter) continue
          next[c.request_id] = mergeClobber(prev[c.request_id], c)
        }
        return next
      })
    }
    try {
      const probe = await getActiveRun(tid, sig)
      if (stale()) return null
      const active = probe.run_id
      setOnline(true)
      setActiveRunId(active)
      // run 结束（active=null）也拉列表核对终态；进行中按 run 过滤
      try {
        const list = await listConfirmations(
          active === null
            ? { thread_id: tid }
            : { thread_id: tid, run_id: active },
          sig,
        )
        if (stale()) return null
        mergeList(list, active)
      } catch (inner) {
        if (isAbortError(inner) || stale()) return null
        setOnline(false)
      }
      return active
    } catch (err) {
      // R3：abort 静默；旧世代/旧序号错误不污染当前会话（setOnline 也要检查）
      if (isAbortError(err) || stale()) return null
      setOnline(false)
      return null
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null
      }
    }
  }, [])

  useEffect(() => {
    if (!threadId) return
    stopAll()
    setItems({})
    setActiveRunId(null)
    setOnline(true)

    void fetchAndMerge()
    const handle = setInterval(() => {
      void fetchAndMerge()
    }, POLL_INTERVAL_MS)
    return () => {
      stopAll()
      clearInterval(handle)
    }
  }, [threadId, fetchAndMerge, stopAll])

  const refresh = useCallback(async () => {
    await fetchAndMerge()
  }, [fetchAndMerge])

  /** 同 tick 可见的 in-flight 守卫（先写 ref 再 setState） */
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

  /** R4：核对该 request（GET 无副作用），判定可重试/已确认/未知 */
  const confirmFromServer = useCallback(
    async (id: string, gen: number): Promise<OpKind> => {
      try {
        const latest = await getConfirmation(id)
        if (gen !== generationRef.current) return 'unknown'
        mergeOne(latest, gen)
        if (
          latest.state === 'pending' ||
          (latest.state === 'approved' && latest.execution_result == null)
        ) {
          // 未领取 → 允许用户明确重试
          return 'retryable'
        }
        // executed（含 claimed）/denied/cancelled/expired → 如实展示
        return 'done'
      } catch {
        return 'unknown'
      }
    },
    [mergeOne],
  )

  const verify = useCallback(
    async (id: string): Promise<{ kind: OpKind }> => {
      const gen = generationRef.current
      return { kind: await confirmFromServer(id, gen) }
    },
    [confirmFromServer],
  )

  /** R4/R3 通用操作外壳：in-flight 守卫 + 世代捕获 + 失败核对 */
  const runOperation = useCallback(
    async (
      id: string,
      action: () => Promise<ConfirmationResponse>,
      afterMerge?: (
        resp: ConfirmationResponse,
        gen: number,
      ) => Promise<{ kind: OpKind }>,
    ): Promise<{ kind: OpKind }> => {
      if (!tryAddInFlight(id)) return { kind: 'unknown' }
      const gen = generationRef.current
      try {
        const resp = await action()
        mergeOne(resp, gen)
        if (afterMerge) return await afterMerge(resp, gen)
        await refresh()
        return { kind: 'done' }
      } catch {
        // R3：错误路径世代检查——旧会话失败不把新会话标离线
        if (gen === generationRef.current) setOnline(false)
        // R4：先核对，不自动重放副作用
        return { kind: await confirmFromServer(id, gen) }
      } finally {
        removeInFlight(id)
      }
    },
    [mergeOne, refresh, confirmFromServer],
  )

  const approve = useCallback(
    (id: string) => runOperation(id, () => approveConfirmation(id)),
    [runOperation],
  )
  const deny = useCallback(
    (id: string) => runOperation(id, () => denyConfirmation(id)),
    [runOperation],
  )
  const cancel = useCallback(
    (id: string) => runOperation(id, () => cancelConfirmation(id)),
    [runOperation],
  )
  const approveAndExecute = useCallback(
    (id: string) =>
      runOperation(
        id,
        async () => {
          const approved = await approveConfirmation(id)
          // 快照执行：服务端读参数原值
          const executed = await executeSnapshot(id, {
            run_id: approved.run_id,
            thread_id: approved.thread_id,
            tool_name: approved.tool_name,
            arguments_hash: approved.arguments_hash,
          })
          return executed
        },
        // R3：mergeOne 用已捕获的 gen，不跨 await 读 generationRef
        async (executed, gen) => {
          // executed 包含 approved+executed 状态，合并即可
          mergeOne(executed, gen)
          return { kind: 'done' }
        },
      ),
    [runOperation, mergeOne],
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
    verify,
  }
}
