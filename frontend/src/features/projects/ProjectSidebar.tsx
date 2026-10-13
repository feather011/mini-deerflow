/**
 * ProjectSidebar — 项目列表侧边栏（展开结构）
 *
 * 职责：
 * - 显示项目列表（loading / empty / error / success）
 * - 每个项目可展开/收起，显示项目内会话
 * - 创建新项目（打开 CreateProjectDialog）
 * - 在项目内创建会话
 * - 删除项目（内联二次确认）
 * - 删除项目内会话
 * - 选中会话高亮
 *
 * 状态管理原则：
 * - 列表数据由 TanStack Query 管理，组件只消费
 * - 创建/删除成功后再更新，失败不改变状态
 */
import { useNavigate } from 'react-router-dom'
import { useState } from 'react'
import { CreateProjectDialog, useProjects, useDeleteProject, useProjectThreads, useCreateProjectThread, useDeleteProjectThread } from './'
import { ErrorNotice } from '../../shared/ui/ErrorNotice'
import type { ProjectSummary } from '../../shared/types/projects'
import type { ThreadDetail } from '../../shared/types/threads'
import type { ApiError } from '../../shared/api/client'
import styles from './ProjectSidebar.module.css'

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

function EmptyProjectsState({ onCreate }: { onCreate: () => void }) {
  return (
    <div className={styles.empty}>
      <span className={styles.emptyIcon} aria-hidden="true">📁</span>
      <p className={styles.emptyTitle}>还没有项目</p>
      <p className={styles.emptyBody}>创建一个项目，绑定本地 Git 仓库</p>
      <button
        type="button"
        className={`${styles.btn} ${styles.btnPrimary}`}
        onClick={onCreate}
      >
        新建项目
      </button>
    </div>
  )
}

