/**
 * ConfirmationCard 组件测试 — TOOL-05 round 2
 *
 * 验证：
 * - 不显示原值（脱敏占位）
 * - 倒计时读 expires_at
 * - 业务状态机：approved / executing（claimed）/ succeeded / failed / unknown
 * - 双击防重：inFlight 时按钮显示"操作中"
 * - 非当前 run 的卡：按钮禁用
 * - offline：按钮不显示
 * - 过期：按钮 disabled
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ConfirmationCard, type OpOutcome } from './ConfirmationCard'
import type { ConfirmationResponse } from '../../shared/types/confirmations'

const baseArgs = {
  activeRunId: 'r-1',
  online: true,
  inFlight: new Set<string>(),
  onDeny: vi.fn(),
  onApproveAndExecute: vi.fn(),
  onVerify: vi.fn(),
}

function makePending(over: Partial<ConfirmationResponse> = {}): ConfirmationResponse {
  return {
    request_id: 'cf-test',
    run_id: 'r-1',
    thread_id: 't-1',
    project_id: null,
    principal: 'MAIN_AGENT',
    tool_name: 'mcp__echo__echo',
    arguments_hash: 'hash-1',
    arguments_display: { m: 'hello', api_key: '••••••' },
    requested_at: Date.now() / 1000,
    expires_at: Date.now() / 1000 + 300,
    state: 'pending',
    decided_at: null,
    decided_by: null,
    executed_at: null,
    execution_result: null,
    ...over,
  }
}

describe('ConfirmationCard', () => {
  it('展示工具名 + 主体', () => {
    render(<ConfirmationCard confirmation={makePending()} {...baseArgs} />)
    expect(screen.getByText('mcp__echo__echo')).toBeInTheDocument()
    expect(screen.getByText('MAIN_AGENT')).toBeInTheDocument()
  })

  it('仅显示 arguments_display（脱敏），不显示原值', () => {
    const display = { m: 'hello', api_key: '••••••' }
    render(
      <ConfirmationCard
        confirmation={makePending({ arguments_display: display })}
        {...baseArgs}
      />,
    )
    expect(screen.getByTestId('arguments-display').textContent).toContain('••••••')
    expect(screen.getByTestId('arguments-display').textContent).not.toContain('sk-')
  })

  it('项目会话显示项目标识', () => {
    render(
      <ConfirmationCard
        confirmation={makePending({ project_id: 'proj-1' })}
        {...baseArgs}
      />,
    )
    expect(screen.getByText('项目')).toBeInTheDocument()
  })

  it('非项目会话不显示项目标识', () => {
    render(
      <ConfirmationCard
        confirmation={makePending({ project_id: null })}
        {...baseArgs}
      />,
    )
    expect(screen.queryByText('项目')).not.toBeInTheDocument()
  })

  it('从 arguments_display 推断 target', () => {
    render(
      <ConfirmationCard
        confirmation={makePending({ arguments_display: { path: '/tmp/a.txt' } })}
        {...baseArgs}
      />,
    )
    expect(screen.getByText('/tmp/a.txt')).toBeInTheDocument()
  })

  it('PENDING 状态显示批准 / 拒绝按钮', () => {
    render(<ConfirmationCard confirmation={makePending()} {...baseArgs} />)
    expect(screen.getByTestId('approve-button')).toBeInTheDocument()
    expect(screen.getByTestId('deny-button')).toBeInTheDocument()
  })

  it('APPROVED 状态显示执行按钮', () => {
    render(
      <ConfirmationCard
        confirmation={makePending({ state: 'approved' })}
        {...baseArgs}
      />,
    )
    expect(screen.getByTestId('execute-button')).toBeInTheDocument()
    expect(screen.queryByTestId('approve-button')).not.toBeInTheDocument()
  })

  it('终态 executed 成功：不提供按钮，显示 ok', () => {
    const { container } = render(
      <ConfirmationCard
        confirmation={makePending({ state: 'executed', execution_result: 'ok' })}
        {...baseArgs}
      />,
    )
    expect(screen.queryByTestId('approve-button')).not.toBeInTheDocument()
    expect(container.textContent).toContain('ok')
  })

  it('executing（claimed）：不提供按钮，显示等待中', () => {
    const { container } = render(
      <ConfirmationCard
        confirmation={makePending({ state: 'executed', execution_result: 'claimed' })}
        {...baseArgs}
      />,
    )
    expect(container.querySelector('[data-business="executing"]')).not.toBeNull()
    expect(screen.queryByTestId('approve-button')).not.toBeInTheDocument()
  })

  it('failed（execution_result 以 error 开头）：显示失败', () => {
    const { container } = render(
      <ConfirmationCard
        confirmation={makePending({
          state: 'executed',
          execution_result: 'error:ValueError',
        })}
        {...baseArgs}
      />,
    )
    expect(container.querySelector('[data-business="failed"]')).not.toBeNull()
    expect(container.textContent).toContain('ValueError')
  })

  it('unknown 业务态：按钮 disabled（不允许重放）', () => {
    const { container } = render(
      <ConfirmationCard
        confirmation={makePending({
          state: 'executed',
          execution_result: 'unknown',
        })}
        {...baseArgs}
      />,
    )
    expect(container.querySelector('[data-business="unknown"]')).not.toBeNull()
    expect(screen.queryByTestId('approve-button')).not.toBeInTheDocument()
  })

  it('offline 时按钮不显示', () => {
    render(
      <ConfirmationCard confirmation={makePending()} {...baseArgs} online={false} />,
    )
    expect(screen.queryByTestId('approve-button')).not.toBeInTheDocument()
  })

  it('activeRunId 与卡片 run 不匹配：按钮不显示 + 提示', () => {
    render(
      <ConfirmationCard
        confirmation={makePending()}
        {...baseArgs}
        activeRunId="r-OTHER"
      />,
    )
    expect(screen.queryByTestId('approve-button')).not.toBeInTheDocument()
    expect(screen.getByText(/非当前活跃 run/)).toBeInTheDocument()
  })

  it('inFlight：提交中按钮显示"操作中"且 disabled，点击无效', async () => {
    const user = userEvent.setup()
    const onApproveAndExecute = vi.fn()
    const onDeny = vi.fn()
    const { container } = render(
      <ConfirmationCard
        confirmation={makePending()}
        {...baseArgs}
        inFlight={new Set(['cf-test'])}
        onApproveAndExecute={onApproveAndExecute}
        onDeny={onDeny}
      />,
    )
    expect(container.querySelector('[data-inflight="true"]')).not.toBeNull()
    expect(screen.getByTestId('inflight-indicator')).toBeInTheDocument()
    const btn = screen.getByTestId('approve-button')
    expect(btn).toBeDisabled()
    await user.click(btn)
    expect(onApproveAndExecute).not.toHaveBeenCalled()
    expect(onDeny).not.toHaveBeenCalled()
  })

  it('过期时按钮不显示 + 显示已过期', () => {
    const past = Date.now() / 1000 - 1
    render(
      <ConfirmationCard
        confirmation={makePending({ expires_at: past })}
        {...baseArgs}
      />,
    )
    expect(screen.queryByTestId('approve-button')).not.toBeInTheDocument()
    expect(screen.getByText('已过期')).toBeInTheDocument()
  })

  it('双击同一按钮：仅发送一次（组件端即时守卫）', async () => {
    const user = userEvent.setup()
    // R6：显式 Promise<OpOutcome> 类型，与回调签名一致
    const onApproveAndExecute = vi.fn((): Promise<OpOutcome> => new Promise(() => {}))
    render(
      <ConfirmationCard
        confirmation={makePending()}
        {...baseArgs}
        onApproveAndExecute={onApproveAndExecute}
      />,
    )
    const btn = screen.getByTestId('approve-button')
    await user.click(btn)
    // 第一次点击已发送，promise 未决，localPendingRef 仍为 true
    // 同一 tick 内再点一次：守卫 ref 已置位，第二次被吞
    await user.click(btn)
    expect(onApproveAndExecute).toHaveBeenCalledTimes(1)
    // 提交中：出现"操作中"指示（localPending 仍为 true）
    expect(screen.getByTestId('inflight-indicator')).toBeInTheDocument()
  })
})
