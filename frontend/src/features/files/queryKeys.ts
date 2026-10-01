/**
 * Files / Artifacts 缓存 key（20E）
 *
 * 集中管理 cache key，避免拼写错误。
 */
export const fileKeys = {
  all: ['files'] as const,
  uploads: () => ['files', 'uploads'] as const,
  artifacts: () => ['files', 'artifacts'] as const,
}
