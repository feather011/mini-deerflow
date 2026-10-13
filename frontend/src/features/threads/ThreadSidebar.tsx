/**
 * ThreadSidebar — 会话列表侧边栏
 *
 * 职责：
 * - 显示 Thread 列表（loading / empty / error / success）
 * - 按 project_id 分组：普通会话 / 项目会话（旧数据无该字段按普通会话）
 * - 搜索会话（按标题 / 最后一条消息过滤，无匹配显示空态）
 * - 创建新 Thread（POST → 导航到新 Thread URL）
 * - 删除 Thread（内联二次确认）
 * - 选择 Thread（导航到 /threads/:threadId）
 * - 当前 Thread 视觉标记
 * - 底部「成果库」「设置」入口（仅在传入回调时渲染）
 * - 错误时显示 Request ID
 *
 * 状态管理原则：
 * - URL 是当前 Thread 选择状态的唯一事实来源
 * - 列表数据由 TanStack Query 管理，组件只消费
 * - 创建/删除成功后再导航，失败不导航
 */
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useThreads, useCreateThread, useDeleteThread } from './hooks'
import { ErrorNotice } from '../../shared/ui/ErrorNotice'
import type { ThreadDetail } from '../../shared/types/threads'
import type { ApiError } from '../../shared/api/client'
import styles from './ThreadSidebar.module.css'

// ---------------------------------------------------------------------------
// 内部子组件
// ---------------------------------------------------------------------------

function LoadingState() {
  return (
    <div className={styles.loading}>
      <span className={styles.spinner} aria-hidden="true" />
      加载中…
    </div>
  )
}

function EmptyState() {
  return (
    <div className={styles.empty}>
      <span className={styles.emptyIcon} aria-hidden="true">💬</span>
      <p className={styles.emptyTitle}>还没有会话</p>
      <p className={styles.emptyBody}>创建一个会话开始使用 Mini DeerFlow</p>
    </div>
  )
}

function NoMatchState() {
  return (
    <div className={styles.empty}>
      <span className={styles.emptyIcon} aria-hidden="true">🔍</span>
      <p className={styles.emptyTitle}>没有匹配的会话</p>
    </div>
  )
}

/**
 * 读取会话归属项目的 ID。
 * 后端 ThreadDetail 契约尚未声明 project_id；旧数据无该字段时按普通会话处理。
 */
function getProjectId(thread: ThreadDetail): string | null {
  const value = (thread as ThreadDetail & { project_id?: unknown }).project_id
  return typeof value === 'string' && value.trim() !== '' ? value : null
}

/** 搜索过滤：按标题（thread_id）或最后一条消息内容匹配（忽略大小写）。 */
function matchesQuery(thread: ThreadDetail, query: string): boolean {
  if (query === '') return true
  const lastMessage = thread.messages[thread.messages.length - 1]?.content ?? ''
  return (
    thread.thread_id.toLowerCase().includes(query) ||
    lastMessage.toLowerCase().includes(query)
  )
}

function ListErrorState({ error }: { error: ApiError }) {
  return (
    <div className={styles.error}>
      <ErrorNotice
        error={error}
        context="thread"
        ariaLabel="会话列表错误"
      />
    </div>
  )
}

interface ThreadItemProps {
  thread: ThreadDetail
  isActive: boolean
  isDeleting: boolean
  isConfirmingDelete: boolean
  onSelect: () => void
  onDeleteClick: () => void
  onConfirmDelete: () => void
  onCancelDelete: () => void
}

