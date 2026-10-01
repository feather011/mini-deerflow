/**
 * Thread 主区域组件
 *
 * 职责：
 * - 未选择 Thread：显示引导文案
 * - 加载中：显示加载状态
 * - 加载成功：显示 Agent 聊天界面（20C）
 * - 404：显示会话不存在，提供返回按钮
 * - 其他错误：显示错误信息和 Request ID
 */
import { useNavigate } from 'react-router-dom'
import { useThread } from './hooks'
import { AgentChatPanel } from './AgentChatPanel'
import { ErrorNotice } from '../../shared/ui/ErrorNotice'
import type { ApiError } from '../../shared/api'
import styles from './ThreadWorkspace.module.css'

interface ThreadWorkspaceProps {
  threadId: string | null
}

function NotSelected() {
  return (
    <div className={styles.center}>
      <span className={styles.bigEmoji} aria-hidden="true">👈</span>
      <p className={styles.title}>选择或创建一个会话</p>
      <p className={styles.hint}>使用左侧栏管理会话</p>
    </div>
  )
}

function Loading() {
  return (
    <div className={styles.center}>
      <span className={styles.spinner} aria-hidden="true" />
      <p className={styles.loadingText}>加载会话…</p>
    </div>
  )
}

function NotFound({ threadId }: { threadId: string }) {
  const navigate = useNavigate()
  return (
    <div className={styles.center}>
      <span className={styles.bigEmoji} aria-hidden="true">❌</span>
      <p className={styles.title}>会话不存在或已被删除</p>
      <p className={styles.threadId}>{threadId}</p>
      <button
        type="button"
        className={styles.backBtn}
        onClick={() => navigate('/')}
      >
        返回会话列表
      </button>
    </div>
  )
}

function ThreadInfo({ thread }: { thread: { thread_id: string; messages: unknown[] } }) {
  return (
    <div className={styles.chat}>
      <AgentChatPanel
        thread={thread as Parameters<typeof AgentChatPanel>[0]['thread']}
      />
    </div>
  )
}

function ThreadError({ error }: { error: ApiError }) {
  const navigate = useNavigate()
  return (
    <div className={styles.center}>
      <span className={styles.bigEmoji} aria-hidden="true">⚠️</span>
      <p className={styles.title}>加载失败</p>
      <ErrorNotice
        error={error}
        context="thread"
        ariaLabel="会话加载错误"
      />
      <div className={styles.errorActions}>
        <button type="button" className={styles.backBtn} onClick={() => navigate('/')}>
          返回会话列表
        </button>
      </div>
    </div>
  )
}

export function ThreadWorkspace({ threadId }: ThreadWorkspaceProps) {
  // Hook 必须在条件语句外调用（React Hooks 规则）
  const { data, isLoading, isError, error } = useThread(threadId)

  if (threadId === null) {
    return <NotSelected />
  }

  if (isLoading) {
    return <Loading />
  }

  // 404 → 会话不存在
  if (isError && error?.status === 404) {
    return <NotFound threadId={threadId} />
  }

  // 其他错误
  if (isError && error != null) {
    return <ThreadError error={error} />
  }

  if (data != null) {
    return <ThreadInfo thread={data} />
  }

  return <NotSelected />
}
