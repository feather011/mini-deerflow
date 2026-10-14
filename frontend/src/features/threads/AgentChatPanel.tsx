/**
 * AgentChatPanel — Agent 聊天面板（UX-1 状态分离 + 草稿保护版）
 *
 * 职责：
 * - 组合 MessageList + MessageComposer + RunEventPanel
 * - 集成 SSE 流（useAgentStream）
 * - 乐观渲染用户消息（提交状态独立于任务状态）
 * - SSE 断开 → submitStatus='unknown'（不标 failed，不引导重复执行）
 * - 草稿保护：sessionStorage 按 threadId 分键，发送失败后按规则恢复
 * - 成功后刷新 Thread 详情缓存
 *
 * 20C 非流式 Run 保留在 runsApi.ts 作为备用能力。
 *
 * 状态分离原则（UX-1 强制）：
 * - 消息提交状态：'idle' | 'pending' | 'submitted' | 'unknown' | 'failed'
 * - 任务状态（StreamStatus）：'idle' | 'connecting' | 'streaming' | 'completed' | 'failed' | ...
 * - SSE 连接状态：与任务状态共用 streamStatus，但提交状态独立维护
 *
 * 草稿生命周期：
 * - 发送前：savePendingSent → 保存快照，编辑区继续接受新草稿
 * - 发送成功/历史回来：clearPendingSent
 * - unknown + 无新草稿：draft.pendingSent 存在 → 显示"恢复原消息"按钮
 * - unknown + 有新草稿：保留新草稿，pendingSent 不动
 * - 恢复操作：restorePendingSent → pendingSent → textarea
 */
import { useEffect, useRef, useState } from 'react'
import { useAgentStream } from './useAgentStream'
import { useContextUsage, ContextUsageIndicator } from './'
import type { ThreadDetail } from '../../shared/types/threads'
import type { ApiError } from '../../shared/api/client'
import { MessageList } from './MessageList'
import { MessageComposer } from './MessageComposer'
import { RunEventPanel } from './RunEventPanel'
import { ErrorNotice } from '../../shared/ui/ErrorNotice'
import { SSEContext } from '../../app/SSEContext'
import { ingestRealTaskData, ingestProjectExecuteResult } from '../inspector/realTaskAdapter'
import type { OptimisticMessage, UserSubmitStatus } from './types'
import { generateClientId } from './types'
import { useDraftStore } from './useDraftStore'
import styles from './AgentChatPanel.module.css'

interface AgentChatPanelProps {
  thread: ThreadDetail
}

// ---------------------------------------------------------------------------
// 提交状态守卫
// ---------------------------------------------------------------------------
function useSubmitGuard() {
  const submittingRef = { current: false }
  return submittingRef
}

