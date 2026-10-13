/**
 * Project TanStack Query 缓存 Key
 *
 * 项目创建/删除会让会话列表失效（会话带 project_id，且删项目会级联删会话），
 * 因此 hooks 里需要跨 feature 失效 threadKeys.list()。
 */
export const projectKeys = {
  all: ['projects'] as const,
  list: () => [...projectKeys.all, 'list'] as const,
  detail: (projectId: string) =>
    [...projectKeys.all, 'detail', projectId] as const,
  threads: (projectId: string) =>
    [...projectKeys.all, 'threads', projectId] as const,
}