/**
 * runProgress — 从真实 SSE 事件推导「当前正在执行什么」。
 *
 * 用途：主聊天区只显示**一行**轻量状态（现在在做什么），让用户不必打开
 * 「查看任务 → 过程」页签就能知道任务在推进。完整时间线仍由 TaskInspector
 * 的「过程」页签负责 —— 这里不重复渲染事件明细。
 *
 * 原则：只依据**真实收到的事件**推导，不猜测、不美化：
 * 没有事件就返回 null（不显示任何"正在执行"字样）。
 */
import type { RunEvent } from '../../shared/types/events'

/** 当前阶段 */
export type RunPhase =
  | 'starting'   // Run 已建立
  | 'thinking'   // 模型推理
  | 'generating' // 模型正在输出
  | 'tool'       // 工具执行中
  | 'result'     // 执行结果已到达，正在汇总
  | 'done'       // 终态

export interface RunProgress {
  /** 一句话说明当前正在做什么 */
  text: string
  /** 当前阶段 */
  phase: RunPhase
}

/**
 * 从后往前找**最后一条**能说明"当前在做什么"的事件。
 *
 * heartbeat 不携带任何阶段信息，直接跳过（否则心跳会把状态压回"连接中"）。
 */
export function describeRunProgress(
  events: readonly RunEvent[] | null | undefined,
): RunProgress | null {
  if (!events || events.length === 0) return null

  for (let i = events.length - 1; i >= 0; i--) {
    const event = events[i]!
    if (event.event_type === 'heartbeat') continue

    switch (event.event_type) {
      case 'run.started':
        return { phase: 'starting', text: '已建立运行，正在准备…' }
      case 'model.started':
        return { phase: 'thinking', text: '模型思考中…' }
      case 'model.token':
        return { phase: 'generating', text: '模型正在组织回答…' }
      case 'model.completed':
        return { phase: 'thinking', text: '模型已给出本轮回答，正在准备下一步…' }
      case 'model.failed':
        return { phase: 'done', text: '模型调用失败' }
      case 'tool.started':
        return {
          phase: 'tool',
          text: `正在执行工具：${event.tool_name ?? '（未知工具）'}`,
        }
      case 'tool.completed':
        return {
          phase: 'tool',
          text: `工具已完成：${event.tool_name ?? '（未知工具）'}，正在汇总结果…`,
        }
      case 'tool.failed':
        return {
          phase: 'tool',
          text: `工具执行失败：${event.tool_name ?? '（未知工具）'}`,
        }
      case 'run.result':
        return { phase: 'result', text: '执行结果已到达，正在汇总…' }
      case 'run.completed':
        return { phase: 'done', text: '执行完成' }
      case 'run.failed':
        return { phase: 'done', text: '执行失败' }
      case 'run.timed_out':
        return { phase: 'done', text: '执行超时' }
      case 'run.cancelled':
        return { phase: 'done', text: '已取消' }
      default:
        continue
    }
  }
  return null
}
