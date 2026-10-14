/**
 * 前端本地消息状态类型（不参与后端契约）
 *
 * 与 MessageResponse 的区别：
 * - MessageResponse：后端 HTTP 契约，role + content，不可变
 * - OptimisticMessage：前端本地，用户消息发送前/发送中的临时状态
 *
 * 状态分离原则：
 * - 消息提交状态（submitStatus）独立于任务状态（StreamStatus）
 * - SSE error 不等于消息提交失败；只代表连接中断、提交状态不明
 */
import type { MessageResponse } from '../../shared/types/threads'

/** 用户消息的提交状态 */
export type UserSubmitStatus =
  /** 尚未发送 */
  | 'idle'
  /** 发送中，SSE 刚建立但尚未收到 streaming 事件 */
  | 'pending'
  /** 服务端已确认收到（streaming / completed 事件到达） */
  | 'submitted'
  /** 连接中断，但消息是否被服务端接收不明 */
  | 'unknown'
  /** 明确提交失败（HTTP 层确认拒绝，如 422/401） */
  | 'failed'

/** 前端乐观渲染的用户消息 */
export interface OptimisticMessage {
  /** 前端生成，格式 'local-{nanoid}'，用于去重 */
  clientId: string
  /** 消息正文，与服务端消息 content 一致 */
  content: string
  /** 提交状态 */
  status: UserSubmitStatus
  /** 创建时间（毫秒），用于排序 */
  createdAt: number
}

/** 生成客户端消息 ID（不依赖 nanoid 库，避免新增依赖） */
export function generateClientId(): string {
  return `local-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
}

/**
 * MessageResponse 的前端扩展字段（不参与后端 JSON 契约）。
 * 在渲染层将 OptimisticMessage 混入 MessageResponse 时使用。
 */
export interface MessageResponseLocal extends MessageResponse {
  /** 前端生成，格式 'local-{id}'，用于乐观消息去重 */
  clientId?: string
  /** 乐观消息提交状态（仅客户端使用） */
  _submitStatus?: UserSubmitStatus
}
