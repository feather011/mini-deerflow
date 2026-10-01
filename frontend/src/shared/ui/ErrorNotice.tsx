/**
 * ErrorNotice — 统一错误展示组件（20F）
 *
 * 职责：
 * - 展示稳定中文文案（由 getErrorMessage 按上下文解析）
 * - 展示错误码（后端稳定机器码，便于报障）
 * - 展示 Request ID（关联后端日志）
 * - 按需提供重试 / 关闭操作（仅在回调存在时渲染按钮）
 *
 * 安全约束：
 * - 绝不渲染 traceback、绝对路径、密钥或内部异常
 * - 文案一律经过 getErrorMessage 过滤
 */
import { getErrorMessage, type ErrorContext } from '../errors'
import type { ApiError } from '../api/client'
import styles from './ErrorNotice.module.css'

export type ErrorNoticeTone = 'error' | 'warning' | 'info'

/** 可接受的错误形状：统一的 ApiError，或内联的轻量对象 */
export interface ErrorNoticeSource {
  status: number
  code?: string
  message?: string
  requestId?: string
}

interface ErrorNoticeProps {
  /** ApiError 或任何含 status 的对象 */
  error: ApiError | ErrorNoticeSource
  /** 业务上下文，决定文案措辞 */
  context?: ErrorContext
  /** 视觉语气，默认 error */
  tone?: ErrorNoticeTone
  /** 覆盖自动解析的标题 */
  title?: string
  /** 覆盖自动解析的正文；适合非 HTTP 失败（如用户主动停止） */
  description?: string
  /** 点击重试时调用；不传则不渲染重试按钮 */
  onRetry?: () => void
  /** 点击关闭时调用；不传则不渲染关闭按钮 */
  onDismiss?: () => void
  /** 重试按钮文案 */
  retryLabel?: string
  /** 关闭按钮文案 */
  dismissLabel?: string
  /** 无障碍标签；默认 "错误提示" */
  ariaLabel?: string
}

export function ErrorNotice({
  error,
  context = 'generic',
  tone = 'error',
  title,
  description,
  onRetry,
  onDismiss,
  retryLabel = '重试',
  dismissLabel = '关闭',
  ariaLabel = '错误提示',
}: ErrorNoticeProps) {
  const message = description ?? getErrorMessage(error, context)
  const hasRequestId = Boolean(error.requestId)

  return (
    <div
      className={`${styles.notice} ${styles[tone]}`}
      role="alert"
      aria-label={ariaLabel}
      data-testid="error-notice"
    >
      <div className={styles.body}>
        {title && <p className={styles.title}>{title}</p>}
        <p className={styles.message}>{message}</p>

        {/* 错误码与 Request ID 放在次要行，便于用户报障时提供 */}
        {(error.code || hasRequestId) && (
          <p className={styles.meta}>
            {error.code && (
              <span className={styles.code}>错误码：{error.code}</span>
            )}
            {error.requestId && (
              <span className={styles.requestId} data-testid="error-request-id">
                请求编号：{error.requestId}
              </span>
            )}
          </p>
        )}
      </div>

      {/* 仅在回调确实可用时才渲染操作按钮 */}
      {(onRetry || onDismiss) && (
        <div className={styles.actions}>
          {onRetry && (
            <button
              type="button"
              className={styles.retryBtn}
              onClick={onRetry}
            >
              {retryLabel}
            </button>
          )}
          {onDismiss && (
            <button
              type="button"
              className={styles.dismissBtn}
              onClick={onDismiss}
            >
              {dismissLabel}
            </button>
          )}
        </div>
      )}
    </div>
  )
}
