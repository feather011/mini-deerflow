/**
 * ProjectFilePanel — 项目工作区文件浏览面板
 *
 * 职责：
 * - 显示项目工作区文件树
 * - 浏览目录
 * - 读取文件内容
 *
 * 状态管理：
 * - 数据由 TanStack Query 管理
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getProject } from './api'
import { browseWorkspace, getFileContent } from './workspaceApi'
import styles from './ProjectFilePanel.module.css'

// ---------------------------------------------------------------------------
// 子组件
// ---------------------------------------------------------------------------

interface LoadingStateProps {
  message: string
}

function LoadingState({ message }: LoadingStateProps) {
  return (
    <div className={styles.loading}>
      <span className={styles.spinner} aria-hidden="true" />
      {message}
    </div>
  )
}

interface FileEntry {
  name: string
  is_dir: boolean
  size: number
  modified: string | null
}

interface FileItemProps {
  entry: FileEntry
  onNavigate: (name: string, isDir: boolean) => void
}

function FileItem({ entry, onNavigate }: FileItemProps) {
  const icon = entry.is_dir ? '📁' : '📄'
  const size = entry.is_dir ? '' : formatSize(entry.size)

  return (
    <div
      className={styles.fileItem}
      onClick={() => onNavigate(entry.name, entry.is_dir)}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          onNavigate(entry.name, entry.is_dir)
        }
      }}
    >
      <span className={styles.fileIcon}>{icon}</span>
      <span className={styles.fileName}>{entry.name}</span>
      <span className={styles.fileSize}>{size}</span>
    </div>
  )
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

interface BreadcrumbProps {
  path: string
  onNavigate: (path: string) => void
}

function Breadcrumb({ path, onNavigate }: BreadcrumbProps) {
  const parts = path === '.' ? [] : path.split('/')

  return (
    <div className={styles.breadcrumb}>
      <button
        type="button"
        className={styles.breadcrumbItem}
        onClick={() => onNavigate('.')}
      >
        /
      </button>
      {parts.map((part, index) => {
        const currentPath = parts.slice(0, index + 1).join('/')
        return (
          <span key={currentPath}>
            <span className={styles.breadcrumbSeparator}>/</span>
            <button
              type="button"
              className={styles.breadcrumbItem}
              onClick={() => onNavigate(currentPath)}
            >
              {part}
            </button>
          </span>
        )
      })}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

interface ProjectFilePanelProps {
  projectId: string
}

export function ProjectFilePanel({ projectId }: ProjectFilePanelProps) {
  const [currentPath, setCurrentPath] = useState<string>('.')
  const [selectedFile, setSelectedFile] = useState<string | null>(null)

  const { data: project } = useQuery({
    queryKey: ['project', projectId],
    queryFn: () => getProject(projectId),
  })

  const { data: browseData, isLoading: isLoadingBrowse } = useQuery({
    queryKey: ['project-workspace', projectId, currentPath],
    queryFn: () => browseWorkspace(projectId, currentPath === '.' ? undefined : currentPath),
    enabled: !!projectId,
  })

  const { data: fileContent, isLoading: isLoadingFile } = useQuery({
    queryKey: ['project-file', projectId, selectedFile],
    queryFn: () => getFileContent(projectId, selectedFile!),
    enabled: !!selectedFile,
  })

  function handleNavigate(name: string, isDir: boolean) {
    if (isDir) {
      setCurrentPath(currentPath === '.' ? name : `${currentPath}/${name}`)
      setSelectedFile(null)
    } else {
      setSelectedFile(currentPath === '.' ? name : `${currentPath}/${name}`)
    }
  }

  function handleBreadcrumb(path: string) {
    setCurrentPath(path)
    setSelectedFile(null)
  }

  if (!projectId) {
    return <div className={styles.error}>未指定项目</div>
  }

  return (
    <div className={styles.panel}>
      <div className={styles.header}>
        <h3 className={styles.title}>工作区</h3>
        <span className={styles.projectName}>{project?.name}</span>
      </div>

      <Breadcrumb path={currentPath} onNavigate={handleBreadcrumb} />

      <div className={styles.content}>
        <div className={styles.fileList}>
          {isLoadingBrowse && <LoadingState message="加载文件列表..." />}

          {!isLoadingBrowse && browseData && (
            <>
              {/* 返回上级目录 */}
              {currentPath !== '.' && (
                <div
                  className={styles.fileItem}
                  onClick={() => {
                    const parts = currentPath.split('/')
                    parts.pop()
                    handleBreadcrumb(parts.join('/') || '.')
                  }}
                  role="button"
                  tabIndex={0}
                >
                  <span className={styles.fileIcon}>📂</span>
                  <span className={styles.fileName}>..</span>
                </div>
              )}

              {browseData.entries.length === 0 ? (
                <div className={styles.empty}>空目录</div>
              ) : (
                browseData.entries.map((entry: FileEntry) => (
                  <FileItem
                    key={entry.name}
                    entry={entry}
                    onNavigate={handleNavigate}
                  />
                ))
              )}
            </>
          )}
        </div>

        {selectedFile && (
          <div className={styles.filePreview}>
            <div className={styles.previewHeader}>
              <span className={styles.previewTitle}>{selectedFile}</span>
              <button
                type="button"
                className={styles.closeBtn}
                onClick={() => setSelectedFile(null)}
              >
                ✕
              </button>
            </div>
            <div className={styles.previewContent}>
              {isLoadingFile ? (
                <LoadingState message="加载文件..." />
              ) : fileContent ? (
                <pre className={styles.codeBlock}>
                  {fileContent.content}
                </pre>
              ) : null}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
