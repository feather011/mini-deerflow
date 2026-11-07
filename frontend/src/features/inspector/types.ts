/**
 * 任务审查面板数据类型（WM-FE1 子任务 B）
 *
 * 语义对齐 前端大框架/FRONTEND_PLAN.md「状态必须如实呈现」：
 * - 任务状态与验证状态分开建模（执行完成 ≠ 业务验收通过）
 * - 没有命令证据（command/output 为 null）时，UI 不得显示测试通过
 */
/**
 * 任务状态
 *
 * `no_run` 与 `running` 必须分开：还没有任何 Run 时说"运行中"是假的
 * （产品口径：暂无运行）。
 */
export type TaskStatus =
  | 'no_run'
  | 'running'
  | 'completed'
  | 'failed'
  | 'stopped'
  | 'timeout'
  | 'needs_input'

/** 验证状态（独立于任务状态） */
export type ValidationStatus =
  | 'not_run'
  | 'running'
  | 'interrupted'
  | 'passed'
  | 'failed'
  | 'unverified'

/**
 * 后端 Guard 的权威判定。
 *
 * 与前端自己按退出码推断的结论**分开建模**：判定以代码算出的 guard 为准，
 * 前端不得用自己的推断覆盖它（那是"判定与证据对不上"的根源）。
 */
export type GuardVerdict = 'verified' | 'unverified' | 'failed'

/** 后端 guard 字段的结构化形态 */
export interface GuardEvidence {
  verdict: GuardVerdict
  /** 后端给的判定标签（如「验证通过」「有改动但未验证」） */
  label: string
  /** 判定依据（为什么会是这个结论） */
  reason: string
  /** 验收条件是否与实际执行的命令建立了关联；null = 后端没给这个信息 */
  acceptance_covered: boolean | null
}

/**
 * 单条验证命令的结构化证据。
 *
 * `exit_code` 为 null 表示**后端没能解析出退出码** —— 必须显示成
 * 「缺失／未判定」，绝不能当成 0（当成 0 就是把"没读到"说成"通过了"）。
 */
export interface VerificationEntry {
  /** 实际执行的命令 */
  command: string
  /** 结构化退出码；null = 缺失 */
  exit_code: number | null
  /** 是否通过；null = 退出码缺失，无法判定 */
  passed: boolean | null
  /** 命令输出 */
  output: string | null
}

/** diff 行类型 */
export type DiffLineType = 'add' | 'remove' | 'context'

/** 单行 unified diff */
export interface DiffLine {
  type: DiffLineType
  /** 旧文件行号（新增行没有 → null） */
  old_line: number | null
  /** 新文件行号（删除行没有 → null） */
  new_line: number | null
  /** 行内容（不含 +/- 前缀） */
  text: string
}

/** 本轮变更的一个文件 */
export interface ChangedFile {
  path: string
  /** A=新增 M=修改 D=删除 */
  change: 'A' | 'M' | 'D'
  added: number
  removed: number
  diff: DiffLine[]
  /** diff 正文被截断（后端只给前若干行） */
  diff_truncated?: boolean
}

/** 验证证据 */
export interface ValidationEvidence {
  /**
   * 命令证据层面的状态。
   *
   * 它**始终**是 guard 的镜像（前端不再自己按退出码推断结论）；
   * guard 缺失时恒为 'not_run'。
   */
  status: ValidationStatus
  /** 主证据命令（第一条）；没有命令时为 null */
  command: string | null
  /** 主证据输出；没有证据时为 null（UI 不得据此宣称通过） */
  output: string | null
  /** 说明文字 */
  note: string
  /**
   * 后端 guard 的权威判定。
   *
   * null = 这条记录里没有判定字段（旧的执行记录）→ 界面显示「未判定」，
   * 不得退回"自己按退出码算一个结论"。
   */
  guard: GuardEvidence | null
  /** 逐条验证命令及其结构化退出码 */
  entries: VerificationEntry[]
}

/** 过程事件（时间线条目） */
export interface TaskEvent {
  time: string
  title: string
  description: string
}

/** 一个会话上归属的一轮任务结果 */
export interface TaskResult {
  thread_id: string
  /** 头部副标题：run 归属说明 */
  run_label: string
  /** 任务标题 */
  title: string
  status: TaskStatus
  validation: ValidationEvidence
  files: ChangedFile[]
  events: TaskEvent[]
  /** 当前展示的 run_id（用于检测新 run 开始时清空旧结果） */
  run_id_marker: string | null
  /**
   * 最近一次**执行记录**的状态（来自持久化的 last_run_result）。
   *
   * 刷新后没有进行中的 Run（status=no_run），但历史执行结果仍然存在。
   * 这两个事实必须分开呈现：界面可以说"当前无进行中的运行"，
   * 但**不能**因此说成"从未执行过"。
   *
   * null = 没有任何执行记录。
   */
  last_result_status?: string | null
}

/** 任务结果数据来源：real=真实运行结果，mock=演示数据 */
export type TaskDataSource = 'real' | 'mock'

/** 任务结果数据源接口：生产路径用 realTaskAdapter，mockTaskAdapter 仅供测试 */
export interface TaskAdapter {
  /**
   * 数据来源标记。
   * UI 依据它决定是否标注"模拟数据" —— 不能与面板开合之类的 UI 状态挂钩：
   * 真实 harness 产出的文件与 diff 不是模拟数据。
   */
  dataSource: TaskDataSource
  getTask(threadId: string | null): TaskResult | null
}
