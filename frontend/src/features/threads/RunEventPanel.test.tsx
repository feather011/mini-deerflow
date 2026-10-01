/**
 * RunEventPanel 组件测试（20D）
 *
 * 覆盖用户可见行为：
 * - 事件顺序展示
 * - 唯一终态
 * - 运行中状态
 * - 失败状态
 * - 停止接收按钮
 * - 终态后不再显示后续事件
 * - 错误提示 + Request ID
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { userEvent } from '@testing-library/user-event'
import { RunEventPanel } from './RunEventPanel'
import type { RunEvent } from '../../shared/types/events'

function ev(opts: Partial<RunEvent> = {}): RunEvent {
  return {
    run_id: 'run-1',
    thread_id: 't1',
    event_type: 'run.started',
    sequence: 0,
    timestamp: 1000,
    answer: null,
    error: null,
    attempt: null,
    operation: null,
    tool_name: null,
    duration_seconds: null,
    ...opts,
  }
}

describe('RunEventPanel', () => {
  it('初始 idle 状态显示"准备连接…"', () => {
    render(
      <RunEventPanel
        status="idle"
        events={[]}
        runId={null}
        errorMessage={null}
        requestId={null}
        terminalEvent={null}
        onStop={vi.fn()}
      />,
    )
    expect(screen.getByText('准备连接…')).toBeInTheDocument()
  })

  it('运行中状态显示"运行中"且显示停止接收按钮', () => {
    render(
      <RunEventPanel
        status="streaming"
        events={[]}
        runId="run-1"
        errorMessage={null}
        requestId={null}
        terminalEvent={null}
        onStop={vi.fn()}
      />,
    )
    expect(screen.getByText('运行中')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '停止接收' })).toBeInTheDocument()
  })

  it('连接中状态也显示停止接收按钮', () => {
    render(
      <RunEventPanel
        status="connecting"
        events={[]}
        runId={null}
        errorMessage={null}
        requestId={null}
        terminalEvent={null}
        onStop={vi.fn()}
      />,
    )
    expect(screen.getByRole('button', { name: '停止接收' })).toBeInTheDocument()
  })

  it('事件按入参顺序展示', () => {
    const events: RunEvent[] = [
      ev({ sequence: 0, event_type: 'run.started' }),
      ev({ sequence: 1, event_type: 'model.started', attempt: 1 }),
      ev({ sequence: 2, event_type: 'tool.started', tool_name: 'bash' }),
      ev({ sequence: 3, event_type: 'tool.completed', tool_name: 'bash' }),
      ev({ sequence: 4, event_type: 'model.completed', attempt: 1, duration_seconds: 1.2 }),
    ]

    render(
      <RunEventPanel
        status="streaming"
        events={events}
        runId="run-1"
        errorMessage={null}
        requestId={null}
        terminalEvent={null}
        onStop={vi.fn()}
      />,
    )

    const labels = screen.getAllByText(/^(Run 开始|模型开始|工具开始|工具完成|模型完成)$/)
    expect(labels.map((n) => n.textContent)).toEqual([
      'Run 开始',
      '模型开始',
      '工具开始',
      '工具完成',
      '模型完成',
    ])
  })

  it('唯一终态：只显示一个 run.completed 标签', () => {
    const events: RunEvent[] = [
      ev({ sequence: 0, event_type: 'run.started' }),
      ev({ sequence: 1, event_type: 'model.started' }),
      ev({ sequence: 2, event_type: 'run.completed', answer: '最终回答', duration_seconds: 5 }),
    ]

    render(
      <RunEventPanel
        status="completed"
        events={events}
        runId="run-1"
        errorMessage={null}
        requestId={null}
        terminalEvent={events[2]!}
        onStop={vi.fn()}
      />,
    )

    expect(screen.getAllByText('Run 完成')).toHaveLength(1)
    expect(screen.getAllByText('最终回答')).toHaveLength(2)
  })

  it('run.failed 状态显示"执行失败"且展示错误信息', () => {
    render(
      <RunEventPanel
        status="failed"
        events={[ev({ event_type: 'run.failed', error: 'something went wrong' })]}
        runId="run-1"
        errorMessage="Agent 执行失败"
        requestId={null}
        terminalEvent={ev({ event_type: 'run.failed', error: 'something went wrong' })}
        onStop={vi.fn()}
      />,
    )

    expect(screen.getByText('执行失败')).toBeInTheDocument()
    expect(screen.getByText(/错误: something went wrong/)).toBeInTheDocument()
    expect(screen.getByText('Agent 执行失败')).toBeInTheDocument()
  })

  it('stopped 状态不显示停止接收按钮', () => {
    render(
      <RunEventPanel
        status="stopped"
        events={[ev({ sequence: 0, event_type: 'run.started' })]}
        runId="run-1"
        errorMessage={null}
        requestId={null}
        terminalEvent={null}
        onStop={vi.fn()}
      />,
    )

    expect(screen.getByText('已停止')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '停止接收' })).not.toBeInTheDocument()
  })

  it('点击停止接收按钮触发 onStop', async () => {
    const user = userEvent.setup()
    const onStop = vi.fn()

    render(
      <RunEventPanel
        status="streaming"
        events={[]}
        runId="run-1"
        errorMessage={null}
        requestId={null}
        terminalEvent={null}
        onStop={onStop}
      />,
    )

    await user.click(screen.getByRole('button', { name: '停止接收' }))
    expect(onStop).toHaveBeenCalledTimes(1)
  })

  it('停止接收按钮的 tooltip 明确说明不会取消后端 Agent', () => {
    render(
      <RunEventPanel
        status="streaming"
        events={[]}
        runId="run-1"
        errorMessage={null}
        requestId={null}
        terminalEvent={null}
        onStop={vi.fn()}
      />,
    )

    const btn = screen.getByRole('button', { name: '停止接收' })
    expect(btn.getAttribute('title')).toContain('不会取消后端 Agent')
  })

  it('终态后不再展示运行中按钮', () => {
    render(
      <RunEventPanel
        status="completed"
        events={[]}
        runId="run-1"
        errorMessage={null}
        requestId={null}
        terminalEvent={ev({ event_type: 'run.completed', answer: 'OK', duration_seconds: 1 })}
        onStop={vi.fn()}
      />,
    )

    expect(screen.getByText('执行完成')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '停止接收' })).not.toBeInTheDocument()
  })

  it('错误信息 + Request ID 同时显示', () => {
    render(
      <RunEventPanel
        status="error"
        events={[]}
        runId={null}
        errorMessage="无法连接后端服务"
        requestId="req-debug-xyz"
        terminalEvent={null}
        onStop={vi.fn()}
      />,
    )

    expect(screen.getByText('无法连接后端服务')).toBeInTheDocument()
    expect(screen.getByText(/请求编号：req-debug-xyz/)).toBeInTheDocument()
  })

  it('run.completed 终态展示 answerBar', () => {
    render(
      <RunEventPanel
        status="completed"
        events={[]}
        runId="run-1"
        errorMessage={null}
        requestId={null}
        terminalEvent={ev({ event_type: 'run.completed', answer: '这是 Agent 的最终回答' })}
        onStop={vi.fn()}
      />,
    )

    expect(screen.getByText('最终回答')).toBeInTheDocument()
    expect(screen.getByText('这是 Agent 的最终回答')).toBeInTheDocument()
  })

  it('后端省略可选字段（undefined）时不崩溃', () => {
    // 真实 SSE 不会带 attempt / duration_seconds / error / tool_name，
    // 类型标的是 | null 但运行时是 undefined，曾导致 .toFixed 崩溃。
    const event = {
      ...ev({ sequence: 1, event_type: 'model.started' }),
      attempt: undefined as unknown as number | null,
      duration_seconds: undefined as unknown as number | null,
      error: undefined as unknown as string | null,
      tool_name: undefined as unknown as string | null,
      operation: undefined as unknown as string | null,
    }

    render(
      <RunEventPanel
        status="streaming"
        events={[event]}
        runId="run-1"
        errorMessage={null}
        requestId={null}
        terminalEvent={null}
        onStop={vi.fn()}
      />,
    )

    expect(screen.getByText('模型开始')).toBeInTheDocument()
    // 不应渲染出 "尝试: undefined" / "耗时: undefineds"
    expect(screen.queryByText(/undefined/)).not.toBeInTheDocument()
  })
})