// ---------------------------------------------------------------------------
// 组件
// ---------------------------------------------------------------------------
export function AgentChatPanel({ thread }: AgentChatPanelProps) {
  const submittingRef = useSubmitGuard()

  // 乐观消息
  const [optimisticMsg, setOptimisticMsg] = useState<OptimisticMessage | null>(null)

  // 消息提交状态
  const [submitStatus, setSubmitStatus] = useState<UserSubmitStatus>('idle')

  // 上下文用量快照
  const { snapshot, error: ctxError, refresh } = useContextUsage(thread.thread_id)

  // SSE 流 Hook
  const {
    status: streamStatus,
    events,
    runId,
    errorMessage,
    requestId,
    terminalEvent,
    start,
    stop,
  } = useAgentStream({
    threadId: thread.thread_id,
    message: '',
    autoStart: false,
  })

  // 草稿存储（按 threadId 分键）
  const { draft, saveCurrent, savePendingSent, clearPendingSent, restorePendingSent } =
    useDraftStore(thread.thread_id)

  // 是否终态
  const isTerminal = streamStatus === 'completed' ||
    streamStatus === 'failed' ||
    streamStatus === 'timed_out' ||
    streamStatus === 'cancelled' ||
    streamStatus === 'stopped'

  // pending：提交中（发送按钮/输入框禁用）
  const isPending = submitStatus === 'pending'

  // 流错误包装成 ApiError
  const streamError: ApiError | null =
    streamStatus === 'error' && errorMessage
      ? ({
          name: 'ApiError',
          message: errorMessage,
          status: 0,
          code: 'STREAM_ERROR',
          requestId: requestId ?? undefined,
          details: null,
        } as ApiError)
      : null

  // 流结束后刷新上下文快照
  useEffect(() => {
    if (isTerminal) void refresh()
  }, [isTerminal]) // eslint-disable-line react-hooks/exhaustive-deps

  // 跟踪已处理的 run.result 事件
  const processedResultSeq = useRef<number>(-1)

  useEffect(() => {
    ingestRealTaskData(thread.thread_id, {
      streamStatus,
      events,
      runId,
      title: (() => {
        const firstUser = thread.messages.find((m) => m.role === 'user')
        return (firstUser?.content ?? '').slice(0, 80)
      })() || '（无标题会话）',
    })

    for (const event of events) {
      if (
        event.event_type === 'run.result' &&
        event.tool_result != null &&
        event.sequence > processedResultSeq.current
      ) {
        processedResultSeq.current = event.sequence
        ingestProjectExecuteResult(thread.thread_id, event.tool_result)
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [thread.thread_id, streamStatus, events, runId])

  // 同步 SSE 状态到提交状态
  useEffect(() => {
    switch (streamStatus) {
      case 'connecting':
        // eslint-disable-next-line react(set-state-in-effect)
        setSubmitStatus('pending')
        break
      case 'streaming':
        setSubmitStatus('submitted')
        clearPendingSent()
        break
      case 'error':
        // unknown：消息是否被服务端接收不明
        // 输入框由 draft.current 恢复可写；发送按钮 disabled（submittingRef=true）
        setSubmitStatus('unknown')
        submittingRef.current = false
        break
      case 'completed':
        setSubmitStatus('submitted')
        clearPendingSent()
        submittingRef.current = false
        break
      case 'failed':
      case 'timed_out':
      case 'cancelled':
        // 用户消息已提交，服务端已接收；不清草稿
        setSubmitStatus('submitted')
        clearPendingSent()
        submittingRef.current = false
        break
      case 'stopped':
        setSubmitStatus('submitted')
        clearPendingSent()
        submittingRef.current = false
        break
      // idle / 默认：不操作
    }
  }, [streamStatus]) // eslint-disable-line react-hooks/exhaustive-deps

  // 发送消息
  function handleSend(message: string) {
    if (submittingRef.current) return

    const clientId = generateClientId()
    submittingRef.current = true
    setSubmitStatus('pending')
    setOptimisticMsg({
      clientId,
      content: message,
      status: 'pending',
      createdAt: Date.now(),
    })

    // 保存草稿快照（发送前），编辑区继续接受新草稿
    savePendingSent(message)
    start(message)
  }

  /** 用户主动恢复：pendingSent → textarea，pendingSent 清空，释放 submittingRef 允许再发 */
  function handleRestore() {
    if (!draft.pendingSent) return
    restorePendingSent()
    // 恢复后释放 submittingRef：用户恢复后可继续编辑并发送
    submittingRef.current = false
  }

  const isStopped = streamStatus === 'stopped'

  // unknown 状态下：pendingSent 存在即可恢复（恢复只清 pendingSent，不影响新草稿 draft.current）
  const canRestore = submitStatus === 'unknown' && draft.pendingSent !== null

  return (
    <SSEContext.Provider value={{ events, status: streamStatus }}>
      <div className={styles.panel}>
        {/* 消息列表（含乐观消息） */}
        <div className={styles.messageArea}>
          <MessageList messages={thread.messages} optimisticMessage={optimisticMsg} />
        </div>

        {/* SSE 事件面板 */}
        {submitStatus !== 'idle' && (
          <div className={styles.streamArea}>
            <RunEventPanel
              status={streamStatus === 'idle' ? 'connecting' : streamStatus}
              events={events}
              runId={runId}
              errorMessage={streamStatus === 'error' ? errorMessage : null}
              requestId={requestId}
              terminalEvent={terminalEvent}
              onStop={stop}
            />
          </div>
        )}

        {/* 停止接收后的常驻说明 */}
        {isStopped && (
          <ErrorNotice
            error={{ status: 0, code: 'STREAM_STOPPED', message: '' }}
            context="generic"
            tone="info"
            title="已停止接收事件"
            description="后端 Agent 未被取消，仍在后台运行；如需停止请到后端取消该 Run。"
            dismissLabel="知道了"
            onDismiss={() => {
              setSubmitStatus('submitted')
              setOptimisticMsg(null)
              clearPendingSent()
            }}
            ariaLabel="停止接收提示"
          />
        )}

        {/* 错误提示（unknown 时不自动清除草稿） */}
        {streamError && (
          <ErrorNotice
            error={streamError}
            context="run"
            onDismiss={() => {
              // unknown：用户关闭错误提示，不做任何草稿操作
              // submittingRef 保持 true，直到下一次成功发送
            }}
            dismissLabel="关闭"
            ariaLabel="运行错误"
          />
        )}

        {/* 消息输入 */}
        <MessageComposer
          value={draft.current}
          onValueChange={saveCurrent}
          onSend={handleSend}
          isPending={isPending}
          hasPendingRestore={canRestore}
          onRestore={handleRestore}
        />

        {/* 上下文用量 */}
        <div className={styles.contextBar}>
          <ContextUsageIndicator snapshot={snapshot} error={ctxError} />
        </div>
      </div>
    </SSEContext.Provider>
  )
}
