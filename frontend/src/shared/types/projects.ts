/**
 * Project API 类型定义（工作模式第 2 步）
 *
 * 描述 HTTP 契约，不依赖后端 Python 领域对象。
 */

/**
 * 项目对外视图。
 */
export interface ProjectSummary {
  project_id: string
  name: string
  repo_path: string
  created_at: string
}

/**
 * GET /api/projects 的响应包装。
 */
export interface ProjectListResponse {
  projects: ProjectSummary[]
}

/**
 * POST /api/projects 请求体。
 */
export interface CreateProjectRequest {
  name: string
}