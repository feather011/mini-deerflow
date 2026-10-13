/**
 * CreateProjectDialog — 「新建项目」对话框
 *
 * 流程（GOAL.md Phase 4）：
 *   输入项目名 → 创建，后端自动创建项目目录
 */
import { useState } from 'react'
import type { KeyboardEvent } from 'react'
import { useCreateProject } from './hooks'
import { ErrorNotice } from '../../shared/ui/ErrorNotice'
import type { ProjectSummary } from '../../shared/types/projects'
import styles from './CreateProjectDialog.module.css'

interface CreateProjectDialogProps {
  onClose: () => void
  onCreated: (project: ProjectSummary) => void
}

export function CreateProjectDialog({
  onClose,
  onCreated,
}: CreateProjectDialogProps) {
  const [name, setName] = useState('')
  const createMutation = useCreateProject()

  function handleSubmit() {
    if (createMutation.isPending) return
    createMutation.mutate(
      { name: name.trim() },
      { onSuccess: (project) => onCreated(project) },
    )
  }

  function handleKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    if (e.key === 'Escape') onClose()
  }

  return (
    <div className={styles.backdrop} onClick={onClose}>
      <div
        className={styles.dialog}
        role="dialog"
        aria-modal="true"
        aria-label="新建项目"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={handleKeyDown}
      >
        <h2 className={styles.title}>新建项目</h2>

        <p className={styles.body}>
          输入项目名称，创建后将在后端自动创建项目目录。
        </p>

        <div className={styles.field}>
          <label className={styles.label} htmlFor="project-name">
            项目名
          </label>
          <input
            id="project-name"
            className={styles.input}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="输入项目名称"
            autoFocus
          />
          <p className={styles.hint}>
            创建后将在后端自动创建项目目录
          </p>
        </div>

        {createMutation.isError && createMutation.error != null && (
          <ErrorNotice
            error={createMutation.error}
            context="project"
            ariaLabel="创建项目失败"
          />
        )}

        <div className={styles.actions}>
          <button type="button" className={styles.btn} onClick={onClose}>
            取消
          </button>
          <button
            type="button"
            className={`${styles.btn} ${styles.btnPrimary}`}
            onClick={handleSubmit}
            disabled={name.trim() === '' || createMutation.isPending}
          >
            {createMutation.isPending ? '创建中…' : '创建'}
          </button>
        </div>
      </div>
    </div>
  )
}
