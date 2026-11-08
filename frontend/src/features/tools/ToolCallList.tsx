/**
 * ToolCallList — 工具调用折叠块列表
 *
 * 渲染在会话消息流下方（与确认卡、子任务卡同位）。
 * 空的（没有工具调用）时不渲染任何东西。
 */
import { ToolCallBlock } from './ToolCallBlock'
import type { ToolCallEntry } from './useToolCalls'

export interface ToolCallListProps {
  entries: ToolCallEntry[]
}

export function ToolCallList({ entries }: ToolCallListProps) {
  if (entries.length === 0) return null
  return (
    <div data-testid="tool-call-list">
      {entries.map((entry) => (
        <ToolCallBlock key={entry.key} entry={entry} />
      ))}
    </div>
  )
}
