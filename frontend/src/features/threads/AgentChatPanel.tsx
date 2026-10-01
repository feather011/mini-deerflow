/**
 * AgentChatPanel — Agent 聊天面板（20D）
 *
 * 职责：
 * - 组合 MessageList + MessageComposer + RunEventPanel
 * - 集成 SSE 流（useAgentStream）
 * - 显示运行中 / 错误状态
 * - 成功后刷新 Thread 详情缓存
 * - 失败时保留输入，显示错误信息
 *
 * 20C 非流式 Run 保留在 runsApi.ts 作为备用能力。
 */
import { useState } from 'react'
import { useAgentStream } from './useAgentStream'
import type { ThreadDetail } from '../../shared/types/threads'
import type { ApiError } from '../../shared/api/client'
import { MessageList } from './MessageList'
import { MessageComposer } from './MessageComposer'
import { RunEventPanel } from './RunEventPanel'
import { ErrorNotice } from '../../shared/ui/ErrorNotice'
import styles from './AgentChatPanel.module.css'

interface AgentChatPanelProps {
  thread: ThreadDetail
}

export function AgentChatPanel({ thread }: AgentChatPanelProps) {
  const [pendingMessage, setPendingMessage] = useState<string | null>(null)

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
    message: pendingMessage ?? '',
    autoStart: false,
  })

  // 错误提示：把流错误包装成 ApiError 形状交给统一组件展示
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

  // 是否正在运行（pending 状态）
  const isTerminal = streamStatus === 'completed' ||
    streamStatus === 'failed' ||
    streamStatus === 'timed_out' ||
    streamStatus === 'cancelled' ||
    streamStatus === 'stopped'

  const isPending = pendingMessage !== null && !isTerminal && !streamError

  function handleSend(message: string) {
    setPendingMessage(message)
    // 必须把消息一并传给 start：本组件的 message 来源是 pendingMessage，
    // 而 setState 尚未生效，start() 会读到上一轮的空字符串。
    start(message)
  }

  // 流结束后事件面板必须继续保留：否则终态（执行完成/失败）只闪现一帧，
  // 用户与 E2E 都无法观察「唯一终态」「停止接收」等结论。
  // 面板生命周期 = pendingMessage 存在；发送下一条消息时整体替换。
  const displayPendingMessage = pendingMessage

  // 用户主动停止：输入框恢复可用，但事件面板仍保留（status=stopped），
  // 因此需要一条常驻提示说明后端 Agent 仍在运行，避免被误解为「已取消」
  const isStopped = streamStatus === 'stopped'

  return (
    <div className={styles.panel}>
      {/* 消息列表 */}
      <div className={styles.messageArea}>
        <MessageList messages={thread.messages} />
      </div>

      {/* SSE 事件面板（pending 时显示） */}
      {displayPendingMessage && (
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
          onDismiss={() => setPendingMessage(null)}
          ariaLabel="停止接收提示"
        />
      )}

      {/* 错误提示 */}
      {streamError && (
        <ErrorNotice
          error={streamError}
          context="run"
          onDismiss={() => setPendingMessage(null)}
          dismissLabel="关闭"
          ariaLabel="运行错误"
        />
      )}

      {/* 消息输入 */}
      <MessageComposer
        onSend={handleSend}
        isPending={isPending}
      />
    </div>
  )
}
