/**
 * MessageList — 消息列表组件
 *
 * 职责：
 * - 按顺序展示服务端消息（MessageDetail.messages）
 * - 乐观渲染用户消息（optimisticMsg），在服务端历史回来后去重合并
 * - assistant 消息按 Markdown 渲染（MarkdownMessage），user/tool/system 按原文展示
 * - 渲染全程不使用 dangerouslySetInnerHTML（原始 HTML 不会被执行）
 * - role 视觉区分（user / assistant）
 * - 提交状态通过 data-submit-status 属性透传给 CSS（不污染 content）
 * - 滚动到底部
 */
import { useEffect, useRef } from 'react'
import type { MessageResponse } from '../../shared/types/threads'
import type { OptimisticMessage, MessageResponseLocal } from './types'
import { MarkdownMessage } from './MarkdownMessage'
import styles from './MessageList.module.css'

interface MessageListProps {
  messages: MessageResponse[]
  /** 当前乐观消息（发送中但服务端尚未返回的用户消息） */
  optimisticMessage?: OptimisticMessage | null
  /** 当前 run 累加的 token 文本（用于流式渲染 assistant 消息） */
  streamingText?: string
}

/**
 * 判断服务端返回的 user 消息是否与本地乐观消息是同一条。
 * 匹配条件：同 role=user，且 content 完全一致。
 * 注意：服务端不返回 clientId，因此用 content 作为去重键。
 * 若用户连发两条内容相同的消息，clientId 不同的两条会合并为一条——这是当前产品的可接受行为。
 */
function isDuplicateOfOptimistic(
  serverMsg: MessageResponse,
  optimistic: OptimisticMessage,
): boolean {
  return serverMsg.role === 'user' && serverMsg.content === optimistic.content
}

/** 单条消息渲染 */
function MessageItem({
  message,
  submitStatus,
}: {
  message: MessageResponse | MessageResponseLocal
  submitStatus?: string
}) {
  const isUser = message.role === 'user'
  const isAssistant = message.role === 'assistant'

  return (
    <div
      className={`${styles.message} ${isUser ? styles.user : ''} ${isAssistant ? styles.assistant : ''}`}
      data-role={message.role}
      data-submit-status={submitStatus}
    >
      <div className={styles.roleLabel}>
        {isUser ? '你' : isAssistant ? 'Agent' : message.role}
      </div>
      <div className={styles.content}>
        {/* assistant 走 Markdown 渲染（流式/最终/历史共用同一组件）；
            其余角色按原文展示，不解析 Markdown 标记 */}
        {isAssistant ? (
          <MarkdownMessage content={message.content} />
        ) : (
          message.content.split('\n').map((line, i) => (
            <span key={i}>
              {line}
              {i < message.content.split('\n').length - 1 && <br />}
            </span>
          ))
        )}
      </div>
    </div>
  )
}

export function MessageList({ messages, optimisticMessage, streamingText }: MessageListProps) {
  const bottomRef = useRef<HTMLDivElement>(null)

  // 渲染列表：服务端消息原样保留。
  // 乐观消息只在「后端尚未同步到这条 user 消息」时才追加。
  // 判定方式：后端 messages 中没有 content 完全一致的同 role=user 消息。
  // 这样做的好处：不改变后端消息的相对顺序，避免 assistant 被挤到 user 前面。
  const showOptimistic = (() => {
    if (!optimisticMessage) return false
    // 查后端是否已有这条 user 消息
    return !messages.some((m) => isDuplicateOfOptimistic(m, optimisticMessage))
  })()

  // 乐观消息的 OptimisticMessage.status → MessageResponseLocal._submitStatus
  const optimisticItem = showOptimistic
    ? { ...optimisticMessage, role: 'user' as const, _submitStatus: optimisticMessage!.status } as MessageResponseLocal
    : null

  // 25C: 流式 assistant 消息（有 token 才显示）
  const streamingItem = streamingText
    ? { content: streamingText, role: 'assistant' as const, message_id: 'streaming', created_at: new Date().toISOString() } as MessageResponse
    : null

  // 合并后的渲染列表：服务端消息原样保留 + 乐观消息（仅后端未同步时追加）+ 流式 assistant（最后）
  const renderItems: (MessageResponse | MessageResponseLocal)[] = [
    ...messages,
    ...(optimisticItem ? [optimisticItem] : []),
    ...(streamingItem ? [streamingItem] : []),
  ]

  // 新消息到达、乐观消息状态变化、或流式文本变化时滚动到底部
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [renderItems.length, optimisticMessage?.status, streamingText])

  if (renderItems.length === 0) {
    return (
      <div className={styles.empty}>
        <p>还没有消息，发送第一条消息开始对话</p>
      </div>
    )
  }

  return (
    <div className={styles.list}>
      {renderItems.map((msg, index) => (
        <MessageItem
          key={'clientId' in msg && msg.clientId ? msg.clientId : index}
          message={msg}
          submitStatus={'_submitStatus' in msg ? (msg as MessageResponseLocal)._submitStatus : undefined}
        />
      ))}
      <div ref={bottomRef} />
    </div>
  )
}
