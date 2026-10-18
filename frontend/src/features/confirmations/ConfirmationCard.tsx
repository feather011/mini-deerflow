/**
 * TOOL-05 ConfirmationCard 组件 — round 2
 *
 * 业务状态机（基于 state + execution_result 派生）：
 * - approved  : PENDING|APPROVED state + 无 error/unknown
 * - executing : state=executed + execution_result='claimed'（后端已领取、handler 还没回）
 * - succeeded : state=executed + execution_result='ok'
 * - failed    : state=executed + execution_result='error:xxx' / state=denied|cancelled|expired
 * - unknown   : 网络错过后结果不明；按钮禁用，提示"结果未知"（与 offline 不同）
 *
 * inFlight：operationId 由父 hook 提供，重复点击同 request 第二次直接吞
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { ConfirmationResponse } from '../../shared/types/confirmations'
import styles from './ConfirmationCard.module.css'

export type OperationId = 'approve' | 'deny' | 'cancel' | 'execute'

export interface ConfirmationCardProps {
  confirmation: ConfirmationResponse
  /** 真实活跃 run_id；卡片按 run 精确控制。null 表示无活跃 run。 */
  activeRunId: string | null
  /** 网络是否在线。 */
  online: boolean
  /** 正在操作的 request_id 集合（用于按钮 disabled 与重复点击守卫）。 */
  inFlight: Set<string>
  onDeny: (id: string) => void
  onApproveAndExecute: (id: string) => void
}

const TERMINAL_STATES = new Set(['denied', 'cancelled', 'expired'])

type BusinessState = 'approved' | 'executing' | 'succeeded' | 'failed' | 'unknown'

function deriveBusinessState(c: ConfirmationResponse): BusinessState {
  if (c.state === 'denied' || c.state === 'cancelled' || c.state === 'expired') {
    return 'failed'
  }
  if (c.state === 'pending' || c.state === 'approved') {
    return 'approved'
  }
  // state == 'executed'
  if (c.execution_result === null || c.execution_result === 'claimed') {
    return 'executing'
  }
  if (c.execution_result === 'ok') return 'succeeded'
  if (c.execution_result.startsWith('error')) return 'failed'
  // 含 'unknown' 或其他无法判定
  return 'unknown'
}

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

const BUSINESS_STATE_LABEL: Record<BusinessState, string> = {
  approved: '待批准',
  executing: '执行中',
  succeeded: '已成功',
  failed: '已失败',
  unknown: '结果未知',
}

const BUSINESS_STATE_COLOR: Record<BusinessState, string> = {
  approved: '#d4a017',
  executing: '#2a6fb3',
  succeeded: '#2a7d2a',
  failed: '#888',
  unknown: '#b30',
}

