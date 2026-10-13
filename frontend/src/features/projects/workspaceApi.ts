/**
 * Project Workspace API 调用函数
 *
 * 用于浏览项目工作区文件、读取文件内容等。
 */
import { apiGet } from '../../shared/api/client'

export interface BrowseEntry {
  name: string
  is_dir: boolean
  size: number
  modified: string | null
}

export interface BrowseResponse {
  path: string
  is_root: boolean
  entries: BrowseEntry[]
  truncated: boolean
}

export interface FileContentResponse {
  path: string
  content: string
  truncated: boolean
  total_lines: number | null
}

/** GET /api/projects/{project_id}/workspace/browse — 浏览工作区目录 */
export async function browseWorkspace(
  projectId: string,
  path?: string,
  signal?: AbortSignal,
): Promise<BrowseResponse> {
  const params = path ? `?path=${encodeURIComponent(path)}` : ''
  return apiGet<BrowseResponse>(
    `/api/projects/${encodeURIComponent(projectId)}/workspace/browse${params}`,
    { signal }
  )
}

/** GET /api/projects/{project_id}/workspace/file — 读取文件内容 */
export async function getFileContent(
  projectId: string,
  path: string,
  signal?: AbortSignal,
): Promise<FileContentResponse> {
  return apiGet<FileContentResponse>(
    `/api/projects/${encodeURIComponent(projectId)}/workspace/file?path=${encodeURIComponent(path)}`,
    { signal }
  )
}
