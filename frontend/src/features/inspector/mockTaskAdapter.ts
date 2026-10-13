/**
 * mockTaskAdapter — 任务审查面板的模拟数据适配器（WM-FE1 子任务 B）
 *
 * 数据来源说明（非真实执行结果，UI 上以「演示数据」pill 标注）：
 * - 分页修复示例（demo-verified）的文件 diff 与事件改写自
 *   前端大框架/index.html 原型中的 sampleTask / diffA / diffB，
 *   仅演示布局与交互，不代表任何真实 run 的产物。
 * - demo-unverified 是 FRONTEND_PLAN.md 要求的样例：
 *   「执行完成但验证未运行」——执行结束 ≠ 业务验收通过。
 * - 其余样例（running / failed / stopped / empty）按 FRONTEND_PLAN.md
 *   的状态清单（运行中/已完成/失败/已停止/超时/需要输入；验证：未运行/
 *   运行中/中断/通过/失败）构造，用于演示状态与空态。
 *
 * 归属规则：数据按 threadId 取用，不同 threadId 的任务互不串；
 * threadId 为 null 或未登记的 id 返回 null（此会话暂无任务结果）。
 * 本模块为纯静态数据，不发网络请求、不触碰本地文件。
 */
import type {
  ChangedFile,
  DiffLine,
  TaskAdapter,
  TaskResult,
} from './types'

/** 内置演示 threadId（测试与装配可引用，避免拼错） */
export const MOCK_TASK_IDS = {
  /** 已完成 + 验证通过（含命令证据、add/remove/context diff） */
  verified: 'demo-verified',
  /** 已完成 + 验证未运行（执行完成 ≠ 业务验收通过） */
  unverified: 'demo-unverified',
  /** 运行中 */
  running: 'demo-running',
  /** 失败 + 验证失败（保留已有变更） */
  failed: 'demo-failed',
  /** 用户停止 + 验证中断（保留已有变更） */
  stopped: 'demo-stopped',
  /** 已完成但本轮无文件变更（空态） */
  empty: 'demo-empty',
} as const

// ---- diff 行构造小工具 ----

const ctx = (old_line: number, new_line: number, text: string): DiffLine => ({
  type: 'context',
  old_line,
  new_line,
  text,
})

const add = (new_line: number, text: string): DiffLine => ({
  type: 'add',
  old_line: null,
  new_line,
  text,
})

const rem = (old_line: number, text: string): DiffLine => ({
  type: 'remove',
  old_line,
  new_line: null,
  text,
})

// ---- 演示数据 ----

/** 分页修复（源自原型 diffA）：src/pagination.py */
const paginationPy: ChangedFile = {
  path: 'src/pagination.py',
  change: 'M',
  added: 3,
  removed: 1,
  diff: [
    ctx(1, 1, 'def paginate(items, page, size):'),
    rem(2, '    start = page * size'),
    add(2, '    if page < 1 or size < 1:'),
    add(3, '        raise ValueError("invalid pagination")'),
    add(4, '    start = (page - 1) * size'),
    ctx(3, 5, '    return items[start:start + size]'),
  ],
}

/** 分页回归测试（源自原型 diffB）：tests/test_pagination.py（新增文件） */
const paginationTestPy: ChangedFile = {
  path: 'tests/test_pagination.py',
  change: 'A',
  added: 6,
  removed: 0,
  diff: [
    add(1, 'def test_first_page():'),
    add(2, '    assert paginate([1, 2, 3], 1, 2) == [1, 2]'),
    add(3, ''),
    add(4, 'def test_invalid_page():'),
    add(5, '    with pytest.raises(ValueError):'),
    add(6, '        paginate([1, 2], 0, 2)'),
  ],
}

/** 报告导出修改（demo-unverified）：只改代码，未跑验证 */
const reportPy: ChangedFile = {
  path: 'src/mini_deerflow/report.py',
  change: 'M',
  added: 3,
  removed: 1,
  diff: [
    ctx(1, 1, 'def render_report(result: dict) -> str:'),
    rem(2, '    return str(result)'),
    add(2, '    lines = ["# " + result["title"], ""]'),
    add(3, '    lines.extend(result["items"])'),
    add(4, '    return "\\n".join(lines)'),
    ctx(3, 5, ''),
  ],
}

/** 进行中的搜索接口改动（demo-running） */
const searchPy: ChangedFile = {
  path: 'src/mini_deerflow/search.py',
  change: 'M',
  added: 2,
  removed: 0,
  diff: [
    ctx(1, 1, 'def search_threads(query: str) -> list[str]:'),
    add(2, '    q = query.strip().lower()'),
    add(3, '    return [t for t in titles if q in t.lower()]'),
    ctx(2, 4, '    return titles'),
  ],
}

/** 失败任务保留的改动（demo-failed） */
const discountPy: ChangedFile = {
  path: 'src/mini_deerflow/discount.py',
  change: 'M',
  added: 1,
  removed: 1,
  diff: [
    ctx(1, 1, 'def discount(price, rate):'),
    ctx(2, 2, '    # rate: 0-1 表示折扣率'),
    rem(3, '    return price * rate'),
    add(3, '    return price * (1 - rate)'),
  ],
}

/** 停止任务保留的改动（demo-stopped） */
const notesMd: ChangedFile = {
  path: 'docs/notes.md',
  change: 'M',
  added: 2,
  removed: 1,
  diff: [
    ctx(1, 1, '# 接入笔记'),
    rem(2, '状态：草稿'),
    add(2, '状态：部分完成（任务已停止）'),
    add(3, '待补充：验证命令与结果'),
    ctx(2, 4, ''),
  ],
}

