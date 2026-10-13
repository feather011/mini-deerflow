/** 最近一次上下文用量快照（前端 TS 类型）。 */

export interface ContextUsageBreakdown {
  system: number
  current_input: number
  active_tool_cycle: number
  recent_messages: number
  summary: number
  task_notes: number
  memory: number
  skills: number
  tool_schemas: number
}

export interface ContextUsageSnapshot {
  thread_id: string
  run_id: string | null
  budget: number
  total: number
  breakdown: ContextUsageBreakdown
  compacted: boolean
  tokens_before: number | null
  soft_limit: number
  captured_at: number
}

export interface ContextUsageResponse {
  snapshot: ContextUsageSnapshot | null
}

export type ContextUsageError =
  | { kind: 'fetch_failed'; message: string }
  | null

/**
 * 占用率分桶（颜色 + 文字）。
 * `error` 是独立的一档：既没有数据、又拉不到数据时，
 * 必须与浮层一样报「请求失败」——否则圆环写「暂无统计」、
 * 浮层写「请求失败」，同一时刻自相矛盾。
 */
export type UsageTone = 'empty' | 'error' | 'safe' | 'warn' | 'critical'

// 有数据时永远不会是 empty/error——让重载把这个事实表达出来，
// 调用方就不必在分支里二次断言。
export function classifyTone(
  snapshot: ContextUsageSnapshot,
): 'safe' | 'warn' | 'critical'
export function classifyTone(
  snapshot: ContextUsageSnapshot | null,
): Exclude<UsageTone, 'error'>
export function classifyTone(
  snapshot: ContextUsageSnapshot | null,
): Exclude<UsageTone, 'error'> {
  if (!snapshot) return 'empty'
  const pct = snapshot.budget > 0 ? (snapshot.total / snapshot.budget) * 100 : 0
  if (pct >= 80) return 'critical'
  if (pct >= 50) return 'warn'
  return 'safe'
}

/**
 * 圆环最终状态：有数据按占用率分档；没数据但请求失败 → error；都没有 → 空态。
 * 数据优先于错误——上一次成功的快照仍然有效，不能因为这次刷新失败就丢掉。
 */
export function resolveTone(
  snapshot: ContextUsageSnapshot | null,
  error: ContextUsageError,
): UsageTone {
  if (snapshot) return classifyTone(snapshot)
  return error ? 'error' : 'empty'
}

export function toneLabel(tone: UsageTone): string {
  switch (tone) {
    case 'empty':
      return '暂无统计'
    case 'error':
      return '请求失败'
    case 'safe':
      return '充足'
    case 'warn':
      return '接近预算'
    case 'critical':
      return '触发压缩'
  }
}

export function snapshotPct(snapshot: ContextUsageSnapshot): number {
  if (snapshot.budget <= 0) return 0
  return Math.round((snapshot.total / snapshot.budget) * 100)
}

/**
 * 9 分区的中文标签 + 分类色（与后端 ContextTokenBreakdown 同序）。
 *
 * 配色与分类**绑定**：颜色跟着实体走，不随显示/排序变化重排。
 * 色值取自 dataviz 参考调色板的 8 个分类槽（固定顺序，不循环），
 * 已跑 validate_palette.js --mode light --surface #ffffff 验证：
 * CVD 相邻 ΔE 9.1、常规视觉 ΔE 19.6 均达标。
 * 第 9 项「系统提示」恒为 0（仓库无固定 system 段），
 * 不占用分类槽，用中性灰表示「未使用」。
 */
export interface BreakdownLabel {
  key: keyof ContextUsageBreakdown
  label: string
  color: string
}

export const BREAKDOWN_LABELS: ReadonlyArray<BreakdownLabel> = [
  { key: 'summary', label: '摘要', color: '#2a78d6' },
  { key: 'task_notes', label: '任务笔记', color: '#eb6834' },
  { key: 'memory', label: '记忆', color: '#1baf7a' },
  { key: 'skills', label: '技能', color: '#eda100' },
  { key: 'recent_messages', label: '近期对话', color: '#e87ba4' },
  { key: 'current_input', label: '当前输入', color: '#008300' },
  { key: 'active_tool_cycle', label: '进行中工具', color: '#4a3aa7' },
  { key: 'tool_schemas', label: '工具描述', color: '#e34948' },
  { key: 'system', label: '系统提示', color: '#9ca3af' },
]

/**
 * 千分位格式化。
 *
 * 刻意**不用 K/万 缩写**：本项目典型量级是 `854 / 73,856`，
 * 缩写成 `0.9K / 73.9K` 会把 854 的精度抹掉（参考图是 85.4K 量级才用 K）。
 */
export function formatTokens(value: number): string {
  return value.toLocaleString('en-US')
}