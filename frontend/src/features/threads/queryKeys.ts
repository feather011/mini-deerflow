/**
 * TanStack Query 缓存 Key
 *
 * URL 是当前 Thread 选择状态的唯一事实来源。
 * TanStack Query 是 Thread 服务端数据的事实来源。
 */
export const threadKeys = {
  all: ['threads'] as const,
  list: () => [...threadKeys.all, 'list'] as const,
  detail: (threadId: string) => [...threadKeys.all, 'detail', threadId] as const,
}
