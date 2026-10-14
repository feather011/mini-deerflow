/**
 * MessageList — 消息列表组件
 *
 * 职责：
 * - 按顺序展示服务端消息（MessageDetail.messages）
 * - 乐观渲染用户消息（optimisticMsg），在服务端历史回来后去重合并
 * - 纯文本安全渲染（不使用 dangerouslySetInnerHTML）
 * - role 视觉区分（user / assistant）
 * - 提交状态通过 data-submit-status 属性透传给 CSS（不污染 content）
 * - 滚动到底部
 */
import { useEffect, useRef } from 'react'
import type { MessageResponse } from '../../shared/types/threads'
import type { OptimisticMessage, MessageResponseLocal } from './types'
import styles from './MessageList.module.css'

interface MessageListProps {
  messages: MessageResponse[]
  /** 当前乐观消息（发送中但服务端尚未返回的用户消息） */
  optimisticMessage?: OptimisticMessage | null
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
        {message.content.split('\n').map((line, i) => (
          <span key={i}>
            {line}
            {i < message.content.split('\n').length - 1 && <br />}
          </span>
        ))}
      </div>
    </div>
  )
}

export function MessageList({ messages, optimisticMessage }: MessageListProps) {
  const bottomRef = useRef<HTMLDivElement>(null)

  // 去重：过滤掉与服务端消息重复的乐观消息。
  // 服务端消息在先（历史），乐观消息在后——若服务端已有同 content 的 user 消息，
  // 说明乐观消息已被服务端接受，此时不重复渲染。
  const deduplicatedMessages = (() => {
    if (!optimisticMessage) return messages
    return messages.filter((m) => !isDuplicateOfOptimistic(m, optimisticMessage))
  })()

  // 乐观消息的 OptimisticMessage.status → MessageResponseLocal._submitStatus
  // 注意：Object.assign 覆盖同名字段；这里用解构把 status → _submitStatus
  const optimisticItem = optimisticMessage
    ? { ...optimisticMessage, role: 'user' as const, _submitStatus: optimisticMessage.status } as MessageResponseLocal
    : null

  // 合并后的渲染列表：服务端消息 + 乐观消息（若有）
  const renderItems: (MessageResponse | MessageResponseLocal)[] = [
    ...deduplicatedMessages,
    ...(optimisticItem ? [optimisticItem] : []),
  ]

  // 新消息到达或乐观消息状态变化时滚动到底部
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [renderItems.length, optimisticMessage?.status])

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
