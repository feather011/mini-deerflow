/**
 * ProjectDetail — 项目概览组件
 *
 * 职责：
 * - 显示项目信息（名称、创建时间）
 * - 简单说明（实际会话管理在 ProjectSidebar 的展开列表中）
 */
import type { ProjectSummary } from '../../shared/types/projects'
import styles from './ProjectDetail.module.css'

interface ProjectDetailProps {
  project: ProjectSummary
}

export function ProjectDetail({ project }: ProjectDetailProps) {
  const createdDate = new Date(project.created_at).toLocaleDateString('zh-CN', {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
  })

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <h2 className={styles.title}>{project.name}</h2>
        <p className={styles.meta}>创建于 {createdDate}</p>
      </div>

      <div className={styles.info}>
        <p className={styles.hint}>
          在左侧展开项目，可查看和创建会话
        </p>
      </div>
    </div>
  )
}