export function ConfirmationCard({
  confirmation,
  activeRunId,
  online,
  inFlight,
  onDeny,
  onApproveAndExecute,
}: ConfirmationCardProps) {
  const [now, setNow] = useState(() => Date.now() / 1000)
  useEffect(() => {
    if (TERMINAL_STATES.has(confirmation.state) && confirmation.state !== 'pending') {
      // PENDING/EXECUTED/EXECUTING 都需倒计时；纯终态才停
      if (confirmation.state === 'executed') {
        // executed 也可能回到 executing（claimed），继续倒计时
      } else {
        return
      }
    }
    const handle = setInterval(() => setNow(Date.now() / 1000), 1000)
    return () => clearInterval(handle)
  }, [confirmation.state])

  const c = confirmation
  const business = deriveBusinessState(c)
  const isActionable = business === 'approved' && c.state !== 'expired' && !TERMINAL_STATES.has(c.state)
  const runMatches = activeRunId !== null && c.run_id === activeRunId
  const expired = c.expires_at <= now
  // R4：组件端即时提交守卫 — 父级 inFlight 依赖 React 批处理，双击的第二次
  // 事件可能早于状态回写；本地 ref 在第一次点击的同步阶段即置位。
  const localPendingRef = useRef(false)
  const [localPending, setLocalPending] = useState(false)
  const inFlightThis = inFlight.has(c.request_id) || localPending
  const submitGuard = useCallback(
    (fn: (id: string) => void) => (id: string) => {
      if (localPendingRef.current) return
      localPendingRef.current = true
      setLocalPending(true)
      fn(id)
    },
    [],
  )
  const handleApproveAndExecute = submitGuard(onApproveAndExecute)
  const handleDeny = submitGuard(onDeny)

  // 后端状态离开待处理（父级回写）后，重置本地守卫，让卡片可再次操作
  useEffect(() => {
    if (business !== 'approved' && localPendingRef.current) {
      localPendingRef.current = false
      setLocalPending(false)
    }
  }, [business])

  const countdown = formatCountdown(c.expires_at, now)
  const target = deriveTarget(c.arguments_display)
  const isProject = c.project_id !== null && c.project_id !== undefined
  const principal = c.principal

  // 按钮可用性：
  // - approved 业务态 + run 匹配 + 在线 + 未过期 + 未在飞
  // - executing 业务态：按钮 disable（等待后端结果）
  // - 终态 / 失败：按钮隐藏
  // 提交中不隐藏按钮（R4：提交中按钮状态），改为 disabled。
  const showActionButtons = isActionable && runMatches && online && !expired
  const buttonsDisabled = inFlightThis

  return (
    <div
      className={styles.card}
      data-state={c.state}
      data-business={business}
      data-offline={(!online).toString()}
      data-inflight={inFlightThis.toString()}
      role="region"
      aria-label={`工具调用需批准：${c.tool_name}`}
    >
      <div className={styles.header}>
        <span aria-hidden>⚠</span>
        <span>{BUSINESS_STATE_LABEL[business]}</span>
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
          {business === 'approved' ? (
            <>倒计时：<span className={expired ? styles.expired : ''}>{countdown}</span></>
          ) : null}
          {business === 'executing' ? <span>等待后端结果…</span> : null}
          {business === 'succeeded' ? (
            <span>
              已成功：<code>{c.execution_result ?? 'ok'}</code>
            </span>
          ) : null}
          {business === 'failed' && c.state === 'denied' ? <span>已拒绝</span> : null}
          {business === 'failed' && c.state === 'cancelled' ? <span>已取消</span> : null}
          {business === 'failed' && c.state === 'expired' ? <span className={styles.expired}>已过期</span> : null}
          {business === 'failed' && c.state === 'executed' ? (
            <span>失败：<code>{c.execution_result}</code></span>
          ) : null}
          {business === 'unknown' ? (
            <span className={styles.expired}>结果未知（请手动核对）</span>
          ) : null}
        </span>
        <span style={{ color: BUSINESS_STATE_COLOR[business] }}>
          {business}
        </span>
      </div>

      {business === 'unknown' ? (
        <div className={styles.meta}>
          <span>操作结果未确认；按钮已禁用，避免重放</span>
        </div>
      ) : null}

      {isActionable && !runMatches ? (
        <div className={styles.meta}>
          <span>非当前活跃 run（{c.run_id}），按钮已禁用</span>
        </div>
      ) : null}
      {isActionable && !online ? (
        <div className={styles.meta}>
          <span className={styles.expired}>已离线</span>
        </div>
      ) : null}

      {showActionButtons ? (
        <div className={styles.actions}>
          {c.state === 'approved' ? (
            <button
              className={`${styles.btn} ${styles.btnApprove}`}
              data-testid="execute-button"
              data-inflight={buttonsDisabled.toString()}
              onClick={() => handleApproveAndExecute(c.request_id)}
              disabled={buttonsDisabled}
            >
              执行
            </button>
          ) : (
            <>
              <button
                className={`${styles.btn} ${styles.btnApprove}`}
                data-testid="approve-button"
                data-inflight={buttonsDisabled.toString()}
                onClick={() => handleApproveAndExecute(c.request_id)}
              disabled={buttonsDisabled}
              >
                批准并执行
              </button>
              <button
                className={`${styles.btn} ${styles.btnDeny}`}
                data-testid="deny-button"
                data-inflight={buttonsDisabled.toString()}
                onClick={() => handleDeny(c.request_id)}
                disabled={buttonsDisabled}
              >
                拒绝
              </button>
            </>
          )}
        </div>
      ) : null}
      {inFlightThis ? (
        <div className={styles.meta} data-testid="inflight-indicator">
          <span>操作中…</span>
        </div>
      ) : null}
    </div>
  )
}
