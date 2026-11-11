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
import { useEffect, useMemo, useRef, useState } from 'react'
import { useAgentStream } from './useAgentStream'
import { useContextUsage, ContextUsageIndicator } from './'
import type { ThreadDetail } from '../../shared/types/threads'
import type { ApiError } from '../../shared/api/client'
import { MessageList } from './MessageList'
import type { MessageBlock } from './MessageList'
import { MessageComposer } from './MessageComposer'
import { RunEventPanel } from './RunEventPanel'
import { RunStatusLine } from './RunStatusLine'
import { ErrorNotice } from '../../shared/ui/ErrorNotice'
import { SSEContext } from '../../app/SSEContext'
import { ingestRealTaskData, ingestProjectExecuteResult } from '../inspector/realTaskAdapter'
import type { OptimisticMessage, UserSubmitStatus } from './types'
import { generateClientId } from './types'
import { useDraftStore } from './useDraftStore'
// TOOL-05：确认卡（聊天内嵌紧邻对应工具活动）
import { useConfirmations } from '../confirmations/useConfirmations'
import { ConfirmationCardList } from '../confirmations/ConfirmationCardList'
// Subagent 首版：子任务通知条（聊天区内嵌，点击在右侧打开子 Agent 会话）
import { useSubagentCards } from '../subagents/useSubagentCards'
import { SubagentCardList } from '../subagents/SubagentCardList'
import type { SubagentCard as SubagentCardModel } from '../subagents/useSubagentCards'
// 工具调用折叠块（会话内，像消息一样折叠展示）
import { useToolCalls } from '../tools/useToolCalls'
import { ToolCallList } from '../tools/ToolCallList'
import styles from './AgentChatPanel.module.css'

interface AgentChatPanelProps {
  thread: ThreadDetail
  /** 点击子任务通知条：在右侧打开该子 Agent 的会话窗口 */
  onOpenSubagent?: (card: SubagentCardModel) => void
}

