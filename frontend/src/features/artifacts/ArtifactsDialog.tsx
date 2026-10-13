/**
 * ArtifactsDialog — 成果库对话框（WM-FE1 子任务 C）
 *
 * 演示态：筛选、列表、预览、导出全部为纯前端模拟，不请求后端。
 * 顶部固定标注「演示数据 · 未接入后端」；导出用 Blob + <a download>
 * 在本地生成 .md/.txt 文件，不发网络请求。
 */
import { useState } from 'react'
import type { KeyboardEvent, JSX } from 'react'
import { mockArtifacts } from './mockArtifacts'
import type { MockArtifact } from './mockArtifacts'
import styles from './ArtifactsDialog.module.css'

const FILTERS = ['全部', '笔记', '代码变更', '文档'] as const
type ArtifactFilter = (typeof FILTERS)[number]

function matchFilter(artifact: MockArtifact, filter: ArtifactFilter): boolean {
  return filter === '全部' || artifact.type === filter
}

/** 纯前端导出：Blob → 临时 <a download> → 本地文件，无网络请求 */
function exportArtifact(artifact: MockArtifact): void {
  const ext = artifact.type === '代码变更' ? 'txt' : 'md'
  const blob = new Blob([artifact.text], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `${artifact.title}.${ext}`
  document.body.appendChild(link)
  link.click()
  link.remove()
  // 让浏览器先开始处理下载再释放 Object URL
  setTimeout(() => URL.revokeObjectURL(url), 500)
}

export function ArtifactsDialog(props: {
  open: boolean
  onClose: () => void
}): JSX.Element {
  const { open, onClose } = props
  const [filter, setFilter] = useState<ArtifactFilter>('全部')
  const [selectedId, setSelectedId] = useState<string | null>(null)

  if (!open) return <></>

  function handleKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    if (e.key === 'Escape') onClose()
  }

  const items = mockArtifacts.filter((a) => matchFilter(a, filter))
  // 选中项必须仍在当前筛选结果里，否则回到未选中态
  const selected = items.find((a) => a.id === selectedId) ?? null

  return (
    <div className={styles.backdrop} onClick={onClose}>
      <div
        className={styles.dialog}
        role="dialog"
        aria-modal="true"
        aria-label="成果库"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={handleKeyDown}
      >
        <div className={styles.header}>
          <h2 className={styles.title}>成果库</h2>
        </div>

        <div className={styles.body}>
          <p className={styles.demoNote}>演示数据 · 未接入后端</p>

          <div className={styles.filters} role="group" aria-label="成果筛选">
            {FILTERS.map((f) => (
              <button
                key={f}
                type="button"
                className={`${styles.chip} ${filter === f ? styles.chipActive : ''}`}
                aria-pressed={filter === f}
                onClick={() => setFilter(f)}
              >
                {f}
              </button>
            ))}
          </div>

          {items.length > 0 ? (
            <ul className={styles.list}>
              {items.map((a) => (
                <li key={a.id}>
                  <button
                    type="button"
                    className={`${styles.item} ${selectedId === a.id ? styles.itemActive : ''}`}
                    aria-pressed={selectedId === a.id}
                    onClick={() => setSelectedId(a.id)}
                  >
                    <span className={styles.itemTitle}>{a.title}</span>
                    <span className={styles.pill}>{a.type}</span>
                    <span className={styles.itemSource}>
                      来源会话：{a.source}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <p className={styles.empty}>
              「{filter}」分类下暂无成果，试试其他分类。
            </p>
          )}

          {selected ? (
            <section className={styles.preview} aria-label="成果预览">
              <div className={styles.previewHead}>
                <h3 className={styles.previewTitle}>{selected.title}</h3>
                <button
                  type="button"
                  className={styles.exportBtn}
                  onClick={() => exportArtifact(selected)}
                >
                  导出 {selected.type === '代码变更' ? '.txt' : '.md'}
                </button>
              </div>
              <pre
                className={styles.previewText}
                data-testid="artifact-preview"
              >
                {selected.text}
              </pre>
            </section>
          ) : (
            items.length > 0 && (
              <p className={styles.hint}>选择一条成果查看预览</p>
            )
          )}
        </div>

        <div className={styles.footer}>
          <button type="button" className={styles.btn} onClick={onClose}>
            关闭
          </button>
        </div>
      </div>
    </div>
  )
}
