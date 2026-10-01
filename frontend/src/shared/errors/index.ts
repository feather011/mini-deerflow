/**
 * 统一错误出口（20F）
 *
 * 所有 Feature 的错误文案都从这里取，避免各 Feature 重复维护状态码映射。
 */
export {
  getErrorMessage,
  isSafeToDisplay,
} from './messages'
export type { ErrorContext, ErrorLike } from './messages'
