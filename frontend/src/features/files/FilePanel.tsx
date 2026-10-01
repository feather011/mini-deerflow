/**
 * FilePanel — 右侧文件 / 产物面板（20E）
 *
 * 结构：
 * - Tabs：Uploads / Artifacts 切换
 * - Uploads：上传组件 + 列表（含删除）
 * - Artifacts：只读列表（无上传、无删除）
 *
 * 删除：使用 useDeleteFile 钩，成功 → invalidateQueries → useUploads 自动重拉。
 * 同时把删除函数下传给 FileList（不依赖子组件自取数据）。
 *
 * 不读取 / 展示服务器绝对路径。
 */
import { useState } from 'react'
import { useUploads, useArtifacts, useDeleteFile } from './hooks'
import { FileUpload } from './FileUpload'
import { FileList } from './FileList'
import { ErrorNotice } from '../../shared/ui/ErrorNotice'
import type { ApiError } from '../../shared/api'
import styles from './FilePanel.module.css'

type Tab = 'uploads' | 'artifacts'

interface ErrorBannerProps {
  error: ApiError
  onDismiss: () => void
}

function ErrorBanner({ error, onDismiss }: ErrorBannerProps) {
  return (
    <ErrorNotice
      error={error}
      context="file"
      onDismiss={onDismiss}
      dismissLabel="关闭"
    />
  )
}

interface ListSectionProps {
  scope: 'uploads' | 'artifacts'
  onError: (err: ApiError) => void
  deletingPath: string | null
  onDelete: (path: string) => Promise<void>
}

function ListSection({ scope, onError, deletingPath, onDelete }: ListSectionProps) {
  const uploads = useUploads()
  const artifacts = useArtifacts()
  const query = scope === 'uploads' ? uploads : artifacts

  if (query.isLoading) {
    return <div className={styles.hint}>加载中…</div>
  }

  if (query.isError) {
    return (
      <ErrorBanner
        error={query.error as ApiError}
        onDismiss={() => query.refetch()}
      />
    )
  }

  const entries = query.data?.entries ?? []
  if (entries.length === 0) {
    return (
      <div className={styles.hint}>
        {scope === 'uploads' ? '还没有上传的文件' : '还没有产物'}
      </div>
    )
  }

  return (
    <FileList
      entries={entries}
      scope={scope}
      onError={onError}
      onDelete={scope === 'uploads' ? onDelete : undefined}
      deletingPath={deletingPath}
    />
  )
}

export function FilePanel() {
  const [tab, setTab] = useState<Tab>('uploads')
  const [actionError, setActionError] = useState<ApiError | null>(null)
  const deleteMutation = useDeleteFile()

  async function handleDelete(path: string): Promise<void> {
    // 成功 → useDeleteFile.onSuccess → invalidateQueries(['files','uploads'])
    // → useUploads 自动 refetch → 列表自动移除本条目
    await deleteMutation.mutateAsync({ path })
  }

  return (
    <div className={styles.panel}>
      <div className={styles.tabs} role="tablist">
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'uploads'}
          className={`${styles.tab} ${tab === 'uploads' ? styles.tabActive : ''}`}
          onClick={() => setTab('uploads')}
        >
          Uploads
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'artifacts'}
          className={`${styles.tab} ${tab === 'artifacts' ? styles.tabActive : ''}`}
          onClick={() => setTab('artifacts')}
        >
          Artifacts
        </button>
      </div>

      {/* Uploads 上传组件（仅在 uploads tab 显示） */}
      {tab === 'uploads' && <FileUpload />}

      {actionError && (
        <ErrorBanner
          error={actionError}
          onDismiss={() => setActionError(null)}
        />
      )}

      <ListSection
        scope={tab}
        onError={setActionError}
        deletingPath={
          deleteMutation.isPending && deleteMutation.variables
            ? deleteMutation.variables.path
            : null
        }
        onDelete={handleDelete}
      />
    </div>
  )
}