/**
 * TOOL-05 单个确认卡
 *
 * 展示（不展示原值）：
 * - 工具名
 * - 主体 + 项目标识（项目会话显示）
 * - 操作目标（从 arguments_display 推断：path / url / command）
 * - 参数（脱敏后）
 * - 倒计时（读后端 expires_at；归零前端不调任何接口）
 * - Approve / Deny 按钮
 *
 * 终态（approved/executed/denied/expired/cancelled）展示为单行，
 * 不再提供按钮。executed 状态额外显示 execution_result。
 */
import { useEffect, useState } from 'react'
import type { ConfirmationResponse } from '../../shared/types/confirmations'
import styles from './ConfirmationCard.module.css'

export interface ConfirmationCardProps {
  confirmation: ConfirmationResponse
  /** 是否有活跃 run（决定是否启用 Approve / Deny 按钮）。 */
  hasActiveRun: boolean
  /** 网络是否在线（offline 时按钮禁用）。 */
  online: boolean
  onDeny: (id: string) => void
  onApproveAndExecute: (id: string) => void
}

const TERMINAL_STATES = new Set(['executed', 'denied', 'cancelled', 'expired'])

function formatCountdown(expiresAt: number, now: number): string {
  const remaining = Math.max(0, expiresAt - now)
  if (remaining === 0) return '已过期'
  const m = Math.floor(remaining / 60)
  const s = Math.floor(remaining % 60)
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

function deriveTarget(args: Record<string, unknown>): string | null {
  if (typeof args.path === 'string') return args.path
  if (typeof args.url === 'string') return args.url
  if (typeof args.command === 'string') return args.command
  if (typeof args.target === 'string') return args.target
  if (typeof args.query === 'string') return args.query
  return null
}

export function ConfirmationCard({
  confirmation,
  hasActiveRun,
  online,
  onDeny,
  onApproveAndExecute,
}: ConfirmationCardProps) {
  const [now, setNow] = useState(() => Date.now() / 1000)
  useEffect(() => {
    if (TERMINAL_STATES.has(confirmation.state)) return
    const handle = setInterval(() => setNow(Date.now() / 1000), 1000)
    return () => clearInterval(handle)
  }, [confirmation.state])

  const c = confirmation
  const isTerminal = TERMINAL_STATES.has(c.state)
  const isApproved = c.state === 'approved'
  const countdown = formatCountdown(c.expires_at, now)
  const expired = c.expires_at <= now

  const target = deriveTarget(c.arguments_display)
  const isProject = c.project_id !== null && c.project_id !== undefined
  const principal = c.principal

  return (
    <div
      className={styles.card}
      data-state={c.state}
      data-offline={(!online).toString()}
      role="region"
      aria-label={`工具调用需批准：${c.tool_name}`}
    >
      <div className={styles.header}>
        <span aria-hidden>⚠</span>
        <span>工具调用需批准</span>
        <span className={styles.toolName}>{c.tool_name}</span>
        <span className={styles.principal} title="授权主体">{principal}</span>
        {isProject ? (
          <span className={styles.projectTag} title="项目会话">项目</span>
        ) : null}
      </div>

      {target !== null ? (
        <div className={styles.summary}>
          目标：<code>{target}</code>
        </div>
      ) : null}

      <div className={styles.summary}>参数（脱敏）：</div>
      <pre className={styles.params} data-testid="arguments-display">
        {JSON.stringify(c.arguments_display, null, 2)}
      </pre>

      <div className={styles.meta}>
        <span>
          倒计时：<span className={expired ? styles.expired : ''}>{countdown}</span>
        </span>
        <span>state={c.state}</span>
      </div>

      {c.state === 'executed' && c.execution_result ? (
        <div className={styles.meta}>
          <span>结果：<code>{c.execution_result}</code></span>
        </div>
      ) : null}
      {c.state === 'denied' ? (
        <div className={styles.meta}><span>已拒绝</span></div>
      ) : null}
      {c.state === 'cancelled' ? (
        <div className={styles.meta}><span>已取消</span></div>
      ) : null}
      {c.state === 'expired' ? (
        <div className={styles.meta}><span className={styles.expired}>已过期</span></div>
      ) : null}

      {!isTerminal && hasActiveRun && online ? (
        <div className={styles.actions}>
          {isApproved ? (
            <button
              className={`${styles.btn} ${styles.btnApprove}`}
              data-testid="execute-button"
              onClick={() => onApproveAndExecute(c.request_id)}
              disabled={expired}
            >
              执行
            </button>
          ) : (
            <>
              <button
                className={`${styles.btn} ${styles.btnApprove}`}
                data-testid="approve-button"
                onClick={() => onApproveAndExecute(c.request_id)}
                disabled={expired}
              >
                批准并执行
              </button>
              <button
                className={`${styles.btn} ${styles.btnDeny}`}
                data-testid="deny-button"
                onClick={() => onDeny(c.request_id)}
                disabled={expired}
              >
                拒绝
              </button>
            </>
          )}
        </div>
      ) : null}
      {!isTerminal && !hasActiveRun ? (
        <div className={styles.meta}><span>无活跃运行</span></div>
      ) : null}
      {!isTerminal && !online ? (
        <div className={styles.meta}><span className={styles.expired}>已离线</span></div>
      ) : null}
    </div>
  )
}