function ThreadItem({
  thread,
  isActive,
  isDeleting,
  isConfirmingDelete,
  onSelect,
  onDeleteClick,
  onConfirmDelete,
  onCancelDelete,
}: ThreadItemProps) {
  return (
    <div
      className={`${styles.listItem} ${isActive ? styles.listItemActive : ''}`}
      role="button"
      tabIndex={0}
      aria-label={`会话 ${thread.thread_id}${isActive ? '（当前）' : ''}`}
      onClick={onSelect}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') onSelect() }}
    >
      <span className={styles.threadId} title={thread.thread_id}>
        {thread.thread_id}
      </span>

      {isDeleting ? (
        // 删除中状态
        <span className={styles.itemActions}>
          <span className={styles.spinner} aria-label="删除中" />
        </span>
      ) : isConfirmingDelete ? (
        // 二次确认状态
        <div className={`${styles.itemActions} ${styles.confirmDelete}`}>
          <button
            type="button"
            className={`${styles.btn} ${styles.btnDanger}`}
            onClick={(e) => { e.stopPropagation(); onConfirmDelete() }}
            aria-label="确认删除"
          >
            删除
          </button>
          <button
            type="button"
            className={styles.btn}
            onClick={(e) => { e.stopPropagation(); onCancelDelete() }}
            aria-label="取消删除"
          >
            取消
          </button>
        </div>
      ) : (
        // 正常状态：删除按钮不触发列表项选中
        <span className={styles.itemActions}>
          <button
            type="button"
            className={`${styles.btn} ${styles.btnDanger}`}
            onClick={(e) => { e.stopPropagation(); onDeleteClick() }}
            aria-label={`删除会话 ${thread.thread_id}`}
          >
            删除
          </button>
        </span>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// ThreadSidebar 主组件
// ---------------------------------------------------------------------------

interface ThreadSidebarProps {
  /** 当前 URL 中的 threadId（由调用方从 useParams 传入） */
  activeThreadId: string | null
  /** 底部「成果库」入口回调；未传时不渲染该按钮 */
  onOpenArtifacts?: () => void
  /** 底部「设置」入口回调；未传时不渲染该按钮 */
  onOpenSettings?: () => void
}

export function ThreadSidebar({
  activeThreadId,
  onOpenArtifacts,
  onOpenSettings,
}: ThreadSidebarProps) {
  const navigate = useNavigate()

  // Thread 列表（由 TanStack Query 管理）
  const { data: threads, isLoading, isError, error, refetch } = useThreads()

  // 创建 mutation
  const createMutation = useCreateThread()

  // 删除 mutation
  const deleteMutation = useDeleteThread()

  // 删除确认状态（threadId → 正在确认删除）
  const [confirmingDelete, setConfirmingDelete] = useState<string | null>(null)

  // 搜索关键字
  const [searchQuery, setSearchQuery] = useState('')

  // 新建会话：使用 mutate 回调，失败不导航
  function handleCreate() {
    if (createMutation.isPending) return
    createMutation.mutate({}, {
      onSuccess: (thread) => {
        // 只有后端确认成功后才进入新 Thread
        navigate(`/threads/${encodeURIComponent(thread.thread_id)}`)
      },
    })
  }

  // 选中 Thread：导航到对应 URL
  function handleSelect(threadId: string) {
    navigate(`/threads/${encodeURIComponent(threadId)}`)
  }

  // 点击删除按钮：进入二次确认状态
  function handleDeleteClick(threadId: string) {
    setConfirmingDelete(threadId)
  }

  // 确认删除：成功后关闭确认状态，若为当前 Thread 则导航回根路径
  function handleConfirmDelete(threadId: string) {
    setConfirmingDelete(null)
    deleteMutation.mutate(
      { threadId },
      {
        onSuccess: () => {
          // 删除成功后，如果删除的是当前 Thread，导航回根路径
          if (activeThreadId === threadId) {
            navigate('/')
          }
          // 删除其他 Thread 时，当前 URL 不变，列表已刷新
        },
      },
    )
  }

  // 取消删除：关闭确认状态，不发请求
  function handleCancelDelete() {
    setConfirmingDelete(null)
  }

  // 渲染单个会话项（分组复用）
  function renderThreadItem(thread: ThreadDetail) {
    return (
      <ThreadItem
        key={thread.thread_id}
        thread={thread}
        isActive={thread.thread_id === activeThreadId}
        isDeleting={
          deleteMutation.isPending &&
          deleteMutation.variables?.threadId === thread.thread_id
        }
        isConfirmingDelete={confirmingDelete === thread.thread_id}
        onSelect={() => handleSelect(thread.thread_id)}
        onDeleteClick={() => handleDeleteClick(thread.thread_id)}
        onConfirmDelete={() => handleConfirmDelete(thread.thread_id)}
        onCancelDelete={handleCancelDelete}
      />
    )
  }

  // 搜索过滤 + 分组（project_id 非空 → 项目会话，其余 → 普通会话）
  const allThreads = threads ?? []
  const normalizedQuery = searchQuery.trim().toLowerCase()
  const filteredThreads = allThreads.filter((t) => matchesQuery(t, normalizedQuery))
  const normalThreads = filteredThreads.filter((t) => getProjectId(t) === null)
  const projectThreads = filteredThreads.filter((t) => getProjectId(t) !== null)
  const isLoaded = !isLoading && !isError && threads != null

  return (
    <aside className={styles.sidebar} aria-label="会话列表">
      {/* 头部 */}
      <div className={styles.sidebarHeader}>
        <h2 className={styles.sidebarTitle}>会话</h2>
        <div className={styles.sidebarActions}>
          <button
            type="button"
            className={`${styles.btn} ${styles.btnPrimary}`}
            onClick={handleCreate}
            disabled={createMutation.isPending}
            aria-label="新建会话"
          >
            {createMutation.isPending ? (
              <><span className={styles.spinner} aria-hidden="true" /> 创建中…</>
            ) : (
              '+ 新建'
            )}
          </button>
          <button
            type="button"
            className={styles.btn}
            onClick={() => { void refetch() }}
            disabled={isLoading || createMutation.isPending}
            aria-label="刷新会话列表"
            title="刷新"
          >
            ↻
          </button>
        </div>
      </div>

      {/* 搜索 */}
      <div className={styles.searchWrap}>
        <input
          type="search"
          className={styles.searchInput}
          placeholder="搜索会话"
          aria-label="搜索会话"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
        />
      </div>

      {/* 列表区域 */}
      <div className={styles.list}>
        {isLoading && <LoadingState />}
        {isError && error && <ListErrorState error={error} />}
        {isLoaded && allThreads.length === 0 && <EmptyState />}
        {isLoaded && allThreads.length > 0 && filteredThreads.length === 0 && (
          <NoMatchState />
        )}
        {isLoaded && normalThreads.length > 0 && (
          <section className={styles.group} aria-label="普通会话">
            <h3 className={styles.groupTitle}>普通会话</h3>
            {normalThreads.map(renderThreadItem)}
          </section>
        )}
        {isLoaded && projectThreads.length > 0 && (
          <section className={styles.group} aria-label="项目会话">
            <h3 className={styles.groupTitle}>项目会话</h3>
            {projectThreads.map(renderThreadItem)}
          </section>
        )}
      </div>

      {/* 底部入口：仅在传入对应回调时渲染 */}
      {(onOpenArtifacts || onOpenSettings) && (
        <div className={styles.sidebarFooter}>
          {onOpenArtifacts && (
            <button
              type="button"
              className={`${styles.btn} ${styles.footerBtn}`}
              onClick={onOpenArtifacts}
              aria-label="成果库"
            >
              <span aria-hidden="true">▤</span> 成果库
            </button>
          )}
          {onOpenSettings && (
            <button
              type="button"
              className={`${styles.btn} ${styles.footerBtn}`}
              onClick={onOpenSettings}
              aria-label="设置"
            >
              <span aria-hidden="true">⚙</span> 设置
            </button>
          )}
        </div>
      )}
    </aside>
  )
}
