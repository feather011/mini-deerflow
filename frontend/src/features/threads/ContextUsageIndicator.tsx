/**
 * ContextUsageIndicator — 上下文用量圆环 + 浮层
 *
 * 设计契约：
 * - 不发起请求、不持有数据；父组件提供 `snapshot` 与 `error`。
 * - 圆环始终可见；点击 / Enter / Space 切换浮层。
 * - 浮层打开后按 Escape 关闭（document keydown 监听）。
 * - 状态用「颜色 + 文字」双重表达：
 *     empty（无数据）｜ safe（<50%）｜ warn（50-80%）｜ critical（≥80%）
 * - 数字加千分位；**9 个分区全部显示（含 0%）**——口径完整是本面板的价值；
   压缩前后仅在可用时显示。
 */
import { useEffect, useId, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import {
  BREAKDOWN_LABELS,
  classifyTone,
  formatTokens,
  resolveTone,
  snapshotPct,
  toneLabel,
} from '../../shared/types/contextUsage'
import type {
  ContextUsageBreakdown,
  ContextUsageError,
  ContextUsageSnapshot,
  UsageTone,
} from '../../shared/types/contextUsage'
import styles from './ContextUsageIndicator.module.css'

const RING_SIZE = 26
const RING_STROKE = 3
const RING_RADIUS = (RING_SIZE - RING_STROKE) / 2
const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS

interface Props {
  snapshot: ContextUsageSnapshot | null
  error: ContextUsageError
}

export function ContextUsageIndicator({ snapshot, error }: Props) {
  const [open, setOpen] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const popupId = useId()

  const tone = resolveTone(snapshot, error)
  const pct = snapshot ? snapshotPct(snapshot) : 0

  // Escape 关闭浮层。
  useEffect(() => {
    if (!open) return
    function handleKey(e: globalThis.KeyboardEvent) {
      if (e.key === 'Escape') {
        setOpen(false)
        buttonRef.current?.focus()
      }
    }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [open])

  // 浮层外的点击关闭（点击容器内其他区域也保留——只对容器外有效）。
  useEffect(() => {
    if (!open) return
    function handleMouseDown(e: MouseEvent) {
      const target = e.target as Node | null
      if (target && containerRef.current && !containerRef.current.contains(target)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handleMouseDown)
    return () => document.removeEventListener('mousedown', handleMouseDown)
  }, [open])

  function handleClick() {
    setOpen((o) => !o)
  }

  function handleKeyDown(e: KeyboardEvent<HTMLButtonElement>) {
    // Enter / Space 切换；与原生 button[type=button] 行为一致。
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      setOpen((o) => !o)
    }
  }

  const ringLabel = toneLabel(tone)
  const ringNumber = snapshot ? `${pct}%` : '—'
  const arcLength = snapshot ? (pct / 100) * RING_CIRCUMFERENCE : 0

  return (
    <div ref={containerRef} className={styles.wrap}>
      <button
        ref={buttonRef}
        type="button"
        className={`${styles.ringBtn} ${styles[`tone_${tone}`]}`}
        onClick={handleClick}
        onKeyDown={handleKeyDown}
        title={`上下文用量：${ringLabel}`}
        aria-label={`上下文用量：${ringLabel}`}
        aria-expanded={open}
        aria-controls={popupId}
        aria-haspopup="dialog"
      >
        {/* 环 + 占比数字（环内），状态文字排在环右侧——避免二者重叠 */}
        <span className={styles.ringWrap}>
          <svg
            width={RING_SIZE}
            height={RING_SIZE}
            viewBox={`0 0 ${RING_SIZE} ${RING_SIZE}`}
            aria-hidden="true"
            className={styles.ringSvg}
          >
            <circle
              cx={RING_SIZE / 2}
              cy={RING_SIZE / 2}
              r={RING_RADIUS}
              fill="none"
              strokeWidth={RING_STROKE}
              className={styles.ringTrack}
            />
            {snapshot && (
              <circle
                cx={RING_SIZE / 2}
                cy={RING_SIZE / 2}
                r={RING_RADIUS}
                fill="none"
                strokeWidth={RING_STROKE}
                strokeDasharray={`${arcLength} ${RING_CIRCUMFERENCE}`}
                strokeLinecap="round"
                transform={`rotate(-90 ${RING_SIZE / 2} ${RING_SIZE / 2})`}
                className={styles.ringFill}
              />
            )}
          </svg>
          <span className={styles.ringNumber}>{ringNumber}</span>
        </span>
        <span className={styles.ringStatus}>{ringLabel}</span>
      </button>

      {open && (
        <div
          id={popupId}
          role="dialog"
          aria-label="上下文用量详情"
          className={styles.popup}
        >
          {snapshot ? (
            <SnapshotContent
            snapshot={snapshot}
            tone={classifyTone(snapshot)}
            pct={pct}
          />
          ) : error ? (
            <ErrorContent error={error} />
          ) : (
            <EmptyContent />
          )}
        </div>
      )}
    </div>
  )
}

function SnapshotContent({
  snapshot,
  tone,
  pct,
}: {
  snapshot: ContextUsageSnapshot
  tone: Exclude<UsageTone, 'empty' | 'error'>
  pct: number
}) {
  // hover 联动：悬停列表行时高亮对应分段（反向不做的原因：
  // 部分分段不足 1px，无法可靠命中）。
  const [hoverKey, setHoverKey] = useState<keyof ContextUsageBreakdown | null>(null)

  const budget = snapshot.budget
  const used = snapshot.total
  // 9 项**全部**展示（含 0%）——口径完整是这个面板的核心价值，
  // 过滤掉 0 值会让「哪些来源没用上」这件事不可见。
  const rows = BREAKDOWN_LABELS.map(({ key, label, color }) => ({
    key,
    label,
    color,
    value: snapshot.breakdown[key],
  }))
  // 分段条只画非 0 段（0 宽的段在 DOM 里毫无意义）
  const segments = rows.filter((row) => row.value > 0)

  return (
    <>
      <header className={styles.popupHeader}>
        <span className={styles.popupTitle}>上下文用量</span>
        <span className={`${styles.popupBadge} ${styles[`tone_${tone}`]}`}>
          最近一次发送 · {toneLabel(tone)}
        </span>
      </header>

      <div className={styles.headline}>
        <span className={styles.headlinePct}>{pct}%</span>
        <span className={styles.headlineUsed}>
          已使用 {formatTokens(used)} / {formatTokens(budget)}
        </span>
      </div>
      <div className={styles.headlineMeta}>
        <span>输入预算 {formatTokens(budget)}</span>
        <span aria-hidden="true">·</span>
        <span>估算 {formatTokens(used)} tokens</span>
      </div>

      {/* 分段进度条：每段宽度 = 分类值 / 输入预算，拼起来正好等于占用率。
          段间 2px 表面色间隙（不画描边）；0 值分类不占段。 */}
      <div
        className={styles.segTrack}
        role="img"
        aria-label={`用量构成：共 ${formatTokens(used)} tokens，占输入预算 ${pct}%，分 ${segments.length} 个分区`}
      >
        {segments.map((row) => {
          const share = budget > 0 ? (row.value / budget) * 100 : 0
          const dimmed = hoverKey !== null && hoverKey !== row.key
          const active = hoverKey === row.key
          return (
            <span
              key={row.key}
              data-seg-key={row.key}
              data-share={share}
              className={`${styles.seg} ${dimmed ? styles.segDim : ''} ${active ? styles.segActive : ''}`}
              style={{
                background: row.color,
                width: `calc(${Math.max(share, 0)}% - 2px)`,
                marginRight: 2,
              }}
            />
          )
        })}
      </div>

      {snapshot.compacted && snapshot.tokens_before !== null && (
        <div className={styles.compactionRow}>
          <span className={styles.compactionLabel}>压缩前</span>
          <span className={styles.compactionValue}>
            {formatTokens(snapshot.tokens_before)} → {formatTokens(snapshot.total)}
          </span>
          <span className={styles.compactionDelta}>
            (Δ {formatTokens(snapshot.total - snapshot.tokens_before)})
          </span>
        </div>
      )}

      {/* 明细里的百分比是「占本次输入的比重」，与头部「占用输入预算」
          是两个不同口径——必须写明，否则 95% 会被误读为预算占用。 */}
      <div className={styles.breakdownTitle}>分区明细（占本次输入）</div>
      <ul className={styles.breakdown} aria-label="分区明细">
        {rows.map((row) => {
          const ratio =
            used > 0 ? Math.round((row.value / used) * 100) : 0
          return (
            <li
              key={row.key}
              data-row-key={row.key}
              className={`${styles.breakdownRow} ${hoverKey === row.key ? styles.breakdownRowActive : ''}`}
              onMouseEnter={() => setHoverKey(row.key)}
              onMouseLeave={() => setHoverKey(null)}
            >
              {/* 身份靠圆点 + 文字标签，数值本身不穿系列色 */}
              <span
                className={styles.dot}
                data-color={row.color}
                style={{ background: row.color }}
                aria-hidden="true"
              />
              <span className={styles.breakdownLabel}>{row.label}</span>
              <span className={styles.breakdownValue}>
                {/* tokens 是次要信息（灰、小），百分比是主信息（黑、稍粗）
                    ——参考图右对齐的百分比即为阅读主入口 */}
                <span className={styles.breakdownTokens}>
                  {formatTokens(row.value)}
                </span>
                <span className={styles.breakdownRatio}>{ratio}%</span>
              </span>
            </li>
          )
        })}
      </ul>

      <footer className={styles.popupFooter}>
        <span>数据来源：后端 ContextAssembler 最近一次组装结果</span>
      </footer>
    </>
  )
}

function EmptyContent() {
  return (
    <div className={styles.emptyState} role="status">
      <div className={styles.emptyTitle}>暂无统计</div>
      <div className={styles.emptyHint}>
        发送一次消息后，这里会显示实际送给模型的输入用量。
      </div>
    </div>
  )
}

function ErrorContent({ error }: { error: ContextUsageError }) {
  if (!error) return null
  return (
    <div className={styles.errorState} role="alert">
      <div className={styles.emptyTitle}>请求失败</div>
      <div className={styles.emptyHint}>{error.message}</div>
    </div>
  )
}