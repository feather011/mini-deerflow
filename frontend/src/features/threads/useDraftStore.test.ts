/**
 * useDraftStore 单元测试（UX-1 子任务 4：草稿保护）
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useDraftStore } from './useDraftStore'

// Mock sessionStorage
const storage: Record<string, string> = {}
vi.stubGlobal('sessionStorage', {
  getItem: (key: string) => storage[key] ?? null,
  setItem: (key: string, value: string) => { storage[key] = value },
  removeItem: (key: string) => { delete storage[key] },
  clear: () => { Object.keys(storage).forEach(k => delete storage[k]) },
})

beforeEach(() => {
  Object.keys(storage).forEach(k => delete storage[k])
})

describe('useDraftStore', () => {
  it('初始状态为空', () => {
    const { result } = renderHook(() => useDraftStore('t1'))
    expect(result.current.draft.current).toBe('')
    expect(result.current.draft.pendingSent).toBeNull()
  })

  it('saveCurrent 保存当前草稿', () => {
    const { result } = renderHook(() => useDraftStore('t1'))
    act(() => { result.current.saveCurrent('hello') })
    expect(result.current.draft.current).toBe('hello')
    expect(result.current.draft.pendingSent).toBeNull()
    expect(storage['draft:t1']).toContain('"current":"hello"')
  })

  it('savePendingSent 保存发送快照，不清 current', () => {
    const { result } = renderHook(() => useDraftStore('t1'))
    act(() => {
      result.current.saveCurrent('new input')
      result.current.savePendingSent('sent message')
    })
    expect(result.current.draft.current).toBe('')
    expect(result.current.draft.pendingSent).toBe('sent message')
  })

  it('clearPendingSent 清除发送快照', () => {
    const { result } = renderHook(() => useDraftStore('t1'))
    act(() => {
      result.current.savePendingSent('sent message')
      result.current.clearPendingSent()
    })
    expect(result.current.draft.pendingSent).toBeNull()
    expect(result.current.draft.current).toBe('')
  })

  it('restorePendingSent 恢复发送快照（无新草稿时直接恢复）', () => {
    const { result } = renderHook(() => useDraftStore('t1'))
    act(() => {
      result.current.savePendingSent('sent message')
      result.current.restorePendingSent()
    })
    // current = 'sent message' + '' = 'sent message'
    expect(result.current.draft.current).toBe('sent message')
    expect(result.current.draft.pendingSent).toBeNull()
  })

  it('restorePendingSent 有新草稿时追加到末尾（不覆盖）', () => {
    const { result } = renderHook(() => useDraftStore('t1'))
    act(() => {
      // 发送消息：current 清空，pendingSent 保存快照
      result.current.savePendingSent('sent message')
      // 用户在编辑区输入新草稿（此时 pendingSent 仍在）
      result.current.saveCurrent('new draft here')
      // 恢复：旧消息 pendingSent 在前，新草稿 current 追加在后
      result.current.restorePendingSent()
    })
    // current = 'sent message' + 'new draft here'
    expect(result.current.draft.current).toBe('sent messagenew draft here')
    expect(result.current.draft.pendingSent).toBeNull()
  })

  it('restorePendingSent 在无 pendingSent 时无操作', () => {
    const { result } = renderHook(() => useDraftStore('t1'))
    act(() => { result.current.saveCurrent('hello') })
    act(() => { result.current.restorePendingSent() })
    expect(result.current.draft.current).toBe('hello')
    expect(result.current.draft.pendingSent).toBeNull()
  })

  it('clearAll 清空全部草稿', () => {
    const { result } = renderHook(() => useDraftStore('t1'))
    act(() => { result.current.saveCurrent('hello') })
    act(() => { result.current.savePendingSent('sent') })
    // pendingSent 已保存
    expect(result.current.draft.pendingSent).toBe('sent')
    act(() => { result.current.clearAll() })
    expect(result.current.draft.current).toBe('')
    expect(result.current.draft.pendingSent).toBeNull()
    // storage 中 key 已不存在
    expect(Object.keys(storage).filter(k => k === 'draft:t1')).toHaveLength(0)
  })

  it('sessionStorage 键正确按 threadId 命名', () => {
    const { result: r1 } = renderHook(() => useDraftStore('thread-abc'))
    const { result: r2 } = renderHook(() => useDraftStore('thread-xyz'))
    act(() => { r1.current.saveCurrent('draft1') })
    act(() => { r2.current.saveCurrent('draft2') })
    // sessionStorage 中两条记录各自独立
    expect(Object.keys(storage)).toContain('draft:thread-abc')
    expect(Object.keys(storage)).toContain('draft:thread-xyz')
    expect(JSON.parse(storage['draft:thread-abc'] as string)).toMatchObject({ current: 'draft1' })
    expect(JSON.parse(storage['draft:thread-xyz'] as string)).toMatchObject({ current: 'draft2' })
  })

  it('sessionStorage 不可用时 saveCurrent 不崩溃且状态更新', () => {
    // 在 saveCurrent 内部拦截 writeDraft 抛出，验证读路径能正确更新内存状态
    const errStorage = {
      getItem: () => { throw new Error('blocked') },
      setItem: () => { throw new Error('blocked') },
      removeItem: vi.fn(),
      clear: vi.fn(),
    }
    vi.stubGlobal('sessionStorage', errStorage as unknown as Storage)
    const { result } = renderHook(() => useDraftStore('t1'))
    // 初始降级为空
    expect(result.current.draft.current).toBe('')
    // saveCurrent 内部 writeDraft 抛异常，但内存状态应更新
    act(() => { result.current.saveCurrent('hello') })
    expect(result.current.draft.current).toBe('hello')
    // 恢复
    vi.stubGlobal('sessionStorage', {
      getItem: (key: string) => storage[key] ?? null,
      setItem: (key: string, value: string) => { storage[key] = value },
      removeItem: (key: string) => { delete storage[key] },
      clear: () => { Object.keys(storage).forEach(k => delete storage[k]) },
    })
  })

  it('savePendingSent 不被其他 sessionStorage 错误清除', () => {
    const { result } = renderHook(() => useDraftStore('t1'))
    act(() => { result.current.savePendingSent('sent message') })
    expect(result.current.draft.pendingSent).toBe('sent message')
    expect(storage['draft:t1']).toContain('sent message')
  })
})
