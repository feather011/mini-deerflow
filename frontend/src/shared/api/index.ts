/**
 * API Client — 所有 HTTP 请求的唯一出口
 *
 * 职责：
 * - 统一处理网络错误、状态码、JSON 解析、Request ID 提取
 * - Feature 只描述业务动作，不重复处理底层细节
 */
export { apiGet, apiPost, apiDelete, apiUpload, apiDownloadBlob } from './client'

export type { ApiError } from './client'
