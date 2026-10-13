/**
 * SSE 状态 Context
 *
 * 职责：
 * - AgentChatPanel 订阅 SSE 后将状态写入 Context（唯一写入方）
 * - TaskInspector「过程」Tab 从 Context 读取相同状态（只读消费方）
 * - 不持有 threadId 依赖：两者始终订阅/消费同一 App 实例下的同一个 SSE 连接
 *
 * 使用场景：
 * - TaskInspector 在 App.tsx 中以 <TaskInspector open onClose /> 挂载时，
 *   它与 AgentChatPanel 处于同一 App 实例树下，Context 值与 AgentChatPanel 完全同步
 * - 切换会话时 ThreadWorkspace 卸载/重新挂载，AgentChatPanel SSE 连接随之重建，
 *   Context 值自动重置，TaskInspector「过程」Tab 随之切换
 */
import { createContext, useContext } from 'react'
import type { RunEvent } from '../shared/types/events'
import type { StreamStatus } from '../features/threads/useAgentStream'

export interface SSEContextValue {
  /** 已接收的 SSE 事件（按 sequence 排序） */
  events: RunEvent[]
  /** 当前流状态 */
  status: StreamStatus | null
}

export const SSEContext = createContext<SSEContextValue>({
  events: [],
  status: null,
})

/** 读取 SSE 上下文（TaskInspector「过程」Tab 使用） */
export function useSSEContext() {
  return useContext(SSEContext)
}
