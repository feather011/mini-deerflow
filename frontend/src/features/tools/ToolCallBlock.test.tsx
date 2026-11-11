/**
 * ToolCallBlock — 工具调用折叠块
 *
 * 契约：默认折叠成一行（不占地方），展开才显示参数摘要与结果。
 */
import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { ToolCallBlock } from './ToolCallBlock'
import type { ToolCallEntry } from './useToolCalls'

const base: ToolCallEntry = {
  key: 'r1#1',
  tool_name: 'calculator',
  args_summary: 'expression=str·3字',
  output: '42',
  error: null,
  status: 'succeeded',
  duration_seconds: 0.25,
  after_message_count: 1,
}

describe('ToolCallBlock', () => {
  it('默认折叠：一行摘要，不显示结果正文', () => {
    render(<ToolCallBlock entry={base} />)

    expect(screen.getByTestId('tool-call-block')).toHaveTextContent('调用 calculator')
    expect(screen.getByTestId('tool-call-block')).toHaveTextContent('完成')
    expect(screen.queryByTestId('tool-call-output')).not.toBeInTheDocument()
  })

  it('点击展开后显示参数摘要与结果', () => {
    render(<ToolCallBlock entry={base} />)

    fireEvent.click(screen.getByRole('button'))
    expect(screen.getByTestId('tool-call-args')).toHaveTextContent('expression=str·3字')
    expect(screen.getByTestId('tool-call-output')).toHaveTextContent('42')
  })

  it('失败的工具展开后显示失败原因', () => {
    render(
      <ToolCallBlock
        entry={{
          ...base,
          status: 'failed',
          output: null,
          error: '工具 calculator 调用失败（TOOL_NOT_ALLOWED）',
        }}
      />,
    )

    fireEvent.click(screen.getByRole('button'))
    expect(screen.getByTestId('tool-call-error')).toHaveTextContent('TOOL_NOT_ALLOWED')
  })
})
