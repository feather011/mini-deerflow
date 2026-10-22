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
// TOOL-05：确认卡（聊天内嵌紧邻对应工具活动）
import { useConfirmations } from '../confirmations/useConfirmations'
import { ConfirmationCardList } from '../confirmations/ConfirmationCardList'
import styles from './AgentChatPanel.module.css'

interface AgentChatPanelProps {
  thread: ThreadDetail
}

export function AgentChatPanel({ thread }: AgentChatPanelProps) {
  // 提交守卫：useRef 保证同一引用跨渲染持久
  const submittingRef = useRef(false)

  // 乐观消息
  const [optimisticMsg, setOptimisticMsg] = useState<OptimisticMessage | null>(null)

  // 已处理的 run.result 事件序列号（防重复 ingest）
  const processedResultSeq = useRef<number>(0)

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
    currentText,
    start,
    stop,
    cancelRun,
    cancelState,
    cancelError,
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

  // 26：从 API 响应恢复最近一次 run.result（页面刷新后仍显示 diff）
  useEffect(() => {
    const persisted = thread.last_run_result
    if (persisted && typeof persisted === 'string' && persisted.trim().startsWith('{')) {
      ingestProjectExecuteResult(thread.thread_id, persisted)
    }
  }, [thread.thread_id, thread.last_run_result])

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
        // submittingRef.current 保持 true，禁止重发；输入框由 draft.current 恢复可写
        setSubmitStatus('unknown')
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

  // 真实取消（真停止）：只在有正在运行的 run 时提供
  const isRunning = streamStatus === 'connecting' || streamStatus === 'streaming'
  const canStop = isRunning && cancelState !== 'cancelling' && cancelState !== 'cancelled'
  const isStopping = cancelState === 'cancelling'
  const isCancelled = streamStatus === 'cancelled' || cancelState === 'cancelled'

  /** 点击"停止"：请求后端取消当前任务（不是仅断流） */
  function handleStop() {
    void cancelRun?.()
  }

  // unknown 状态下：pendingSent 存在即可恢复（恢复只清 pendingSent，不影响新草稿 draft.current）
  const canRestore = submitStatus === 'unknown' && draft.pendingSent !== null

  // TOOL-05：确认卡数据源（轮询；切换 thread 重新启动）
  const {
    byRunId,
    online: confirmOnline,
    activeRunId: confirmActiveRunId,
    inFlight: confirmInFlight,
    deny: confirmDeny,
    approveAndExecute: confirmApproveAndExecute,
    verify: confirmVerify,
  } = useConfirmations(thread.thread_id)

  return (
    <SSEContext.Provider value={{ events, status: streamStatus }}>
      <div className={styles.panel}>
        {/* 消息列表（含乐观消息 + 流式 assistant） */}
        <div className={styles.messageArea}>
          <MessageList
            messages={thread.messages}
            optimisticMessage={optimisticMsg}
            streamingText={currentText}
          />
          {/* TOOL-05：确认卡紧邻消息流底部渲染（按 run_id 分组） */}
          <ConfirmationCardList
            byRunId={byRunId}
            activeRunId={confirmActiveRunId}
            online={confirmOnline}
            inFlight={confirmInFlight}
            onDeny={confirmDeny}
            onApproveAndExecute={confirmApproveAndExecute}
            onVerify={confirmVerify}
          />
        </div>

        {/* SSE 事件面板（已隐藏） */}
        {false && submitStatus !== 'idle' && (
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

        {/* 已取消：后端确认取消（子进程已回收、锁已释放） */}
        {isCancelled && (
          <ErrorNotice
            error={{ status: 0, code: 'RUN_CANCELLED', message: '' }}
            context="generic"
            tone="info"
            title="已取消当前任务"
            description="后端已取消该 Run 并回收执行进程，项目锁已释放。"
            dismissLabel="知道了"
            onDismiss={() => {
              setSubmitStatus('submitted')
              setOptimisticMsg(null)
              clearPendingSent()
            }}
            ariaLabel="已取消提示"
          />
        )}

        {/* 取消失败：如实告知后端任务可能仍在运行（不得谎称已停止） */}
        {cancelState === 'failed' && (
          <ErrorNotice
            error={{
              status: 0,
              code: 'CANCEL_FAILED',
              message: cancelError ?? '取消失败',
            }}
            context="generic"
            title="取消失败"
            description={`${cancelError ?? '取消失败'}。后端任务可能仍在运行，请稍后重试或查看后端日志。`}
            dismissLabel="关闭"
            onDismiss={() => {
              // 不改任何草稿/提交状态：任务状态未知，如实呈现
            }}
            ariaLabel="取消失败提示"
          />
        )}

        {/* 消息输入 */}
        <MessageComposer
          value={draft.current}
          onValueChange={saveCurrent}
          onSend={handleSend}
          isPending={isPending}
          isSubmitting={submittingRef.current}
          hasPendingRestore={canRestore}
          onRestore={handleRestore}
          canStop={canStop}
          isStopping={isStopping}
          onStop={handleStop}
        />

        {/* 上下文用量 */}
        <div className={styles.contextBar}>
          <ContextUsageIndicator snapshot={snapshot} error={ctxError} />
        </div>
      </div>
    </SSEContext.Provider>
  )
}
