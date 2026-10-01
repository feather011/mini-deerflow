/**
 * FileList — 文件 / 产物列表（20E）
 *
 * 显示项：文件名、相对路径、大小、MIME、文件/目录类型、下载按钮
 * Uploads 额外显示删除按钮（含二次确认）
 *
 * 删除通过 props 传入的 deleteFn 触发（来自 FilePanel 的 useDeleteFile）。
 * 成功由调用方负责缓存失效与 UI 更新。
 */
import { useState } from 'react'
import type { FileMetadata } from '../../shared/types/files'
import type { ApiError } from '../../shared/api'
import { downloadFile, downloadArtifact } from './api'
import styles from './FileList.module.css'

interface FileListProps {
  entries: FileMetadata[]
  /** 'uploads' 或 'artifacts' */
  scope: 'uploads' | 'artifacts'
  /** 触发下载或删除失败时的回调 */
  onError?: (err: ApiError) => void
  /** 删除函数；uploads scope 必须提供 */
  onDelete?: (path: string) => Promise<void>
  /** 是否正在删除某条（控制按钮 disabled） */
  deletingPath?: string | null
}

function formatSize(bytes: number): string {
  if (bytes === 0) return '0 B'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`
}

async function triggerDownload(
  blob: Blob,
  filename: string,
): Promise<void> {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  // 让浏览器先开始处理下载再释放 Object URL，避免部分浏览器时序差异
  setTimeout(() => {
    URL.revokeObjectURL(url)
  }, 0)
}

function FileItem({
  entry,
  scope,
  onError,
  onDelete,
  deletingPath,
}: {
  entry: FileMetadata
  scope: 'uploads' | 'artifacts'
  onError?: (err: ApiError) => void
  onDelete?: (path: string) => Promise<void>
  deletingPath?: string | null
}) {
  const [confirming, setConfirming] = useState(false)
  const isUploads = scope === 'uploads'
  const busy = deletingPath === entry.path

  async function handleDownload() {
    try {
      const blob =
        scope === 'uploads'
          ? await downloadFile(entry.path)
          : await downloadArtifact(entry.path)
      await triggerDownload(blob, entry.name)
    } catch (err) {
      onError?.(err as ApiError)
    }
  }

  async function handleDelete() {
    if (!onDelete) return
    try {
      await onDelete(entry.path)
      setConfirming(false)
    } catch (err) {
      onError?.(err as ApiError)
    }
  }

  return (
    <li className={styles.item} data-kind={entry.kind}>
      <div className={styles.line1}>
        <span className={styles.name} title={entry.name}>
          {entry.name}
        </span>
        <span className={styles.kind}>{entry.kind}</span>
      </div>
      <div className={styles.line2}>
        <span className={styles.path}>{entry.path}</span>
      </div>
      <div className={styles.line3}>
        <span>{formatSize(entry.size)}</span>
        <span className={styles.mime}>
          {entry.media_type ?? 'application/octet-stream'}
        </span>
      </div>
      <div className={styles.actions}>
        <button
          type="button"
          className={styles.actionBtn}
          onClick={handleDownload}
          disabled={busy || entry.kind === 'directory'}
        >
          {entry.kind === 'directory' ? '目录不可下载' : '下载'}
        </button>

        {isUploads && (
          <>
            {!confirming ? (
              <button
                type="button"
                className={`${styles.actionBtn} ${styles.deleteBtn}`}
                onClick={() => setConfirming(true)}
                disabled={busy || entry.kind === 'directory'}
              >
                删除
              </button>
            ) : (
              <>
                <button
                  type="button"
                  className={`${styles.actionBtn} ${styles.confirmBtn}`}
                  onClick={handleDelete}
                  disabled={busy}
                >
                  {busy ? '删除中…' : '确认删除'}
                </button>
                <button
                  type="button"
                  className={styles.actionBtn}
                  onClick={() => setConfirming(false)}
                  disabled={busy}
                >
                  取消
                </button>
              </>
            )}
          </>
        )}
      </div>
    </li>
  )
}

export function FileList({
  entries,
  scope,
  onError,
  onDelete,
  deletingPath,
}: FileListProps) {
  return (
    <ul className={styles.list}>
      {entries.map((entry) => (
        <FileItem
          key={entry.path}
          entry={entry}
          scope={scope}
          onError={onError}
          onDelete={onDelete}
          deletingPath={deletingPath}
        />
      ))}
    </ul>
  )
}