function ErrorState({ error, onRetry }: { error: ApiError; onRetry: () => void }) {
  return (
    <div className={styles.error}>
      <ErrorNotice
        error={error}
        context="project"
        ariaLabel="项目列表错误"
      />
      <button type="button" className={styles.retryBtn} onClick={onRetry}>
        重试
      </button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// 项目项组件
// ---------------------------------------------------------------------------

interface ProjectItemProps {
  project: ProjectSummary
  isExpanded: boolean
  activeThreadId: string | null
  onToggleExpand: () => void
  onSelectThread: (threadId: string) => void
  onDeleteProject: () => void
}

function ProjectItem({
  project,
  isExpanded,
  activeThreadId,
  onToggleExpand,
  onSelectThread,
  onDeleteProject,
}: ProjectItemProps) {
  const { data: threads, isLoading: isLoadingThreads, isError: isErrorThreads, error: threadsError, refetch } = useProjectThreads(project.project_id)
  const createMutation = useCreateProjectThread()
  const deleteMutation = useDeleteProjectThread()
  const [confirmingDeleteThread, setConfirmingDeleteThread] = useState<string | null>(null)
  const [confirmingDeleteProject, setConfirmingDeleteProject] = useState(false)

  function getThreadTitle(thread: ThreadDetail): string {
    const firstUser = thread.messages.find((m) => m.role === 'user')
    const text = firstUser?.content ?? ''
    return text.length > 30 ? text.slice(0, 30) + '…' : text || '新会话'
  }

  function handleCreateThread() {
    if (createMutation.isPending) return
    createMutation.mutate(
      { projectId: project.project_id },
      {
        onSuccess: (thread) => {
          onSelectThread(thread.thread_id)
        },
      }
    )
  }

  function handleSelectThread(threadId: string) {
    onSelectThread(threadId)
  }

  function handleDeleteThread(threadId: string) {
    setConfirmingDeleteThread(threadId)
  }

  function confirmDeleteThread(threadId: string) {
    setConfirmingDeleteThread(null)
    deleteMutation.mutate(
      { threadId, projectId: project.project_id },
      {
        onSuccess: () => {
          if (activeThreadId === threadId) {
            // 导航到根路径
            window.location.href = '/'
          }
        },
      }
    )
  }

  return (
    <div className={styles.projectItem}>
      {/* 项目行 */}
      <div className={styles.projectRow}>
        <button
          type="button"
          className={styles.expandBtn}
          onClick={onToggleExpand}
          aria-expanded={isExpanded}
          aria-label={isExpanded ? '收起' : '展开'}
        >
          <span className={`${styles.chevron} ${isExpanded ? styles.chevronExpanded : ''}`}>▶</span>
        </button>
        <button
          type="button"
          className={styles.projectName}
          onClick={onToggleExpand}
          title={project.name}
        >
          {project.name}
        </button>
        <div className={styles.projectActions}>
          <button
            type="button"
            className={`${styles.btn} ${styles.btnIcon}`}
            onClick={(e) => { e.stopPropagation(); handleCreateThread() }}
            disabled={createMutation.isPending}
            title="新建会话"
          >
            {createMutation.isPending ? <span className={styles.spinnerSm} /> : '+'}
          </button>
          {confirmingDeleteProject ? (
            <div className={styles.confirmDelete}>
              <button
                type="button"
                className={`${styles.btn} ${styles.btnDanger}`}
                onClick={(e) => { e.stopPropagation(); onDeleteProject() }}
              >
                删除
              </button>
              <button
                type="button"
                className={styles.btn}
                onClick={(e) => { e.stopPropagation(); setConfirmingDeleteProject(false) }}
              >
                取消
              </button>
            </div>
          ) : (
            <button
              type="button"
              className={`${styles.btn} ${styles.btnDanger}`}
              onClick={(e) => { e.stopPropagation(); setConfirmingDeleteProject(true) }}
              title="删除项目"
            >
              ✕
            </button>
          )}
        </div>
      </div>

      {/* 展开的会话列表 */}
      {isExpanded && (
        <div className={styles.threadList}>
          {isLoadingThreads && (
            <div className={styles.threadLoading}>
              <span className={styles.spinnerSm} />
              加载中…
            </div>
          )}
          {isErrorThreads && threadsError && (
            <div className={styles.threadError}>
              <ErrorNotice
                error={threadsError}
                context="thread"
                ariaLabel="会话列表错误"
              />
              <button type="button" className={styles.retryBtn} onClick={() => void refetch()}>
                重试
              </button>
            </div>
          )}
          {!isLoadingThreads && !isErrorThreads && threads && (
            <>
              {threads.length === 0 ? (
                <div className={styles.emptyThreads}>
                  还没有会话
                </div>
              ) : (
                threads.map((thread) => (
                  <div
                    key={thread.thread_id}
                    className={`${styles.threadItem} ${thread.thread_id === activeThreadId ? styles.threadItemActive : ''}`}
                  >
                    <button
                      type="button"
                      className={styles.threadName}
                      onClick={() => handleSelectThread(thread.thread_id)}
                      title={getThreadTitle(thread)}
                    >
                      {getThreadTitle(thread)}
                    </button>
                    {confirmingDeleteThread === thread.thread_id ? (
                      <div className={styles.confirmDelete}>
                        <button
                          type="button"
                          className={`${styles.btn} ${styles.btnDanger}`}
                          onClick={() => confirmDeleteThread(thread.thread_id)}
                        >
                          删除
                        </button>
                        <button
                          type="button"
                          className={styles.btn}
                          onClick={() => setConfirmingDeleteThread(null)}
                        >
                          取消
                        </button>
                      </div>
                    ) : (
                      <button
                        type="button"
                        className={`${styles.btn} ${styles.btnDanger}`}
                        onClick={() => handleDeleteThread(thread.thread_id)}
                        title="删除会话"
                      >
                        ✕
                      </button>
                    )}
                  </div>
                ))
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// ProjectSidebar 主组件
// ---------------------------------------------------------------------------

interface ProjectSidebarProps {
  /** 当前 URL 中的 threadId */
  activeThreadId?: string | null
  /** 窄屏时选择会话后关闭回调 */
  onCloseDrawer?: () => void
}

export function ProjectSidebar({ activeThreadId = null, onCloseDrawer }: ProjectSidebarProps) {
  const { data: projects, isLoading, isError, error, refetch } = useProjects()
  const deleteProjectMutation = useDeleteProject()
  const navigate = useNavigate()

  // 展开的项目 ID（默认无展开）
  const [expandedProjects, setExpandedProjects] = useState<Set<string>>(new Set())
  const [showCreate, setShowCreate] = useState(false)

  function handleToggleExpand(projectId: string) {
    setExpandedProjects((prev) => {
      const next = new Set(prev)
      if (next.has(projectId)) {
        next.delete(projectId)
      } else {
        next.add(projectId)
      }
      return next
    })
  }

  function handleSelectThread(threadId: string) {
    navigate(`/threads/${encodeURIComponent(threadId)}`)
    onCloseDrawer?.()
  }

  function handleDeleteProject(projectId: string) {
    deleteProjectMutation.mutate(
      { projectId },
      {
        onSuccess: () => {
          // 如果当前在删除的项目下，导航到根路径
          const currentPath = window.location.pathname
          if (currentPath.includes(`/projects/${projectId}`) || currentPath.includes(`/threads/`)) {
            navigate('/')
          }
        },
      }
    )
  }

  return (
    <>
      <aside className={styles.sidebar} aria-label="项目列表">
        {/* 头部 */}
        <div className={styles.sidebarHeader}>
          <h2 className={styles.sidebarTitle}>项目</h2>
          <div className={styles.sidebarActions}>
            <button
              type="button"
              className={`${styles.btn} ${styles.btnPrimary}`}
              onClick={() => setShowCreate(true)}
              aria-label="新建项目"
            >
              + 新建
            </button>
          </div>
        </div>

        {/* 列表区域 */}
        <div className={styles.list}>
          {isLoading && <LoadingState />}
          {isError && error && <ErrorState error={error} onRetry={() => void refetch()} />}
          {!isLoading && !isError && projects != null && projects.length === 0 && (
            <EmptyProjectsState onCreate={() => setShowCreate(true)} />
          )}
          {!isLoading && !isError && projects != null && projects.map((project) => (
            <ProjectItem
              key={project.project_id}
              project={project}
              isExpanded={expandedProjects.has(project.project_id)}
              activeThreadId={activeThreadId}
              onToggleExpand={() => handleToggleExpand(project.project_id)}
              onSelectThread={handleSelectThread}
              onDeleteProject={() => handleDeleteProject(project.project_id)}
            />
          ))}
        </div>
      </aside>

      {/* 新建项目对话框 */}
      {showCreate && (
        <CreateProjectDialog
          onClose={() => setShowCreate(false)}
          onCreated={() => setShowCreate(false)}
        />
      )}
    </>
  )
}
