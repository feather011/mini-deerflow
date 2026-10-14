/** 徽标语义色（沿用原型 success/warning/danger + 中性底） */
export type PillTone = 'success' | 'warning' | 'danger' | 'neutral'

/** 任务状态 → 文案与语义色 */
export const TASK_STATUS_META = {
  running: { label: '运行中', tone: 'warning' as const },
  completed: { label: '已完成', tone: 'success' as const },
  failed: { label: '失败', tone: 'danger' as const },
  stopped: { label: '已停止', tone: 'warning' as const },
  timeout: { label: '超时', tone: 'danger' as const },
  needs_input: { label: '需要输入', tone: 'warning' as const },
} as const

/** 验证状态 → 文案与语义色 */
export const VALIDATION_STATUS_META = {
  not_run: { label: '未运行', tone: 'neutral' as const },
  passed: { label: '通过', tone: 'success' as const },
  failed: { label: '失败', tone: 'danger' as const },
  running: { label: '运行中', tone: 'warning' as const },
  interrupted: { label: '中断', tone: 'warning' as const },
} as const
