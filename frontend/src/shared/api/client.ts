/**
 * API Client — 所有 HTTP 请求的唯一出口
 *
 * 职责：
 * - 统一处理网络错误、状态码、JSON 解析、Request ID 提取
 * - Feature 只描述业务动作，不重复处理底层细节
 *
 * 不在此文件处理：
 * - 业务状态（由 TanStack Query 管理）
 * - 路由（由 React Router 管理）
 * - UI 渲染（由 React 组件管理）
 */
import { API_BASE_URL } from './config'

// ---------------------------------------------------------------------------
// ApiError — 统一错误类型
// ---------------------------------------------------------------------------

export class ApiError extends Error {
  /** HTTP 状态码 */
  status: number
  /** 后端稳定错误码（如 NOT_FOUND、INTERNAL_ERROR） */
  code: string
  /** 请求 ID，可关联后端日志 */
  requestId?: string
  /** 字段级详情（仅 422，其余为 null） */
  details: unknown
  /** 对用户安全的错误文案 */
  override message: string

  constructor(opts: {
    status: number
    code: string
    message: string
    requestId?: string
    details?: unknown
  }) {
    super(opts.message)
    this.name = 'ApiError'
    this.status = opts.status
    this.code = opts.code
    this.message = opts.message
    this.requestId = opts.requestId
    this.details = opts.details
  }
}

// ---------------------------------------------------------------------------
// 内部辅助
// ---------------------------------------------------------------------------

/** 从响应头或错误体中提取 Request ID（优先 header） */
function extractRequestId(headers: Headers, body: unknown): string | undefined {
  const fromHeader = headers.get('X-Request-ID')
  if (fromHeader) return fromHeader
  if (
    body !== null &&
    typeof body === 'object' &&
    'error' in body &&
    typeof (body as { error?: unknown }).error === 'object' &&
    (body as { error: { request_id?: string } }).error !== null
  ) {
    return (body as { error: { request_id?: string } }).error.request_id
  }
  return undefined
}

/** 解析 JSON，失败时返回 null（不崩溃） */
function parseJson(text: string): unknown | null {
  try {
    return JSON.parse(text)
  } catch {
    return null
  }
}

// ---------------------------------------------------------------------------
// 核心请求函数
// ---------------------------------------------------------------------------

/** 请求选项 */
interface RequestOptions {
  /** 中断信号 */
  signal?: AbortSignal
  /** JSON 请求体（仅 POST/PATCH/PUT 使用） */
  body?: unknown
}

/**
 * 统一请求函数，支持 GET / POST / DELETE
 *
 * @param path   - 相对路径，自动拼接到 API_BASE_URL
 * @param method - HTTP 方法
 * @param options - signal / body
 * @returns 解析后的 JSON 数据（204 返回 undefined）
 * @throws ApiError - 网络错误（status=0）或 HTTP 错误（4xx/5xx）
 */
export async function apiRequest<T>(
  path: string,
  method: 'GET' | 'POST' | 'DELETE',
  options: RequestOptions = {},
): Promise<T> {
  const url = `${API_BASE_URL}${path}`

  /** 只有携带 JSON body 时才设置 Content-Type */
  const hasBody = options.body !== undefined
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (hasBody) {
    headers['Content-Type'] = 'application/json'
  }

  let response: Response
  try {
    response = await fetch(url, {
      method,
      signal: options.signal,
      headers,
      ...(hasBody ? { body: JSON.stringify(options.body) } : {}),
    })
  } catch {
    throw new ApiError({
      status: 0,
      code: 'NETWORK_ERROR',
      message: '无法连接到后端服务，请检查服务是否运行。',
    })
  }

  // 204 无响应体
  if (response.status === 204) {
    return undefined as T
  }

  const text = await response.text()
  const body = parseJson(text)

  if (!response.ok) {
    const requestId = extractRequestId(response.headers, body)
    const errBody = body as {
      error?: { code?: string; message?: string; details?: unknown }
    } | null

    throw new ApiError({
      status: response.status,
      code: errBody?.error?.code ?? 'UNKNOWN_ERROR',
      message:
        errBody?.error?.message ??
        (response.status === 0
          ? '无法连接到后端服务'
          : `请求失败（${response.status}）`),
      requestId,
      details: errBody?.error?.details ?? null,
    })
  }

  // 200 / 201 返回 JSON
  return body as T
}

