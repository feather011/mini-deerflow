/**
 * 任务审查面板数据类型（WM-FE1 子任务 B）
 *
 * 语义对齐 前端大框架/FRONTEND_PLAN.md「状态必须如实呈现」：
 * - 任务状态与验证状态分开建模（执行完成 ≠ 业务验收通过）
 * - 没有命令证据（command/output 为 null）时，UI 不得显示测试通过
 */
/** 任务状态 */
export type TaskStatus =
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
}

/** 验证证据 */
export interface ValidationEvidence {
  status: ValidationStatus
  /** 实际执行的命令；未运行时为 null */
  command: string | null
  /** 命令输出摘要；没有证据时为 null（UI 不得据此宣称通过） */
  output: string | null
  /** 说明文字 */
  note: string
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
}

/** 任务结果数据源接口：当前由 mockTaskAdapter 实现，后续可替换为真实 API */
export interface TaskAdapter {
  getTask(threadId: string | null): TaskResult | null
}
