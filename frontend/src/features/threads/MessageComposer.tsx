/**
 * MessageComposer — 消息输入组件
 *
 * 职责：
 * - 多行文本输入（textarea）
 * - Enter 发送，Shift+Enter 换行
 * - 空白或超长消息禁止提交
 * - pending 时禁用输入和发送按钮
 * - IME 选字期间不发送（composition 守卫 + keyboard.isComposing 检查）
 * - 防重复提交：发送入口有即时 ref 守卫，不依赖 React render 同步的 disabled 属性
 * - 受控模式：value / onChange 由父组件传入；内部 state 仅作 fallback
 * - 草稿恢复：hasPendingRestore=true 时显示"恢复原消息"按钮
 */
import { useState, useRef, useEffect } from 'react'
import type { KeyboardEvent, ChangeEvent, CompositionEvent } from 'react'
import styles from './MessageComposer.module.css'

const MAX_MESSAGE_LENGTH = 8192

interface MessageComposerProps {
  /** 受控模式：父组件传入当前 textarea 值（来自 useDraftStore.draft.current） */
  value?: string
  /** 受控模式：textarea 值变化时通知父组件 */
  onValueChange?: (value: string) => void
  onSend: (message: string) => void
  isPending: boolean
  /** 显示"恢复原消息"按钮（pendingSent 存在且无新草稿时） */
  hasPendingRestore?: boolean
  /** 用户点击"恢复原消息" */
  onRestore?: () => void
}

export function MessageComposer({
  value: controlledValue,
  onValueChange,
  onSend,
  isPending,
  hasPendingRestore,
  onRestore,
}: MessageComposerProps) {
  // 受控模式：父组件传入 value 时使用外部值，否则内部管理
  const [internalValue, setInternalValue] = useState('')
  const value = controlledValue !== undefined ? controlledValue : internalValue
  const setValue = onValueChange ?? setInternalValue

  // IME 选字状态
  const [isComposing, setIsComposing] = useState(false)
  const composingRef = useRef(false)
  // 防重复：发送入口即时守卫
  const submittingRef = useRef(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  const trimmedValue = value.trim()
  const isEmpty = trimmedValue.length === 0
  const isTooLong = value.length > MAX_MESSAGE_LENGTH
  const canSend = !isEmpty && !isTooLong && !isPending && !isComposing

  // 父组件已接收（isPending 上升）→ 释放 submittingRef
  useEffect(() => {
    if (isPending) submittingRef.current = false
  }, [isPending])

  function handleChange(e: ChangeEvent<HTMLTextAreaElement>) {
    setValue(e.target.value)
  }

  function handleCompositionStart(_e: CompositionEvent<HTMLTextAreaElement>) {
    composingRef.current = true
    setIsComposing(true)
  }

  function handleCompositionEnd(_e: CompositionEvent<HTMLTextAreaElement>) {
    composingRef.current = false
    setIsComposing(false)
  }

  function handleSend() {
    if (submittingRef.current) return
    if (composingRef.current) return
    if (!canSend) return
    submittingRef.current = true
    onSend(trimmedValue)
    // 受控模式：父组件通过 onValueChange 同步清空；fallback 时清内部 state
    if (!onValueChange) setValue('')
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      if (e.nativeEvent.isComposing || composingRef.current) return
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
          onCompositionStart={handleCompositionStart}
          onCompositionEnd={handleCompositionEnd}
          placeholder={
            isPending
              ? 'Agent 正在运行…'
              : isComposing
                ? '正在选字…'
                : '输入消息，Enter 发送，Shift+Enter 换行'
          }
          disabled={isPending}
          rows={3}
          aria-label="消息输入"
          aria-busy={isPending}
        />
        <div className={styles.charCount}>
          {hasPendingRestore && onRestore && (
            <button
              type="button"
              className={styles.restoreBtn}
              onClick={onRestore}
              title="恢复上一条发送的消息"
            >
              恢复原消息
            </button>
          )}
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
