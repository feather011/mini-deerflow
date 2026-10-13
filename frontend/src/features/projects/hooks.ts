/**
 * Project TanStack Query Hooks
 *
 * 不做乐观更新：等项目接口确认成功后再更新界面（与 threads 侧一致）。
 */
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import type { ApiError } from '../../shared/api/client'
import type { ProjectSummary } from '../../shared/types/projects'
import type { ThreadDetail } from '../../shared/types/threads'
import {
  createProject,
  deleteProject,
  listProjects,
} from './api'
import {
  getProjectThreads,
  createThread,
  getThread,
  deleteThread,
} from '../threads/api'
import { projectKeys } from './queryKeys'
import { threadKeys } from '../threads/queryKeys'

// ---------------------------------------------------------------------------
// useProjects — 列表
// ---------------------------------------------------------------------------

export function useProjects() {
  return useQuery<ProjectSummary[], ApiError>({
    queryKey: projectKeys.list(),
    queryFn: async ({ signal }) => {
      const res = await listProjects(signal)
      return res.projects
    },
  })
}

// ---------------------------------------------------------------------------
// useCreateProject — 创建
// ---------------------------------------------------------------------------

export function useCreateProject() {
  const queryClient = useQueryClient()

  return useMutation<
    ProjectSummary,
    ApiError,
    { name: string }
  >({
    mutationFn: async ({ name }) => createProject({ name }),

    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: projectKeys.list() })
    },
  })
}

// ---------------------------------------------------------------------------
// useDeleteProject — 删除
// ---------------------------------------------------------------------------

export function useDeleteProject() {
  const queryClient = useQueryClient()

  return useMutation<void, ApiError, { projectId: string }>({
    mutationFn: async ({ projectId }) => deleteProject(projectId),

    onSuccess: () => {
      // 后端会级联删掉该项目下的会话，因此会话列表也必须刷新——
      // 否则侧栏会留下一批指向已消失项目的孤儿会话。
      void queryClient.invalidateQueries({ queryKey: threadKeys.list() })
      void queryClient.invalidateQueries({ queryKey: projectKeys.list() })
    },
  })
}

// ---------------------------------------------------------------------------
// useProjectThreads — 项目内线程列表
// ---------------------------------------------------------------------------

export function useProjectThreads(projectId: string | null) {
  return useQuery<ThreadDetail[], ApiError>({
    queryKey: projectKeys.threads(projectId ?? 'null'),
    queryFn: async ({ signal }: { signal?: AbortSignal }) => {
      if (!projectId) return []
      const res = await getProjectThreads(projectId, signal)
      // 获取完整详情（包含 messages 用于显示标题）
      const threads = await Promise.all(
        res.threads.map((t) => getThread(t.thread_id, signal)),
      )
      return threads
    },
    enabled: projectId !== null,
  })
}

// ---------------------------------------------------------------------------
// useCreateProjectThread — 在项目内创建会话
// ---------------------------------------------------------------------------

export function useCreateProjectThread() {
  const queryClient = useQueryClient()

  return useMutation<ThreadDetail, ApiError, { projectId: string }>({
    mutationFn: async ({ projectId }) => createThread({ project_id: projectId }),

    onSuccess: (_data, { projectId }) => {
      void queryClient.invalidateQueries({ queryKey: projectKeys.threads(projectId) })
    },
  })
}

// ---------------------------------------------------------------------------
// useDeleteProjectThread — 删除项目内会话
// ---------------------------------------------------------------------------

export function useDeleteProjectThread() {
  const queryClient = useQueryClient()

  return useMutation<void, ApiError, { threadId: string; projectId: string }>({
    mutationFn: async ({ threadId }) => deleteThread(threadId),

    onSuccess: (_data, { projectId, threadId }) => {
      void queryClient.invalidateQueries({ queryKey: projectKeys.threads(projectId) })
      void queryClient.invalidateQueries({ queryKey: threadKeys.detail(threadId) })
      void queryClient.invalidateQueries({ queryKey: threadKeys.list() })
    },
  })
}
