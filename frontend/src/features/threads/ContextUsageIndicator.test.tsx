/**
 * ContextUsageIndicator 组件测试
 *
 * 覆盖：
 * - 无数据：圆环显示 "—"，浮层打开显示「暂无统计」
 * - 有数据：圆环显示百分比 + 状态色
 * - 状态分桶：safe / warn / critical 按阈值
 * - 键盘打开（Enter / Space）；Escape 关闭
 * - 点击外部关闭浮层
 * - 压缩前后信息：仅 compacted=True 时显示
 * - 分区明细 9 项全部显示（含 0%）
 * - 错误态：error 有值时浮层显示「请求失败」
 */
import { describe, expect, it } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import {
  classifyTone,
  resolveTone,
  snapshotPct,
  toneLabel,
  BREAKDOWN_LABELS,
} from '../../shared/types/contextUsage'
import type { ContextUsageSnapshot } from '../../shared/types/contextUsage'
import { ContextUsageIndicator } from './ContextUsageIndicator'

// ---- 工具 ----

function makeSnapshot(overrides: Partial<ContextUsageSnapshot> = {}): ContextUsageSnapshot {
  return {
    thread_id: 't1',
    run_id: 'run-1',
    budget: 1000,
    total: 500,
    breakdown: {
      system: 0,
      current_input: 100,
      active_tool_cycle: 0,
      recent_messages: 300,
      summary: 100,
      task_notes: 0,
      memory: 0,
      skills: 0,
      tool_schemas: 0,
    },
    compacted: false,
    tokens_before: null,
    soft_limit: 800,
    captured_at: 1000,
    ...overrides,
  }
}

function makeSnapshotWithCompaction(): ContextUsageSnapshot {
  return makeSnapshot({
    total: 200,
    breakdown: {
      system: 0,
      current_input: 50,
      active_tool_cycle: 0,
      recent_messages: 100,
      summary: 50,
      task_notes: 0,
      memory: 0,
      skills: 0,
      tool_schemas: 0,
    },
    compacted: true,
    tokens_before: 800,
  })
}

// ---- tone / pct 单元 ----

describe('classifyTone', () => {
  it('null → empty', () => {
    expect(classifyTone(null)).toBe('empty')
  })
  it('total/budget < 50% → safe', () => {
    expect(classifyTone(makeSnapshot({ total: 100, budget: 1000 }))).toBe('safe')
  })
  it('total/budget 50% → warn', () => {
    expect(classifyTone(makeSnapshot({ total: 500, budget: 1000 }))).toBe('warn')
  })
  it('total/budget 80% → critical', () => {
    expect(classifyTone(makeSnapshot({ total: 800, budget: 1000 }))).toBe('critical')
  })
  it('total/budget > 100% 也算 critical（实际被硬上限挡住，但保险）', () => {
    expect(classifyTone(makeSnapshot({ total: 1200, budget: 1000 }))).toBe('critical')
  })
})

describe('snapshotPct', () => {
  it('四舍五入到整数', () => {
    expect(snapshotPct(makeSnapshot({ total: 333, budget: 1000 }))).toBe(33)
    expect(snapshotPct(makeSnapshot({ total: 335, budget: 1000 }))).toBe(34)
  })
  it('budget=0 时返回 0', () => {
    expect(snapshotPct(makeSnapshot({ total: 100, budget: 0 }))).toBe(0)
  })
})

describe('resolveTone', () => {
  it('无数据且请求失败 → error（不是 empty）', () => {
    expect(resolveTone(null, { kind: 'fetch_failed', message: 'Not Found' })).toBe('error')
  })
  it('无数据且无失败 → empty', () => {
    expect(resolveTone(null, null)).toBe('empty')
  })
  it('有数据但本次刷新失败 → 仍按占用率分档（数据优先，不丢上一次快照）', () => {
    expect(
      resolveTone(makeSnapshot({ total: 900, budget: 1000 }), {
        kind: 'fetch_failed',
        message: 'boom',
      }),
    ).toBe('critical')
    expect(
      resolveTone(makeSnapshot({ total: 100, budget: 1000 }), {
        kind: 'fetch_failed',
        message: 'boom',
      }),
    ).toBe('safe')
  })
})

