/**
 * 统一错误文案映射（20F）
 *
 * 设计原则：
 * 1. 前端只依赖稳定的 HTTP 状态码，不依赖后端自然语言文案（后端文案可能变化）。
 * 2. 每个业务上下文（run / file / thread / health）可以覆盖默认文案。
 * 3. 绝不展示 traceback、绝对路径、密钥或内部异常。
 * 4. status=0 表示网络层失败（fetch reject / CORS），不是 HTTP 状态码。
 */

export type ErrorContext =
  | 'run'
  | 'file'
  | 'thread'
  | 'project'
  | 'health'
  | 'generic'

/** 默认文案（未知上下文时使用） */
const DEFAULT_MESSAGES: Record<number, string> = {
  0: '无法连接后端服务',
  400: '请求内容不符合要求',
  404: '请求的资源不存在',
  409: '操作与当前状态冲突',
  413: '内容超出大小限制',
  422: '请求参数校验失败',
  429: '请求过于频繁，请稍候',
  500: '服务内部错误',
  503: '服务暂不可用',
  504: '服务响应超时',
}

/** 各业务上下文的文案覆盖 */
const CONTEXT_MESSAGES: Record<ErrorContext, Partial<Record<number, string>>> = {
  run: {
    409: '当前会话正在执行，请稍候',
    422: '消息内容不符合要求',
    429: '本次运行额度已耗尽',
    500: 'Agent 执行失败',
    503: 'Agent 服务暂不可用',
    504: 'Agent 执行超时',
  },
  file: {
    400: '路径非法或目标不是文件',
    404: '文件不存在',
    409: '同名文件已存在',
    413: '文件超出大小限制',
    500: '文件服务异常',
    503: '文件服务暂不可用',
  },
  thread: {
    404: '会话不存在或已被删除',
    500: '会话服务异常',
    503: '会话服务暂不可用',
  },
  project: {
    400: '仓库路径非法或不是 Git 仓库',
    404: '项目不存在或已被删除',
    409: '该仓库已被其他项目绑定',
    500: '项目服务异常',
    503: '项目服务暂不可用',
  },
  health: {
    503: '后端尚未就绪',
    500: '后端健康检查异常',
  },
  generic: {},
}

/** 各上下文的兜底文案（状态码未命中时） */
const CONTEXT_FALLBACK: Record<ErrorContext, string> = {
  run: '运行失败，请稍后重试',
  file: '操作失败，请稍后重试',
  thread: '加载失败，请稍后重试',
  project: '项目操作失败，请稍后重试',
  health: '后端连接异常',
  generic: '操作失败，请稍后重试',
}

/** 错误对象最小形状（避免强耦合 ApiError） */
export interface ErrorLike {
  status: number
  code?: string
  message?: string
}

/**
 * 根据状态码和业务上下文返回稳定中文文案。
 *
 * @param error  - ApiError 或任何含 status 的对象
 * @param context - 业务上下文，决定文案措辞
 */
export function getErrorMessage(
  error: ErrorLike,
  context: ErrorContext = 'generic',
): string {
  const override = CONTEXT_MESSAGES[context][error.status]
  if (override) return override

  const fallback = DEFAULT_MESSAGES[error.status]
  if (fallback) return fallback

  // 未知状态码：优先用后端文案，但仅在它不像内部异常时才展示
  const backendMessage = error.message?.trim()
  if (backendMessage && isSafeToDisplay(backendMessage)) {
    return backendMessage
  }

  return CONTEXT_FALLBACK[context]
}

/**
 * 判断后端文案是否可安全展示给用户。
 *
 * 过滤掉泄漏内部实现的文案（traceback 片段、绝对路径、Python 异常类名）。
 */
export function isSafeToDisplay(message: string): boolean {
  // Python traceback 片段
  if (/Traceback \(most recent call last\)/i.test(message)) return false
  // 内部异常类名，如 ValueError / KeyError
  if (/\b\w*(Error|Exception)\b\s*:/.test(message)) return false
  // Windows / POSIX 绝对路径
  if (/[A-Za-z]:\\|\/(?:home|root|usr|var|etc|tmp)\//.test(message)) return false
  // 疑似密钥
  if (/(sk-[A-Za-z0-9]{16,}|api[_-]?key\s*[:=])/i.test(message)) return false
  return true
}
