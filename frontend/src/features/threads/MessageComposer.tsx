/**
 * MessageComposer — 消息输入组件
 *
 * 职责：
 * - 多行文本输入（textarea）
 * - Enter 发送，Shift+Enter 换行
 * - 空白或超长消息禁止提交
 * - pending 时禁用输入和发送按钮
 * - 成功后清空输入，失败时保留输入
 */
import { useState, useRef } from 'react'
import type { KeyboardEvent, ChangeEvent } from 'react'
import styles from './MessageComposer.module.css'

const MAX_MESSAGE_LENGTH = 8192

interface MessageComposerProps {
  onSend: (message: string) => void
  isPending: boolean
}

export function MessageComposer({ onSend, isPending }: MessageComposerProps) {
  const [value, setValue] = useState('')
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  const trimmedValue = value.trim()
  const isEmpty = trimmedValue.length === 0
  const isTooLong = value.length > MAX_MESSAGE_LENGTH
  const canSend = !isEmpty && !isTooLong && !isPending

  function handleChange(e: ChangeEvent<HTMLTextAreaElement>) {
    setValue(e.target.value)
  }

  function handleSend() {
    if (!canSend) return
    onSend(trimmedValue)
    setValue('')
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    // Enter 发送，Shift+Enter 换行
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    handleSend()
  }

  return (
    <form className={styles.composer} onSubmit={handleSubmit}>
      <div className={styles.inputWrapper}>
        <textarea
          ref={textareaRef}
          className={styles.textarea}
          value={value}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          placeholder={isPending ? 'Agent 正在运行…' : '输入消息，Enter 发送，Shift+Enter 换行'}
          disabled={isPending}
          rows={3}
          aria-label="消息输入"
        />
        <div className={styles.charCount}>
          {value.length > 0 && (
            <span className={isTooLong ? styles.charCountError : ''}>
              {value.length} / {MAX_MESSAGE_LENGTH}
            </span>
          )}
        </div>
      </div>
      <button
        type="submit"
        className={styles.sendBtn}
        disabled={!canSend}
        aria-label="发送消息"
      >
        {isPending ? '运行中…' : '发送'}
      </button>
    </form>
  )
}