describe('toneLabel', () => {
  it('五个 tone 都有非空标签', () => {
    expect(toneLabel('error')).toBe('请求失败')
    expect(toneLabel('empty')).toBe('暂无统计')
    expect(toneLabel('safe')).toBe('充足')
    expect(toneLabel('warn')).toBe('接近预算')
    expect(toneLabel('critical')).toBe('触发压缩')
  })
})

// ---- 组件 ----

describe('ContextUsageIndicator', () => {
  it('无数据：圆环显示「—」+「暂无统计」', () => {
    render(<ContextUsageIndicator snapshot={null} error={null} />)
    const btn = screen.getByRole('button', { name: /上下文用量/ })
    expect(btn).toHaveTextContent('—')
    expect(btn).toHaveTextContent('暂无统计')

    fireEvent.click(btn)
    const popup = screen.getByRole('dialog')
    expect(within(popup).getByText('暂无统计')).toBeTruthy()
  })

  it('请求失败：浮层显示「请求失败」', () => {
    render(
      <ContextUsageIndicator
        snapshot={null}
        error={{ kind: 'fetch_failed', message: 'Network Error' }}
      />,
    )
    fireEvent.click(screen.getByRole('button'))
    const popup = screen.getByRole('dialog')
    expect(within(popup).getByText('请求失败')).toBeTruthy()
    expect(within(popup).getByText('Network Error')).toBeTruthy()
  })

  it('请求失败：圆环也必须报「请求失败」，不得同时写「暂无统计」', () => {
    // 回归：曾出现圆环写「暂无统计」而浮层写「请求失败」的自相矛盾
    render(
      <ContextUsageIndicator
        snapshot={null}
        error={{ kind: 'fetch_failed', message: 'Not Found' }}
      />,
    )
    const btn = screen.getByRole('button')
    expect(btn).toHaveTextContent('请求失败')
    expect(btn).not.toHaveTextContent('暂无统计')

    fireEvent.click(btn)
    const popup = screen.getByRole('dialog')
    // 浮层与圆环状态一致
    expect(within(popup).getByText('请求失败')).toBeTruthy()
    expect(within(popup).queryByText('暂无统计')).toBeNull()
  })

  it('请求失败但有旧数据：仍展示数据（不因刷新失败丢掉有效快照）', () => {
    render(
      <ContextUsageIndicator
        snapshot={makeSnapshot({ total: 900, budget: 1000 })}
        error={{ kind: 'fetch_failed', message: 'boom' }}
      />,
    )
    const btn = screen.getByRole('button')
    expect(btn).toHaveTextContent('90%')
    expect(btn).not.toHaveTextContent('请求失败')

    fireEvent.click(btn)
    const popup = screen.getByRole('dialog')
    expect(within(popup).getByText(/输入预算/)).toBeTruthy()
    expect(within(popup).queryByText('请求失败')).toBeNull()
  })

  it('有数据：圆环显示百分比与状态文字', () => {
    render(
      <ContextUsageIndicator
        snapshot={makeSnapshot({ total: 500, budget: 1000 })}
        error={null}
      />,
    )
    const btn = screen.getByRole('button')
    expect(btn).toHaveTextContent('50%')
    expect(btn).toHaveTextContent('接近预算') // warn
  })

  it('点击圆环打开浮层，再点关闭', () => {
    render(<ContextUsageIndicator snapshot={makeSnapshot()} error={null} />)
    const btn = screen.getByRole('button')
    expect(btn).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(btn)
    expect(btn).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('dialog')).toBeTruthy()
    fireEvent.click(btn)
    expect(btn).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('键盘打开：Enter', () => {
    render(<ContextUsageIndicator snapshot={makeSnapshot()} error={null} />)
    const btn = screen.getByRole('button')
    btn.focus()
    fireEvent.keyDown(btn, { key: 'Enter' })
    expect(screen.getByRole('dialog')).toBeTruthy()
  })

  it('键盘打开：Space', () => {
    render(<ContextUsageIndicator snapshot={makeSnapshot()} error={null} />)
    const btn = screen.getByRole('button')
    btn.focus()
    fireEvent.keyDown(btn, { key: ' ' })
    expect(screen.getByRole('dialog')).toBeTruthy()
  })

  it('键盘关闭：Escape 关闭并把焦点还给圆环', () => {
    render(<ContextUsageIndicator snapshot={makeSnapshot()} error={null} />)
    const btn = screen.getByRole('button')
    fireEvent.click(btn)
    expect(screen.getByRole('dialog')).toBeTruthy()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(btn)
  })

  it('点击外部关闭浮层', () => {
    render(
      <div>
        <div data-testid="outside">外部</div>
        <ContextUsageIndicator snapshot={makeSnapshot()} error={null} />
      </div>,
    )
    fireEvent.click(screen.getByRole('button'))
    expect(screen.getByRole('dialog')).toBeTruthy()
    fireEvent.mouseDown(screen.getByTestId('outside'))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('压缩前后：compacted=True 时显示「压缩前 X → Y」', () => {
    render(
      <ContextUsageIndicator
        snapshot={makeSnapshotWithCompaction()}
        error={null}
      />,
    )
    fireEvent.click(screen.getByRole('button'))
    const popup = screen.getByRole('dialog')
    // 找到「压缩前」那一行的 value 容器
    const compactionRow = within(popup).getByText(/压缩前/).parentElement
    expect(compactionRow).toBeTruthy()
    // 该行应同时含 tokens_before(800) 与 total(200)
    expect(compactionRow?.textContent).toMatch(/800/)
    expect(compactionRow?.textContent).toMatch(/200/)
    // 必有 Δ 标记（压缩带来的减少量）
    expect(compactionRow?.textContent).toMatch(/Δ/)
  })

  it('压缩前后：未压缩时**不**显示压缩行', () => {
    render(<ContextUsageIndicator snapshot={makeSnapshot()} error={null} />)
    fireEvent.click(screen.getByRole('button'))
    const popup = screen.getByRole('dialog')
    expect(within(popup).queryByText(/压缩前/)).toBeNull()
  })

  it('分区明细：9 项全部显示，含 0% 的系统提示', () => {
    const snap = makeSnapshot({
      breakdown: {
        system: 0,
        current_input: 100,
        active_tool_cycle: 0,
        recent_messages: 200,
        summary: 0,
        task_notes: 0,
        memory: 0,
        skills: 50,
        tool_schemas: 0,
      },
    })
    render(<ContextUsageIndicator snapshot={snap} error={null} />)
    fireEvent.click(screen.getByRole('button'))
    const popup = screen.getByRole('dialog')
    const list = within(popup).getByRole('list', { name: '分区明细' })
    const items = within(list).getAllByRole('listitem')
    // 9 个真实分区全部出现——口径完整，0 值不隐藏
    expect(items).toHaveLength(9)
    expect(within(list).getByText('技能')).toBeTruthy()
    expect(within(list).getByText('近期对话')).toBeTruthy()
    expect(within(list).getByText('当前输入')).toBeTruthy()
    // 恒为 0 的系统提示也必须出现（参考图风格：0% 同样可见）
    expect(within(list).getByText('系统提示')).toBeTruthy()
  })

  it('9 行数值之和等于 total（不重复计数、不遗漏）', () => {
    const snap = makeSnapshot({
      total: 1000,
      breakdown: {
        system: 0,
        current_input: 100,
        active_tool_cycle: 50,
        recent_messages: 300,
        summary: 200,
        task_notes: 0,
        memory: 100,
        skills: 50,
        tool_schemas: 200,
      },
    })
    render(<ContextUsageIndicator snapshot={snap} error={null} />)
    fireEvent.click(screen.getByRole('button'))
    const list = screen.getByRole('list', { name: '分区明细' })
    const sum = BREAKDOWN_LABELS.reduce(
      (acc, { key }) => acc + snap.breakdown[key],
      0,
    )
    expect(sum).toBe(snap.total)
    expect(within(list).getAllByRole('listitem')).toHaveLength(
      BREAKDOWN_LABELS.length,
    )
  })

  it('分段宽度之和与占用率同口径（段宽 = 分类值 / 输入预算）', () => {
    const snap = makeSnapshot({ total: 500, budget: 1000 })
    render(<ContextUsageIndicator snapshot={snap} error={null} />)
    fireEvent.click(screen.getByRole('button'))
    // 用 data-seg-key 取分段
    const segments = Array.from(
      document.querySelectorAll('[data-seg-key]'),
    ) as HTMLElement[]
    // 500/1000 = 50% → 至少有非 0 段
    expect(segments.length).toBeGreaterThan(0)
    const totalShare = segments.reduce(
      (acc, el) => acc + Number(el.dataset.share ?? 0),
      0,
    )
    expect(totalShare).toBeCloseTo(50, 5)
  })

  it('圆点取色绑定分类（不因显示顺序换色）', () => {
    render(<ContextUsageIndicator snapshot={makeSnapshot()} error={null} />)
    fireEvent.click(screen.getByRole('button'))
    BREAKDOWN_LABELS.forEach(({ key, color }) => {
      const row = document.querySelector(`[data-row-key="${key}"]`)!
      expect(row).toBeTruthy()
      const dot = row.querySelector('span') as HTMLElement
      // 颜色经 data-color 暴露，不依赖 jsdom 解析 CSS shorthand
      expect(dot.dataset.color).toBe(color)
    })
  })

  it('hover 列表行时对应分段进入高亮态', () => {
    render(<ContextUsageIndicator snapshot={makeSnapshot()} error={null} />)
    fireEvent.click(screen.getByRole('button'))
    // 用非 0 的分区（默认快照里 tool_schemas=0 → 不会渲染分段）
    const row = document.querySelector(
      '[data-row-key="recent_messages"]',
    ) as HTMLElement
    expect(row).toBeTruthy()
    // 必须在事件之后查询：React 重渲染会替换节点，缓存旧引用读不到新 class
    fireEvent.mouseEnter(row)
    expect(
      document.querySelector('[data-seg-key="recent_messages"]')!.className,
    ).toContain('segActive')
    // 同时其它分段被压暗（对照组，证明是联动而非全部高亮）
    expect(
      document.querySelector('[data-seg-key="summary"]')!.className,
    ).toContain('segDim')

    fireEvent.mouseLeave(row)
    expect(
      document.querySelector('[data-seg-key="recent_messages"]')!.className,
    ).not.toContain('segActive')
  })

  it('头部保留「输入预算」「估算」「最近一次发送」三项标注', () => {
    render(<ContextUsageIndicator snapshot={makeSnapshot()} error={null} />)
    fireEvent.click(screen.getByRole('button'))
    const popup = screen.getByRole('dialog')
    expect(within(popup).getByText(/输入预算/)).toBeTruthy()
    expect(within(popup).getByText(/估算/)).toBeTruthy()
    expect(within(popup).getByText(/最近一次发送/)).toBeTruthy()
    // 新增：参考图式的大号占用率 + 已使用/预算
    expect(within(popup).getByText(/已使用/)).toBeTruthy()
  })

  it('九分区键齐全：BREAKDOWN_LABELS 与 backend 同序', () => {
    expect(BREAKDOWN_LABELS.map((b) => b.key)).toEqual([
      'summary',
      'task_notes',
      'memory',
      'skills',
      'recent_messages',
      'current_input',
      'active_tool_cycle',
      'tool_schemas',
      'system',
    ])
  })

  it('浮层标注「输入预算」与「估算」', () => {
    render(<ContextUsageIndicator snapshot={makeSnapshot()} error={null} />)
    fireEvent.click(screen.getByRole('button'))
    const popup = screen.getByRole('dialog')
    expect(within(popup).getByText(/输入预算/)).toBeTruthy()
    expect(within(popup).getByText(/估算/)).toBeTruthy()
  })

  it('浮层标注「最近一次发送」', () => {
    render(<ContextUsageIndicator snapshot={makeSnapshot()} error={null} />)
    fireEvent.click(screen.getByRole('button'))
    expect(within(screen.getByRole('dialog')).getByText(/最近一次发送/)).toBeTruthy()
  })
})