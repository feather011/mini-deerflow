/**
 * 后端状态展示组件
 *
 * 职责：
 * - 根据 BackendStatus 显示对应文案和图标
 * - 提供手动重试按钮
 * - 显示 Request ID（供调试用）
 * - 不展示堆栈或内部路径
 */
import { useBackendStatus } from './useBackendStatus'
import styles from './BackendStatusBar.module.css'

/** 状态文案映射 */
const STATUS_LABELS: Record<string, string> = {
  checking: '连接中…',
  ready: '就绪',
  'not-ready': '后端启动中…',
  offline: '后端离线',
  error: '后端异常',
}

export function BackendStatusBar() {
  const { status, requestId } = useBackendStatus()

  return (
    <header className={styles.bar} role="status" aria-live="polite">
      <div className={styles.left}>
        <span
          className={`${styles.dot} ${styles[status]}`}
          aria-hidden="true"
        />
        <span className={styles.label}>{STATUS_LABELS[status] ?? status}</span>
      </div>

      {requestId && (
        <div className={styles.debug} aria-label={`请求 ID: ${requestId}`}>
          <span className={styles.debugLabel}>ID:</span>
          <code className={styles.debugId}>{requestId}</code>
        </div>
      )}

      {(status === 'offline' || status === 'error') && (
        <button
          type="button"
          className={styles.retryBtn}
          onClick={() => window.location.reload()}
        >
          重试
        </button>
      )}
    </header>
  )
}
