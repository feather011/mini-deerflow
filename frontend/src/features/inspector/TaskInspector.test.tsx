/**
 * TaskInspector 组件测试
 *
 * 覆盖：
 * - open=false 不渲染内容；× 按钮触发 onClose
 * - 头部：状态 pill 语义色、run 归属副标题、底部固定说明
 * - 七个任务状态 / 五个验证状态的文案与语义色映射
 * - 页签切换（变化/验证/过程，role=tablist + aria-selected）
 * - diff 渲染：add/remove/context 三色行 + 新旧行号
 * - 文件列表切换：点击文件显示对应 diff
 * - 空态：本轮暂无文件变更 / 此会话暂无任务结果（threadId 为 null 或未知 id）
 * - 「执行完成但验证未运行」样例：不显示测试通过
 * - thread 隔离：A/B 两个 threadId 数据不串，页签与文件选择不残留
 *
 * 数据策略：本测试显式通过 `adapter={mockTaskAdapter}` 注入演示数据，
 * 生产路径使用 realTaskAdapter（本组件默认），其覆盖见 verify_realTaskAdapter.ts
 */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import {
  MOCK_TASK_IDS,
  TASK_STATUS_META,
  VALIDATION_STATUS_META,
  TaskInspector,
  clearRealTaskData,
  ingestRealTaskData,
  mockTaskAdapter,
} from './index'

function renderInspector(threadId: string | null) {
  const onClose = vi.fn()
  const view = render(
    <TaskInspector
      open
      onClose={onClose}
      threadId={threadId}
      adapter={mockTaskAdapter}
    />,
  )
  return { ...view, onClose }
}

// ---- 无 Run：产品口径「暂无运行」，不得显示「运行中」----

describe('TaskInspector 无 Run 状态', () => {
  it('生产数据源下没有任何 Run 时，pill 显示「暂无运行」且页面无「运行中」', () => {
    clearRealTaskData()
    ingestRealTaskData('thread-no-run', {
      streamStatus: 'idle',
      events: [],
      runId: null,
      title: '还没有派发过任务的会话',
    })
    try {
      render(
        <TaskInspector open onClose={vi.fn()} threadId="thread-no-run" />,
      )
      const pill = screen.getByTestId('task-status-pill')
      expect(pill).toHaveTextContent('暂无运行')
      expect(pill).toHaveAttribute('data-tone', 'neutral')
      expect(screen.queryByText('运行中')).toBeNull()
    } finally {
      clearRealTaskData()
    }
  })

  it('Run 正在跑时才显示「运行中」', () => {
    clearRealTaskData()
    ingestRealTaskData('thread-running', {
      streamStatus: 'streaming',
      events: [],
      runId: 'run-xyz',
      title: '正在跑的会话',
    })
    try {
      render(
        <TaskInspector open onClose={vi.fn()} threadId="thread-running" />,
      )
      expect(screen.getByTestId('task-status-pill')).toHaveTextContent('运行中')
    } finally {
      clearRealTaskData()
    }
  })
})

// ---- open / 关闭 ----

