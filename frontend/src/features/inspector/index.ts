export { TaskInspector, TASK_STATUS_META, VALIDATION_STATUS_META } from './TaskInspector'
export type { TaskInspectorProps, PillTone } from './TaskInspector'
// realTaskAdapter 是生产路径默认数据源；mock 适配器与 demo ID 仅保留给测试
export { realTaskAdapter, ingestRealTaskData, ingestProjectExecuteResult, clearRealTaskData } from './realTaskAdapter'
export { mockTaskAdapter, MOCK_TASK_IDS } from './mockTaskAdapter'
export type {
  TaskAdapter,
  TaskResult,
  TaskStatus,
  TaskEvent,
  ValidationEvidence,
  ValidationStatus,
  ChangedFile,
  DiffLine,
  DiffLineType,
} from './types'
