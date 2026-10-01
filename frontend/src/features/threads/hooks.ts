/**
 * Thread TanStack Query Hooks
 *
 * 职责：
 * - 封装所有 Thread 相关数据请求
 * - 管理缓存失效（创建后刷新列表，删除后刷新列表并清除详情）
 * - 不直接操作 React Router；由调用方决定导航时机
 *
 * 不做乐观更新，等待后端确认成功后再更新界面。
 */
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import type { ThreadDetail } from '../../shared/types/threads'
import type { ApiError } from '../../shared/api/client'
import {
  listThreads,
  createThread,
  getThread,
  deleteThread,
} from './api'
import { runAgent } from './runsApi'
import { threadKeys } from './queryKeys'

// ---------------------------------------------------------------------------
// useThreads — 列表
// ---------------------------------------------------------------------------

export function useThreads() {
  return useQuery<ThreadDetail[], ApiError>({
    queryKey: threadKeys.list(),
    queryFn: async ({ signal }) => {
      const res = await listThreads(signal)
      // 列表项只有 thread_id，获取详情用于渲染
      return Promise.all(
        res.threads.map((s) => getThread(s.thread_id, signal)),
      )
    },
  })
}

// ---------------------------------------------------------------------------
// useThread — 单条详情
// ---------------------------------------------------------------------------

export function useThread(threadId: string | null) {
  return useQuery<ThreadDetail, ApiError>({
    queryKey: threadKeys.detail(threadId ?? 'null'),
    queryFn: async ({ signal }) => {
      // enabled=false 时 TanStack 不会执行，这里仅为满足类型收窄
      if (threadId === null) {
        throw new Error('threadId 未设置')
      }
      return getThread(threadId, signal)
    },
    // threadId 为 null 时不发起请求
    enabled: threadId !== null,
  })
}

// ---------------------------------------------------------------------------
// useCreateThread — 创建
// ---------------------------------------------------------------------------

export interface CreateThreadResult {
  thread: ThreadDetail
}

export function useCreateThread() {
  const queryClient = useQueryClient()

  return useMutation<ThreadDetail, ApiError, { signal?: AbortSignal }>({
    mutationFn: async ({ signal }: { signal?: AbortSignal } = {}) =>
      createThread(undefined, signal),

    onSuccess: () => {
      // 创建只改变 Thread 列表，已有 Thread 详情仍然有效
      void queryClient.invalidateQueries({ queryKey: threadKeys.list() })
    },
  })
}

// ---------------------------------------------------------------------------
// useDeleteThread — 删除
// ---------------------------------------------------------------------------

export function useDeleteThread() {
  const queryClient = useQueryClient()

  return useMutation<
    void,
    ApiError,
    { threadId: string; signal?: AbortSignal }
  >({
    mutationFn: async ({
      threadId,
      signal,
    }: {
      threadId: string
      signal?: AbortSignal
    }) => deleteThread(threadId, signal),

    onSuccess: (_data, { threadId }) => {
      // 先取消正在进行的详情请求（避免删除成功后旧请求返回）
      void queryClient.cancelQueries({ queryKey: threadKeys.detail(threadId) })
      // 移除被删除 Thread 的详情缓存
      void queryClient.removeQueries({ queryKey: threadKeys.detail(threadId) })
      // 删除只改变列表，重新获取列表
      void queryClient.invalidateQueries({ queryKey: threadKeys.list() })
    },
  })
}

// ---------------------------------------------------------------------------
// useAgentRun — Agent 执行（20C）
// ---------------------------------------------------------------------------

export function useAgentRun(threadId: string) {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: async ({
      message,
      signal,
    }: {
      message: string
      signal?: AbortSignal
    }) => runAgent(threadId, { message }, signal),

    onSuccess: () => {
      // Run 成功后刷新当前 Thread 详情（包含新增的 user + assistant 消息）
      void queryClient.invalidateQueries({ queryKey: threadKeys.detail(threadId) })
    },
    // 失败时不刷新，消息历史保持不变
  })
}