// ---------------------------------------------------------------------------
// 便捷方法（底层调用统一请求）
// ---------------------------------------------------------------------------

/** GET 请求 */
export async function apiGet<T>(
  path: string,
  options: Omit<RequestOptions, 'body'> = {},
): Promise<T> {
  return apiRequest<T>(path, 'GET', options)
}

/** POST 请求（201 → JSON 响应） */
export async function apiPost<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  return apiRequest<T>(path, 'POST', options)
}

/** DELETE 请求（204 → undefined） */
export async function apiDelete(
  path: string,
  options: Omit<RequestOptions, 'body'> = {},
): Promise<void> {
  await apiRequest<void>(path, 'DELETE', options)
}

// ---------------------------------------------------------------------------
// FormData / Blob 扩展（20E）
// ---------------------------------------------------------------------------

/**
 * Multipart 上传请求。
 *
 * 不手动设置 Content-Type，由浏览器生成 multipart boundary。
 * 成功响应（201）按 JSON 解析；错误响应沿用 ApiError 规范。
 */
export async function apiUpload<T>(
  path: string,
  formData: FormData,
  options: Omit<RequestOptions, 'body'> = {},
): Promise<T> {
  const url = `${API_BASE_URL}${path}`
  const headers: Record<string, string> = { Accept: 'application/json' }

  let response: Response
  try {
    response = await fetch(url, {
      method: 'POST',
      signal: options.signal,
      headers,
      body: formData,
    })
  } catch {
    throw new ApiError({
      status: 0,
      code: 'NETWORK_ERROR',
      message: '无法连接到后端服务，请检查服务是否运行。',
    })
  }

  const text = await response.text()
  const body = parseJson(text)

  if (!response.ok) {
    const requestId = extractRequestId(response.headers, body)
    const errBody = body as {
      error?: { code?: string; message?: string; details?: unknown }
    } | null
    throw new ApiError({
      status: response.status,
      code: errBody?.error?.code ?? 'UNKNOWN_ERROR',
      message:
        errBody?.error?.message ??
        (response.status === 0
          ? '无法连接到后端服务'
          : `请求失败（${response.status}）`),
      requestId,
      details: errBody?.error?.details ?? null,
    })
  }

  return body as T
}

/**
 * 下载二进制资源，返回 Blob。
 *
 * 不读取响应体文本，错误状态下仍按 JSON 解析。
 */
export async function apiDownloadBlob(
  path: string,
  options: Omit<RequestOptions, 'body'> = {},
): Promise<Blob> {
  const url = `${API_BASE_URL}${path}`

  let response: Response
  try {
    response = await fetch(url, {
      method: 'GET',
      signal: options.signal,
    })
  } catch {
    throw new ApiError({
      status: 0,
      code: 'NETWORK_ERROR',
      message: '无法连接到后端服务，请检查服务是否运行。',
    })
  }

  if (!response.ok) {
    // 错误体仍是 JSON，读取以提取错误信息
    const text = await response.text()
    const body = parseJson(text)
    const requestId = extractRequestId(response.headers, body)
    const errBody = body as {
      error?: { code?: string; message?: string; details?: unknown }
    } | null
    throw new ApiError({
      status: response.status,
      code: errBody?.error?.code ?? 'UNKNOWN_ERROR',
      message:
        errBody?.error?.message ?? `请求失败（${response.status}）`,
      requestId,
      details: errBody?.error?.details ?? null,
    })
  }

  return response.blob()
}
