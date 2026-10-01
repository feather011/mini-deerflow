/**
 * Files / Artifacts TanStack Query Hooks（20E）
 *
 * 缓存策略：
 * - uploads 列表：创建/删除 uploads 后失效
 * - artifacts 列表：只读，无变更操作
 * - 不刷新 Thread 详情
 */
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import type { ApiError } from '../../shared/api'
import type { FileListResponse } from '../../shared/types/files'
import {
  listUploads,
  listArtifacts,
  uploadFile,
  deleteFile,
} from './api'
import { fileKeys } from './queryKeys'

// ---------------------------------------------------------------------------
// useUploads — uploads 列表
// ---------------------------------------------------------------------------

export function useUploads() {
  return useQuery<FileListResponse, ApiError>({
    queryKey: fileKeys.uploads(),
    queryFn: async ({ signal }) => listUploads(signal),
  })
}

// ---------------------------------------------------------------------------
// useArtifacts — artifacts 列表（只读）
// ---------------------------------------------------------------------------

export function useArtifacts() {
  return useQuery<FileListResponse, ApiError>({
    queryKey: fileKeys.artifacts(),
    queryFn: async ({ signal }) => listArtifacts(signal),
  })
}

// ---------------------------------------------------------------------------
// useUploadFile — 上传
// ---------------------------------------------------------------------------

export function useUploadFile() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({
      file,
      dest,
      signal,
    }: {
      file: File
      dest?: string
      signal?: AbortSignal
    }) => uploadFile(file, dest, signal),

    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: fileKeys.uploads() })
    },
  })
}

// ---------------------------------------------------------------------------
// useDeleteFile — 删除
// ---------------------------------------------------------------------------

export function useDeleteFile() {
  const queryClient = useQueryClient()
  return useMutation<void, ApiError, { path: string; signal?: AbortSignal }>({
    mutationFn: async ({ path, signal }) => deleteFile(path, signal),

    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: fileKeys.uploads() })
    },
  })
}