const DEMO_TASKS: Record<string, TaskResult> = {
  [MOCK_TASK_IDS.verified]: {
    thread_id: MOCK_TASK_IDS.verified,
    run_label: 'run-8f2a · 归属当前会话（模拟 run，不对应真实执行）',
    title: '修复分页边界并补回归测试',
    status: 'completed',
    validation: {
      status: 'passed',
      command: 'python -m pytest tests/test_pagination.py -q',
      output: '4 passed in 0.18s',
      note: '示例命令证据，不代表所有验收条件已经确认；业务验收等待用户审查。',
    },
    files: [paginationPy, paginationTestPy],
    events: [
      { time: '00:00', title: '任务开始', description: '执行 Agent 已接收明确任务' },
      { time: '00:02', title: '读取代码', description: 'src/pagination.py' },
      { time: '00:05', title: '修改文件', description: '补充参数检查与页码边界' },
      { time: '00:08', title: '运行验证', description: 'python -m pytest tests/test_pagination.py -q' },
      { time: '00:12', title: '任务结束', description: '保留变更，等待用户审查' },
    ],
  },
  [MOCK_TASK_IDS.unverified]: {
    thread_id: MOCK_TASK_IDS.unverified,
    run_label: 'run-1c4d · 归属当前会话（模拟 run，不对应真实执行）',
    title: '报告导出改为 Markdown 列表',
    // 关键样例：执行完成，但本轮没有运行任何验证命令
    status: 'completed',
    validation: {
      status: 'not_run',
      command: null,
      output: null,
      note: '本轮未运行任何验证命令；没有命令证据不显示测试通过。',
    },
    files: [reportPy],
    events: [
      { time: '00:00', title: '任务开始', description: '执行 Agent 已接收明确任务' },
      { time: '00:03', title: '修改文件', description: 'src/mini_deerflow/report.py' },
      { time: '00:06', title: '任务结束', description: '执行完成；未运行验证，等待用户审查' },
    ],
  },
  [MOCK_TASK_IDS.running]: {
    thread_id: MOCK_TASK_IDS.running,
    run_label: 'run-5b90 · 归属当前会话（模拟 run，不对应真实执行）',
    title: '补充会话搜索接口',
    status: 'running',
    validation: {
      status: 'running',
      command: 'python -m pytest tests/test_search.py -q',
      output: null,
      note: '验证运行中；出结果前不显示通过或失败。',
    },
    files: [searchPy],
    events: [
      { time: '00:00', title: '任务开始', description: '模拟任务已派发' },
      { time: '00:03', title: '修改文件', description: 'src/mini_deerflow/search.py' },
      { time: '00:07', title: '运行验证', description: 'python -m pytest tests/test_search.py -q' },
    ],
  },
  [MOCK_TASK_IDS.failed]: {
    thread_id: MOCK_TASK_IDS.failed,
    run_label: 'run-3e77 · 归属当前会话（模拟 run，不对应真实执行）',
    title: '修正折扣计算口径',
    status: 'failed',
    validation: {
      status: 'failed',
      command: 'python -m pytest tests/test_discount.py -q',
      output: '1 failed, 3 passed in 0.09s',
      note: '验证失败：test_bulk_discount 期望旧口径，需要确认业务预期后再改测试或改实现。',
    },
    files: [discountPy],
    events: [
      { time: '00:00', title: '任务开始', description: '执行 Agent 已接收明确任务' },
      { time: '00:04', title: '修改文件', description: 'src/mini_deerflow/discount.py' },
      { time: '00:08', title: '验证失败', description: '1 failed, 3 passed' },
      { time: '00:09', title: '任务失败', description: '保留已有变更，等待用户审查' },
    ],
  },
  [MOCK_TASK_IDS.stopped]: {
    thread_id: MOCK_TASK_IDS.stopped,
    run_label: 'run-a012 · 归属当前会话（模拟 run，不对应真实执行）',
    title: '整理接入笔记',
    status: 'stopped',
    validation: {
      status: 'interrupted',
      command: null,
      output: null,
      note: '任务被停止，验证未完成；没有完整命令结果不显示通过。',
    },
    files: [notesMd],
    events: [
      { time: '00:00', title: '任务开始', description: '模拟任务已派发' },
      { time: '00:04', title: '用户停止', description: '进度已停止，保留已产生的变更' },
    ],
  },
  [MOCK_TASK_IDS.empty]: {
    thread_id: MOCK_TASK_IDS.empty,
    run_label: 'run-9d31 · 归属当前会话（模拟 run，不对应真实执行）',
    title: '梳理接入方案（未改代码）',
    status: 'completed',
    validation: {
      status: 'not_run',
      command: null,
      output: null,
      note: '本轮为只读梳理，没有代码变更，也未运行验证命令。',
    },
    files: [],
    events: [
      { time: '00:00', title: '任务开始', description: '只读梳理接入方案' },
      { time: '00:05', title: '任务结束', description: '无文件变更，结论已在对话中给出' },
    ],
  },
}

/** 模拟适配器：按 threadId 归属取任务；未登记的 id 与 null 返回 null */
export const mockTaskAdapter: TaskAdapter = {
  getTask(threadId: string | null): TaskResult | null {
    if (!threadId) return null
    return DEMO_TASKS[threadId] ?? null
  },
}