export function AgentChatPanel({ thread, onOpenSubagent }: AgentChatPanelProps) {
  // 提交守卫：useRef 保证同一引用跨渲染持久
  const submittingRef = useRef(false)

  // 乐观消息
  const [optimisticMsg, setOptimisticMsg] = useState<OptimisticMessage | null>(null)

  // 本轮过程块的时序锚点：过程块插在第几条消息之后。
  //
  // 发送时按当时的会话消息数 + 1 记下 —— 那正是"本轮 user 消息"的位置
  // （后端在本轮开始时的历史长度 + 1，两边口径一致）。工具折叠块与子任务条
  // 据此插到消息流里的**当时位置**，而不是永远挂在会话末尾。
  //
  // 绑定 threadId：切换会话后旧锚点不再适用。
  const [runAnchorState, setRunAnchorState] = useState<{
    threadId: string
    anchor: number
  } | null>(null)
  const runAnchor =
    runAnchorState?.threadId === thread.thread_id ? runAnchorState.anchor : null

  // 已处理的 run.result 事件序列号（防重复 ingest）
  const processedResultSeq = useRef<number>(0)

  // 取消提示的「已关闭」标记，绑定到产生它的那一次运行。
  // 键取 runId：新一轮 run 会拿到新的 runId，上一轮的关闭状态自然失效，
  // 于是新一轮被取消时会重新显示它自己的通知。
  const [dismissedCancelRunId, setDismissedCancelRunId] = useState<string | null>(null)

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
  //
  // 注意：这个 effect 必须排在下面「建立本会话任务」的 effect **之后**。
  // `ingestProjectExecuteResult` 在缓存里没有该 thread 的条目时会直接返回，
  // 冷启动（刷新页面）时若先恢复、后建立，保存的结果就被丢弃了。
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

  useEffect(() => {
    const persisted = thread.last_run_result
    if (persisted && typeof persisted === 'string' && persisted.trim().startsWith('{')) {
      ingestProjectExecuteResult(thread.thread_id, persisted)
    }
  }, [thread.thread_id, thread.last_run_result])

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

    // 每轮 run 的 sequence 都从 0/1 重新计数，处理游标必须一起归零。
    // 否则上一轮已经处理到 sequence=30，本轮的 run.result（sequence=1）
    // 会因为「不大于游标」被整段跳过，文件和 diff 都不更新。
    processedResultSeq.current = 0

    // 记下本轮过程块的锚点（第几条消息之后）= 当前消息数 + 1
    setRunAnchorState({
      threadId: thread.thread_id,
      anchor: thread.messages.length + 1,
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

  // 取消提示是否要显示：只有「用户还没关掉这一轮的提示」时才显示。
  // 关闭只改这一行的可见性，不改 streamStatus / cancelState，
  // 因此后端的取消事实、任务历史都不受影响，也不会触发重试。
  const cancelNoticeKey = runId ?? 'unknown-run'
  const showCancelNotice = isCancelled && dismissedCancelRunId !== cancelNoticeKey

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

  // Subagent 首版：子任务卡数据源。
  //
  // 两个来源合并：本轮 SSE 事件（实时进度）+ 线程详情里的持久化记录
  // （页面刷新后靠它恢复卡片 —— G6 的前端一半）。
  const subagentCards = useSubagentCards(events, thread.subagent_runs, runAnchor)
  // 工具调用折叠块：从 SSE 的 tool.* 事件聚合。
  const toolCalls = useToolCalls(events, runAnchor)

  // 过程块按锚点分组后交给 MessageList 交错渲染。
  //
  // 同一锚点下：工具折叠块在前（父 Agent 视角的调用），子任务条在后
  // （子 Agent 视角的结果）。锚点未知（null）的一律排到末尾。
  const blocks = useMemo<MessageBlock[]>(() => {
    const groups = new Map<
      number,
      { tools: typeof toolCalls; cards: SubagentCardModel[] }
    >()
    const groupOf = (anchor: number | null) => {
      const key = anchor ?? Number.MAX_SAFE_INTEGER
      let group = groups.get(key)
      if (!group) {
        group = { tools: [], cards: [] }
        groups.set(key, group)
      }
      return group
    }
    for (const entry of toolCalls) {
      groupOf(entry.after_message_count).tools.push(entry)
    }
    for (const card of subagentCards) {
      groupOf(card.after_message_count).cards.push(card)
    }

    const result: MessageBlock[] = []
    for (const anchor of [...groups.keys()].sort((a, b) => a - b)) {
      const group = groups.get(anchor)!
      if (group.tools.length > 0) {
        result.push({
          key: `tool-calls@${anchor}`,
          afterMessageCount: anchor,
          node: <ToolCallList entries={group.tools} />,
        })
      }
      if (group.cards.length > 0) {
        result.push({
          key: `subagent-cards@${anchor}`,
          afterMessageCount: anchor,
          node: <SubagentCardList cards={group.cards} onOpen={onOpenSubagent} />,
        })
      }
    }
    return result
  }, [toolCalls, subagentCards, onOpenSubagent])

  return (
    <SSEContext.Provider value={{ events, status: streamStatus }}>
      <div className={styles.panel}>
        {/* 消息列表（含乐观消息 + 流式 assistant）
            工具调用折叠块、子任务通知条作为 blocks 传进去 —— 它们和消息
            处在同一个滚动容器里，并按锚点插到消息流里的当时位置：
            会被后来的消息顶上去，而不是永远悬在聊天区底部。 */}
        <div className={styles.messageArea}>
          <MessageList
            messages={thread.messages}
            optimisticMessage={optimisticMsg}
            streamingText={currentText}
            blocks={blocks}
          />
          {/* TOOL-05：确认卡紧邻消息流底部渲染（按 run_id 分组）
              确认卡是「等用户拍板」的待办，固定在底部保证不会滚出视线。 */}
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
        {showCancelNotice && (
          <ErrorNotice
            error={{ status: 0, code: 'RUN_CANCELLED', message: '' }}
            context="generic"
            tone="info"
            title="已取消当前任务"
            description="后端已取消该 Run 并回收执行进程，项目锁已释放。"
            dismissLabel="知道了"
            onDismiss={() => {
              // 只隐藏本轮提示：不动 streamStatus / cancelState，
              // 不重试、不取消其它任务；新一轮被取消会重新显示。
              setDismissedCancelRunId(cancelNoticeKey)
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

        {/* 轻量运行状态：依据真实 SSE 事件显示"当前在做什么"（明细在「查看任务 → 过程」） */}
        {isRunning && <RunStatusLine events={events} status={streamStatus} />}

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
