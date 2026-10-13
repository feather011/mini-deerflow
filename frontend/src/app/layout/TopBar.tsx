/**
 * 工作台顶栏
 *
 * 职责：
 * - 窄屏「打开导航」按钮（抽屉式侧栏）
 * - 当前会话/项目标题 + 面包屑
 * - 「交互原型 · 模拟数据」标识（Mock 能力未接入时如实标注）
 * - 「查看任务」按钮（切换右侧任务审查面板）
 *
 * 不负责数据获取：标题与面包屑由调用方传入。
 */
import styles from './TopBar.module.css'

export interface TopBarProps {
  /** 主标题（会话标题或项目名）；空串时显示占位 */
  title: string
  /** 面包屑文本（如「Mini DeerFlow / 修复分页边界」） */
  breadcrumb?: string
  /** 窄屏菜单按钮回调；不传则不渲染（桌面无需） */
  onOpenNav?: () => void
  /** 「查看任务」按钮回调；不传则不渲染 */
  onTogglePanel?: () => void
  /** 任务面板当前是否打开（按钮 aria-pressed） */
  panelOpen?: boolean
  /** 是否显示「模拟数据」标识（有 Mock 能力时） */
  showDemoTag?: boolean
}

export function TopBar({
  title,
  breadcrumb,
  onOpenNav,
  onTogglePanel,
  panelOpen,
  showDemoTag = false,
}: TopBarProps) {
  return (
    <header className={styles.topbar} aria-label="工作台顶栏">
      {onOpenNav && (
        <button
          type="button"
          className={styles.menuBtn}
          aria-label="打开导航"
          onClick={onOpenNav}
        >
          ☰
        </button>
      )}

      <div className={styles.grow}>
        <div className={styles.title}>{title || 'Mini DeerFlow'}</div>
        {breadcrumb && <div className={styles.breadcrumb}>{breadcrumb}</div>}
      </div>

      {showDemoTag && (
        <span className={styles.demoTag} title="该区域数据为演示内容">
          交互原型 · 模拟数据
        </span>
      )}

      {onTogglePanel && (
        <button
          type="button"
          className={styles.panelBtn}
          aria-pressed={panelOpen ?? false}
          onClick={onTogglePanel}
        >
          {panelOpen ? '收起任务' : '查看任务'}
        </button>
      )}
    </header>
  )
}
