/**
 * MessageList — 消息列表组件
 *
 * 职责：
 * - 按顺序展示 ThreadDetail.messages
 * - 纯文本安全渲染（不使用 dangerouslySetInnerHTML）
 * - role 视觉区分（user / assistant）
 * - 滚动到底部
 */
import { useEffect, useRef } from 'react'
import type { MessageResponse } from '../../shared/types/threads'
import styles from './MessageList.module.css'

interface MessageListProps {
  messages: MessageResponse[]
}

function MessageItem({ message }: { message: MessageResponse }) {
  const isUser = message.role === 'user'
  const isAssistant = message.role === 'assistant'

  return (
    <div
      className={`${styles.message} ${isUser ? styles.user : ''} ${isAssistant ? styles.assistant : ''}`}
      data-role={message.role}
    >
      <div className={styles.roleLabel}>
        {isUser ? '你' : isAssistant ? 'Agent' : message.role}
      </div>
      <div className={styles.content}>
        {/* 纯文本展示，换行符转为 <br> */}
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

export function MessageList({ messages }: MessageListProps) {
  const bottomRef = useRef<HTMLDivElement>(null)

  // 新消息到达时滚动到底部
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages.length])

  if (messages.length === 0) {
    return (
      <div className={styles.empty}>
        <p>还没有消息，发送第一条消息开始对话</p>
      </div>
    )
  }

  return (
    <div className={styles.list}>
      {messages.map((msg, index) => (
        <MessageItem key={index} message={msg} />
      ))}
      <div ref={bottomRef} />
    </div>
  )
}
