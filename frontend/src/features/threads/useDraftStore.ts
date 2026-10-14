/**
 * useDraftStore — 草稿持久化 Hook（sessionStorage + threadId 分键）
 *
 * 设计原则：
 * - 每个 threadId 独立存储草稿，切换会话互不污染
 * - sessionStorage 范围：同标签页、同源；关闭标签页丢失（按当前产品范围）
 * - 跨标签页/登录态变化不在范围
 *
 * 数据结构：
 * - draft:{threadId} = { current: string, pendingSent: string | null }
 *
 * 使用场景：
 * - 发送前保存 pendingSent，编辑区继续接受新草稿
 * - 发送后 clear pendingSent（历史已落库）
 * - unknown 时无新草稿 → 显示"恢复"按钮，用户主动点才回填
 * - unknown 时有新草稿 → 保留新草稿，不动 pendingSent
 */
import { useCallback, useState } from 'react'

export interface DraftState {
  /** textarea 当前内容（用户可正常输入） */
  current: string
  /** 最近一次发送快照（unknown 时可恢复）；发送后清空 */
  pendingSent: string | null
}

function draftKey(threadId: string): string {
  return `draft:${threadId}`
}

function readDraft(threadId: string): DraftState {
  try {
    const raw = sessionStorage.getItem(draftKey(threadId))
    if (raw) {
      const parsed = JSON.parse(raw)
      if (typeof parsed === 'object' && parsed !== null) return parsed as DraftState
    }
  } catch {
    // sessionStorage 不可用或 JSON 解析失败：静默降级
  }
  return { current: '', pendingSent: null }
}

function writeDraft(threadId: string, state: DraftState): void {
  try {
    sessionStorage.setItem(draftKey(threadId), JSON.stringify(state))
  } catch {
    // sessionStorage 不可用（隐私模式等）：静默降级
  }
}

function clearDraft(threadId: string): void {
  try {
    sessionStorage.removeItem(draftKey(threadId))
  } catch {
    // 静默降级
  }
}

/** 钩子：绑定到指定 threadId 的草稿状态 */
export function useDraftStore(threadId: string) {
  const [draft, setDraft] = useState<DraftState>(() => readDraft(threadId))

  /** 保存当前草稿（textarea 变化时调用） */
  const saveCurrent = useCallback(
    (current: string) => {
      setDraft((prev) => {
        const next = { ...prev, current }
        writeDraft(threadId, next)
        return next
      })
    },
    [threadId],
  )

  /** 发送前：保存 pendingSent，编辑区清空继续接受新草稿 */
  const savePendingSent = useCallback(
    (message: string) => {
      setDraft((prev) => {
        const next = { ...prev, current: '', pendingSent: message }
        writeDraft(threadId, next)
        return next
      })
    },
    [threadId],
  )

  /** 发送成功/历史回来：清空 pendingSent */
  const clearPendingSent = useCallback(() => {
    setDraft((prev) => {
      const next = { ...prev, pendingSent: null }
      writeDraft(threadId, next)
      return next
    })
  }, [threadId])

  /** 用户主动恢复：pendingSent → textarea，pendingSent 清空 */
  const restorePendingSent = useCallback(() => {
    setDraft((prev) => {
      if (!prev.pendingSent) return prev
      const next = { current: prev.pendingSent, pendingSent: null }
      writeDraft(threadId, next)
      return next
    })
  }, [threadId])

  /** 清空全部草稿（切换到新会话时调用） */
  const clearAll = useCallback(() => {
    // 先清 sessionStorage（否则 setDraft 的 re-render 会重新 writeDraft 覆盖删除）
    clearDraft(threadId)
    setDraft({ current: '', pendingSent: null })
  }, [threadId])

  return {
    draft,
    saveCurrent,
    savePendingSent,
    clearPendingSent,
    restorePendingSent,
    clearAll,
  }
}
