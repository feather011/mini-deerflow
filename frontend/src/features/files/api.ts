/**
 * Files / Artifacts HTTP 层（20E）
 *
 * 不在组件中直接调用 fetch。
 * 路径段独立编码后再用 `/` 连接，前端编码不是安全边界。
 */
import { apiGet, apiDelete, apiUpload, apiDownloadBlob } from '../../shared/api'
import type { FileListResponse, FileMetadata } from '../../shared/types/files'

// ---------------------------------------------------------------------------
// 路径编码
// ---------------------------------------------------------------------------

/**
 * 将逻辑路径按 segment 编码后用 `/` 连接。
 *
 * 例：
 * - "subdir/file.txt" → "subdir/file.txt"
 * - "sub dir/file name.txt" → "sub%20dir/file%20name.txt"
 * - "a/b/c.json" → "a/b/c.json"
 *
 * 注意：这是 URL 编码，不是安全边界。后端继续校验路径穿越。
 */
export function encodeFilePath(path: string): string {
  return path
    .split('/')
    .map((segment) => encodeURIComponent(segment))
    .join('/')
}

// ---------------------------------------------------------------------------
// 路径处理
// ---------------------------------------------------------------------------

/**
 * 将 FileMetadata.path 转为相对某个根目录的子路径。
 *
 * 后端列表接口的 FileMetadata.path 总是带根前缀
 * （"uploads/subdir/foo.txt"、"artifacts/report.txt"），但下载/删除路由
 * 期待的是相对该根的路径（"subdir/foo.txt"、"report.txt"）。
 *
 * 不剥掉前缀会拼出 /api/artifacts/artifacts/x.txt 这类 URL → 后端 404。
 *
 * 该前缀剥离与 encodeFilePath 一样只是 URL 组装细节，
 * 不是安全边界——路径穿越仍由后端校验。
 */
function stripRootPrefix(path: string, root: 'uploads' | 'artifacts'): string {
  const prefix = `${root}/`
  if (path.startsWith(prefix)) return path.slice(prefix.length)
  return path
}

function stripUploadsPrefix(path: string): string {
  return stripRootPrefix(path, 'uploads')
}

// ---------------------------------------------------------------------------
// 列表
// ---------------------------------------------------------------------------

export function listUploads(signal?: AbortSignal): Promise<FileListResponse> {
  return apiGet<FileListResponse>('/api/files', { signal })
}

export function listArtifacts(signal?: AbortSignal): Promise<FileListResponse> {
  return apiGet<FileListResponse>('/api/artifacts', { signal })
}

// ---------------------------------------------------------------------------
// 上传（FormData）
// ---------------------------------------------------------------------------

export function uploadFile(
  file: File,
  dest?: string,
  signal?: AbortSignal,
): Promise<FileMetadata> {
  const formData = new FormData()
  formData.append('file', file)
  const query = dest ? `?dest=${encodeURIComponent(dest)}` : ''
  return apiUpload<FileMetadata>(`/api/files${query}`, formData, { signal })
}

// ---------------------------------------------------------------------------
// 下载（Blob）
// ---------------------------------------------------------------------------

export function downloadFile(
  path: string,
  signal?: AbortSignal,
): Promise<Blob> {
  return apiDownloadBlob(
    `/api/files/${encodeFilePath(stripUploadsPrefix(path))}`,
    { signal },
  )
}

export function downloadArtifact(
  path: string,
  signal?: AbortSignal,
): Promise<Blob> {
  return apiDownloadBlob(
    `/api/artifacts/${encodeFilePath(stripRootPrefix(path, 'artifacts'))}`,
    { signal },
  )
}

// ---------------------------------------------------------------------------
// 删除（204）
// ---------------------------------------------------------------------------

export function deleteFile(
  path: string,
  signal?: AbortSignal,
): Promise<void> {
  return apiDelete(`/api/files/${encodeFilePath(stripUploadsPrefix(path))}`, {
    signal,
  })
}