describe('TaskInspector 开关', () => {
  it('open=false 时不渲染任何内容', () => {
    const { container } = render(
      <TaskInspector
        open={false}
        onClose={vi.fn()}
        threadId={MOCK_TASK_IDS.verified}
        adapter={mockTaskAdapter}
      />,
    )
    expect(container.innerHTML).toBe('')
    expect(screen.queryByRole('complementary', { name: '任务审查面板' })).toBeNull()
  })

  it('open=true 时渲染面板（aside[aria-label=任务审查面板]）', () => {
    renderInspector(MOCK_TASK_IDS.verified)
    expect(
      screen.getByRole('complementary', { name: '任务审查面板' }),
    ).toBeInTheDocument()
  })

  it('右上角 × 按钮触发 onClose', () => {
    const { onClose } = renderInspector(MOCK_TASK_IDS.verified)
    fireEvent.click(screen.getByRole('button', { name: '关闭任务面板' }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})

// ---- 头部与底部 ----

describe('TaskInspector 头部与底部', () => {
  it('标题「任务结果」 + 状态 pill（语义色正确）', () => {
    renderInspector(MOCK_TASK_IDS.verified)
    expect(screen.getByRole('heading', { name: '任务结果' })).toBeInTheDocument()
    const statusPill = screen.getByTestId('task-status-pill')
    expect(statusPill).toHaveTextContent('已完成')
    expect(statusPill).toHaveAttribute('data-tone', 'success')
    // 不再展示「演示数据」pill：数据由测试注入而非生产路径固有的演示属性
    expect(screen.queryByTestId('demo-pill')).toBeNull()
  })

  it('副标题展示 run 归属说明（含模拟 run 标注：mock fixture 来源）', () => {
    renderInspector(MOCK_TASK_IDS.verified)
    const subtitle = screen.getByText(/run-8f2a/)
    expect(subtitle).toHaveTextContent('归属当前会话')
    expect(subtitle).toHaveTextContent('模拟 run')
  })

  it('底部固定说明：执行完成不等于业务验收通过；不自动提交或推送', () => {
    renderInspector(MOCK_TASK_IDS.verified)
    const footerText = document.querySelector('aside footer')?.textContent ?? ''
    expect(footerText).toContain('执行完成不等于业务验收通过')
    expect(footerText).toContain('不自动提交或推送')
  })

  it('七个任务状态的文案与语义色映射齐全', () => {
    expect(TASK_STATUS_META).toEqual({
      no_run: { label: '暂无运行', tone: 'neutral' },
      running: { label: '运行中', tone: 'warning' },
      completed: { label: '已完成', tone: 'success' },
      failed: { label: '失败', tone: 'danger' },
      stopped: { label: '已停止', tone: 'warning' },
      timeout: { label: '超时', tone: 'danger' },
      needs_input: { label: '需要输入', tone: 'warning' },
    })
  })

  it('五个验证状态的文案与语义色映射齐全', () => {
    expect(VALIDATION_STATUS_META).toEqual({
      not_run: { label: '未运行', tone: 'neutral' },
      running: { label: '运行中', tone: 'warning' },
      interrupted: { label: '中断', tone: 'warning' },
      passed: { label: '通过', tone: 'success' },
      failed: { label: '失败', tone: 'danger' },
    })
  })

  it('失败/运行中/停止任务的状态 pill 语义色正确', () => {
    const { rerender } = render(
      <TaskInspector
        open
        onClose={vi.fn()}
        threadId={MOCK_TASK_IDS.failed}
        adapter={mockTaskAdapter}
      />,
    )
    expect(screen.getByTestId('task-status-pill')).toHaveTextContent('失败')
    expect(screen.getByTestId('task-status-pill')).toHaveAttribute('data-tone', 'danger')

    rerender(
      <TaskInspector
        open
        onClose={vi.fn()}
        threadId={MOCK_TASK_IDS.running}
        adapter={mockTaskAdapter}
      />,
    )
    expect(screen.getByTestId('task-status-pill')).toHaveTextContent('运行中')
    expect(screen.getByTestId('task-status-pill')).toHaveAttribute('data-tone', 'warning')

    rerender(
      <TaskInspector
        open
        onClose={vi.fn()}
        threadId={MOCK_TASK_IDS.stopped}
        adapter={mockTaskAdapter}
      />,
    )
    expect(screen.getByTestId('task-status-pill')).toHaveTextContent('已停止')
    expect(screen.getByTestId('task-status-pill')).toHaveAttribute('data-tone', 'warning')
  })
})

// ---- 页签 ----

describe('TaskInspector 页签', () => {
  it('默认选中「变化」，三个页签在 role=tablist 中', () => {
    renderInspector(MOCK_TASK_IDS.verified)
    const tabs = within(screen.getByRole('tablist', { name: '任务详情' })).getAllByRole('tab')
    expect(tabs.map((t) => t.textContent)).toEqual(['变化', '验证', '过程'])
    expect(tabs[0]).toHaveAttribute('aria-selected', 'true')
    expect(tabs[1]).toHaveAttribute('aria-selected', 'false')
    expect(tabs[2]).toHaveAttribute('aria-selected', 'false')
  })

  it('点击「验证」切到验证页签，「过程」切到时间线', () => {
    renderInspector(MOCK_TASK_IDS.verified)
    fireEvent.click(screen.getByRole('tab', { name: '验证' }))
    expect(screen.getByTestId('validation-pill')).toHaveTextContent('通过')
    expect(screen.getByRole('tab', { name: '验证' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tab', { name: '变化' })).toHaveAttribute('aria-selected', 'false')
    expect(screen.queryByText(/本轮变更文件/)).toBeNull()

    fireEvent.click(screen.getByRole('tab', { name: '过程' }))
    expect(screen.getByText('任务开始')).toBeInTheDocument()
    expect(screen.getByText('python -m pytest tests/test_pagination.py -q')).toBeInTheDocument()
    expect(screen.queryByTestId('validation-pill')).toBeNull()
  })
})

// ---- 变化页签：文件列表 + diff ----

describe('TaskInspector 变化页签', () => {
  it('文件列表展示路径与增删计数，默认选中第一个文件', () => {
    renderInspector(MOCK_TASK_IDS.verified)
    expect(screen.getByText('2 个本轮变更文件 · 原有修改单独保留')).toBeInTheDocument()
    const fileButtons = screen.getAllByRole('button').filter((b) =>
      b.getAttribute('aria-pressed') !== null,
    )
    expect(fileButtons).toHaveLength(2)
    expect(fileButtons[0]).toHaveTextContent('src/pagination.py')
    expect(fileButtons[0]).toHaveTextContent('+3 −1')
    expect(fileButtons[1]).toHaveTextContent('tests/test_pagination.py')
    expect(fileButtons[1]).toHaveTextContent('+6 −0')
    expect(fileButtons[0]).toHaveAttribute('aria-pressed', 'true')
    expect(fileButtons[1]).toHaveAttribute('aria-pressed', 'false')
  })

  it('diff 渲染 add/remove/context 三色行与新旧行号', () => {
    renderInspector(MOCK_TASK_IDS.verified)
    const rows = Array.from(document.querySelectorAll('[data-line-type]'))
    expect(rows).toHaveLength(6)
    expect(rows.filter((r) => r.getAttribute('data-line-type') === 'add')).toHaveLength(3)
    expect(rows.filter((r) => r.getAttribute('data-line-type') === 'remove')).toHaveLength(1)
    expect(rows.filter((r) => r.getAttribute('data-line-type') === 'context')).toHaveLength(2)

    const removeRow = rows.find((r) => r.getAttribute('data-line-type') === 'remove')!
    // 旧文件行号 2，新增行号为空
    expect(removeRow.children[0]).toHaveTextContent('2')
    expect(removeRow.children[1]!.textContent).toBe('')
    expect(removeRow.textContent).toContain('-    start = page * size')

    const addedRow = rows.find(
      (r) =>
        r.getAttribute('data-line-type') === 'add' &&
        r.textContent?.includes('start = (page - 1) * size'),
    )!
    expect(addedRow.children[0]!.textContent).toBe('')
    expect(addedRow.children[1]).toHaveTextContent('4')
  })

  it('点击第二个文件切换到对应 diff', () => {
    renderInspector(MOCK_TASK_IDS.verified)
    fireEvent.click(screen.getByRole('button', { name: /tests\/test_pagination\.py/ }))
    expect(screen.getByText('tests/test_pagination.py · 本轮变化')).toBeInTheDocument()
    expect(document.querySelectorAll('[data-line-type="add"]')).toHaveLength(6)
    expect(document.querySelector('[data-line-type="remove"]')).toBeNull()
  })

  it('空态：本轮暂无文件变更', () => {
    renderInspector(MOCK_TASK_IDS.empty)
    expect(screen.getByText('本轮暂无文件变更')).toBeInTheDocument()
    expect(document.querySelectorAll('[data-line-type]')).toHaveLength(0)
  })
})

// ---- 验证页签 ----

describe('TaskInspector 验证页签', () => {
  it('通过：展示命令证据与输出', () => {
    renderInspector(MOCK_TASK_IDS.verified)
    fireEvent.click(screen.getByRole('tab', { name: '验证' }))
    expect(screen.getByTestId('validation-pill')).toHaveTextContent('通过')
    expect(
      screen.getByText('python -m pytest tests/test_pagination.py -q'),
    ).toBeInTheDocument()
    expect(screen.getByText('4 passed in 0.18s')).toBeInTheDocument()
  })

  it('执行完成但验证未运行：显示「未运行」，不显示测试通过', () => {
    renderInspector(MOCK_TASK_IDS.unverified)
    // 任务状态仍是已完成
    expect(screen.getByTestId('task-status-pill')).toHaveTextContent('已完成')
    fireEvent.click(screen.getByRole('tab', { name: '验证' }))
    expect(screen.getByTestId('validation-pill')).toHaveTextContent('未运行')
    expect(screen.getByTestId('validation-pill')).toHaveAttribute('data-tone', 'neutral')
    // 没有命令证据：不出现独立的「通过」字样，也不出现命令/输出块
    expect(screen.queryByText('通过', { exact: true })).toBeNull()
    expect(screen.queryByText(/passed/)).toBeNull()
    // 明确提示
    expect(
      screen.getByText('执行完成不等于业务验收通过；没有命令证据不显示测试通过。'),
    ).toBeInTheDocument()
  })

  it('失败：展示失败状态与命令输出', () => {
    renderInspector(MOCK_TASK_IDS.failed)
    fireEvent.click(screen.getByRole('tab', { name: '验证' }))
    expect(screen.getByTestId('validation-pill')).toHaveTextContent('失败')
    expect(screen.getByTestId('validation-pill')).toHaveAttribute('data-tone', 'danger')
    expect(screen.getByText('1 failed, 3 passed in 0.09s')).toBeInTheDocument()
  })

  it('停止：验证显示「中断」', () => {
    renderInspector(MOCK_TASK_IDS.stopped)
    fireEvent.click(screen.getByRole('tab', { name: '验证' }))
    expect(screen.getByTestId('validation-pill')).toHaveTextContent('中断')
  })
})

// ---- thread 归属与隔离 ----

describe('TaskInspector thread 隔离', () => {
  it('threadId=null：显示「此会话暂无任务结果」', () => {
    renderInspector(null)
    expect(screen.getByText(/此会话暂无任务结果/)).toBeInTheDocument()
    expect(screen.queryByTestId('task-status-pill')).toBeNull()
    expect(screen.getByText(/未选择会话/)).toBeInTheDocument()
  })

  it('未知 threadId：同样显示暂无任务结果，不借用其他会话数据', () => {
    renderInspector('thread-unknown-404')
    expect(screen.getByText(/此会话暂无任务结果/)).toBeInTheDocument()
    expect(screen.queryByTestId('task-status-pill')).toBeNull()
  })

  it('A/B 两个 threadId 数据不串，且页签与文件选择不残留', () => {
    const { rerender } = render(
      <TaskInspector
        open
        onClose={vi.fn()}
        threadId={MOCK_TASK_IDS.verified}
        adapter={mockTaskAdapter}
      />,
    )
    // A：已完成 + 分页 diff；切到「验证」页签并选中第二个文件
    expect(screen.getByTestId('task-status-pill')).toHaveTextContent('已完成')
    expect(screen.getByText('src/pagination.py · 本轮变化')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /tests\/test_pagination\.py/ }))
    expect(screen.getByText('tests/test_pagination.py · 本轮变化')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: '验证' }))
    expect(screen.getByTestId('validation-pill')).toHaveTextContent('通过')

    // B：失败任务 —— 状态、文件、页签都应是 B 自己的（回落到变化页签）
    rerender(
      <TaskInspector
        open
        onClose={vi.fn()}
        threadId={MOCK_TASK_IDS.failed}
        adapter={mockTaskAdapter}
      />,
    )
    expect(screen.getByTestId('task-status-pill')).toHaveTextContent('失败')
    expect(screen.getByText('src/mini_deerflow/discount.py · 本轮变化')).toBeInTheDocument()
    expect(screen.queryByText('tests/test_pagination.py · 本轮变化')).toBeNull()
    expect(screen.queryByTestId('validation-pill')).toBeNull()
    expect(screen.getByRole('tab', { name: '变化' })).toHaveAttribute('aria-selected', 'true')

    // 回到 A：恢复 A 自己的上次选择（验证页签），且不出现 B 的任何内容
    rerender(
      <TaskInspector
        open
        onClose={vi.fn()}
        threadId={MOCK_TASK_IDS.verified}
        adapter={mockTaskAdapter}
      />,
    )
    expect(screen.getByTestId('task-status-pill')).toHaveTextContent('已完成')
    expect(screen.getByRole('tab', { name: '验证' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByTestId('validation-pill')).toHaveTextContent('通过')
    expect(screen.queryByText(/discount\.py/)).toBeNull()
  })
})
