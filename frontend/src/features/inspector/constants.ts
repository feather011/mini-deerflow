/** 徽标语义色（沿用原型 success/warning/danger + 中性底） */
export type PillTone = 'success' | 'warning' | 'danger' | 'neutral'

/** 任务状态 → 文案与语义色 */
export const TASK_STATUS_META = {
  // 还没有任何 Run：中性色，不冒充"运行中"
  no_run: { label: '暂无运行', tone: 'neutral' as const },
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
  unverified: { label: '未验证', tone: 'warning' as const },
} as const

/**
 * 后端 guard 判定 → 文案与语义色。
 *
 * 这三档是**判定**（结论），与 VALIDATION_STATUS_META（证据层面的状态）
 * 不是一回事：界面上给结论时只认这里。
 */
export const GUARD_VERDICT_META = {
  verified: { label: '已关联检查通过', tone: 'success' as const },
  unverified: { label: '未验证', tone: 'warning' as const },
  failed: { label: '验证失败', tone: 'danger' as const },
} as const

/** 记录里没有 guard 字段（旧记录）→ 明确「未判定」，不自己补一个结论 */
export const GUARD_MISSING_META = {
  label: '未判定',
  tone: 'neutral' as const,
} as const

/**
 * 没有进行中的 Run，但**存在历史执行结果**（刷新页面后的常态）。
 *
 * 这种情况下不能显示「暂无运行」——那等于把"有历史结果"说成"从未执行过"。
 * 显示「历史结果」，如实区分"当前没在跑"与"从来没跑过"。
 */
export const TASK_HISTORY_META = {
  label: '历史结果',
  tone: 'neutral' as const,
} as const
