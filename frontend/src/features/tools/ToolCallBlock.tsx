/**
 * ToolCallBlock — 工具调用的折叠块
 *
 * 默认折叠为一行摘要（像消息一样不占地方）：
 *     ▶ 调用 calculator              完成 · 0.1s
 * 展开后显示：入参摘要 + 结果 / 失败原因。
 *
 * 入参只有**形状摘要**（键名 + 类型 + 长度）—— 后端不把参数原值
 * 放进事件流，所以这里也拿不到、更不该假装有。
 */
import { useState } from 'react'
import type { ToolCallEntry } from './useToolCalls'
import styles from './ToolCallBlock.module.css'

export interface ToolCallBlockProps {
  entry: ToolCallEntry
}

const STATUS_LABEL: Record<ToolCallEntry['status'], string> = {
  running: '执行中',
  succeeded: '完成',
  failed: '失败',
}

export function ToolCallBlock({ entry }: ToolCallBlockProps) {
  const [open, setOpen] = useState(false)
  const hasDetail =
    entry.args_summary != null || entry.output != null || entry.error != null

  return (
    <div className={styles.block} data-status={entry.status} data-testid="tool-call-block">
      <button
        type="button"
        className={styles.row}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        title={hasDetail ? '展开/收起' : '没有更多细节'}
      >
        <span className={styles.caret} aria-hidden="true">
          {open ? '▾' : '▸'}
        </span>
        <span className={styles.toolName}>调用 {entry.tool_name}</span>
        <span className={styles.status}>{STATUS_LABEL[entry.status]}</span>
        {entry.duration_seconds != null ? (
          <span className={styles.duration}>{entry.duration_seconds.toFixed(2)}s</span>
        ) : null}
      </button>

      {open && hasDetail ? (
        <div className={styles.detail}>
          {entry.args_summary ? (
            <>
              <div className={styles.detailLabel}>参数</div>
              <pre className={styles.args} data-testid="tool-call-args">
                {entry.args_summary}
              </pre>
            </>
          ) : null}

          {entry.error ? (
            <>
              <div className={styles.detailLabel}>失败</div>
              <pre className={styles.error} data-testid="tool-call-error">
                {entry.error}
              </pre>
            </>
          ) : entry.output ? (
            <>
              <div className={styles.detailLabel}>结果</div>
              <pre className={styles.output} data-testid="tool-call-output">
                {entry.output}
              </pre>
            </>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}
