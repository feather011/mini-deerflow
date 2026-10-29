/**
 * RunStatusLine — 主聊天区轻量运行状态行。
 *
 * 覆盖：
 * - 无事件时不渲染任何"正在执行"字样
 * - 工具执行中显示工具名
 * - 是 status 区域（可被读屏软件播报）
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { RunStatusLine } from './RunStatusLine'
import type { RunEvent } from '../../shared/types/events'

function makeEvent(overrides: Partial<RunEvent> = {}): RunEvent {
  return {
    run_id: 'r1',
    thread_id: 't1',
    event_type: 'run.started',
    sequence: 1,
    timestamp: 1736000000,
    answer: null,
    error: null,
    attempt: null,
    operation: null,
    tool_name: null,
    duration_seconds: null,
    ...overrides,
  }
}

describe('RunStatusLine', () => {
  it('没有事件时不渲染', () => {
    const { container } = render(<RunStatusLine events={[]} />)
    expect(container.firstChild).toBeNull()
  })

  it('工具执行中显示工具名', () => {
    render(
      <RunStatusLine
        events={[
          makeEvent({ event_type: 'run.started', sequence: 1 }),
          makeEvent({
            event_type: 'tool.started',
            sequence: 2,
            tool_name: 'project_execute',
          }),
        ]}
      />,
    )
    expect(screen.getByRole('status')).toBeInTheDocument()
    expect(screen.getByText(/project_execute/)).toBeInTheDocument()
  })

  it('带 aria-label，可被读屏软件定位', () => {
    render(<RunStatusLine events={[makeEvent({ event_type: 'run.started' })]} />)
    expect(screen.getByLabelText('当前运行状态')).toBeInTheDocument()
  })
})
