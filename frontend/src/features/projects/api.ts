/**
 * Project API 调用函数
 *
 * 与 `features/threads/api.ts` 同风格：只封装 HTTP，不碰状态与缓存。
 */
import type {
  CreateProjectRequest,
  ProjectListResponse,
  ProjectSummary,
} from '../../shared/types/projects'
import { apiGet, apiPost, apiDelete } from '../../shared/api/client'

function projectPath(projectId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}`
}

/** POST /api/projects — 创建项目（201） */
export async function createProject(
  request: CreateProjectRequest,
  signal?: AbortSignal,
): Promise<ProjectSummary> {
  return apiPost<ProjectSummary>('/api/projects', { signal, body: request })
}

/** GET /api/projects — 列出项目 */
export async function listProjects(
  signal?: AbortSignal,
): Promise<ProjectListResponse> {
  return apiGet<ProjectListResponse>('/api/projects', { signal })
}

/** GET /api/projects/{project_id} — 读取详情 */
export async function getProject(
  projectId: string,
  signal?: AbortSignal,
): Promise<ProjectSummary> {
  return apiGet<ProjectSummary>(projectPath(projectId), { signal })
}

/** DELETE /api/projects/{project_id} — 删除并级联删会话（204） */
export async function deleteProject(
  projectId: string,
  signal?: AbortSignal,
): Promise<void> {
  return apiDelete(projectPath(projectId), { signal })
}