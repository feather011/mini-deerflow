/**
 * ConfirmationCard 组件测试 — TOOL-05
 *
 * 验证：
 * - 不显示原值（脱敏占位）
 * - 倒计时读 expires_at
 * - 终态不提供按钮
 * - 项目会话显示项目标识
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ConfirmationCard } from './ConfirmationCard'
import type { ConfirmationResponse } from '../../shared/types/confirmations'

const baseArgs = {
  hasActiveRun: true,
  online: true,
  onDeny: vi.fn(),
  onApproveAndExecute: vi.fn(),
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
    // 脱敏占位可见
    expect(screen.getByTestId('arguments-display').textContent).toContain('••••••')
    // 不含原值（应仅包含脱敏占位）
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

  it('从 arguments_display 推断 target（path/url/command）', () => {
    render(
      <ConfirmationCard
        confirmation={makePending({ arguments_display: { path: '/tmp/a.txt' } })}
        {...baseArgs}
      />,
    )
    expect(screen.getByText('/tmp/a.txt')).toBeInTheDocument()
  })

  it('PENDING 状态显示批准/拒绝按钮', () => {
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

  it('终态（executed）不提供按钮', () => {
    render(
      <ConfirmationCard
        confirmation={makePending({ state: 'executed', execution_result: 'ok' })}
        {...baseArgs}
      />,
    )
    expect(screen.queryByTestId('approve-button')).not.toBeInTheDocument()
    expect(screen.queryByTestId('deny-button')).not.toBeInTheDocument()
    expect(screen.getByText('ok')).toBeInTheDocument()
  })

  it('offline 时按钮不显示', () => {
    render(
      <ConfirmationCard confirmation={makePending()} {...baseArgs} online={false} />,
    )
    // offline 不渲染按钮
    expect(screen.queryByTestId('approve-button')).not.toBeInTheDocument()
    expect(screen.queryByTestId('deny-button')).not.toBeInTheDocument()
  })

  it('无活跃 run 时不显示按钮', () => {
    render(
      <ConfirmationCard
        confirmation={makePending()}
        {...baseArgs}
        hasActiveRun={false}
      />,
    )
    expect(screen.queryByTestId('approve-button')).not.toBeInTheDocument()
    expect(screen.queryByTestId('deny-button')).not.toBeInTheDocument()
  })

  it('过期时按钮 disabled 并显示已过期', () => {
    const past = Date.now() / 1000 - 1
    render(
      <ConfirmationCard
        confirmation={makePending({ expires_at: past })}
        {...baseArgs}
      />,
    )
    expect(screen.getByTestId('approve-button')).toBeDisabled()
    expect(screen.getByText('已过期')).toBeInTheDocument()
  })
})
