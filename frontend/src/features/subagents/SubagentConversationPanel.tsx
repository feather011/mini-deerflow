/**
 * SubagentConversationPanel — 子 Agent 的会话窗口（右侧面板）
 *
 * 数据源：子代理的**隔离线程** `subagent-{task_id}`。
 * SubagentRunner 把子 Agent 的对话写进这个线程，主会话只是拿到结果摘要；
 * 这里按 ID 直读该线程，还原子 Agent 实际聊了什么。
 *
 * 说明：`subagent-` 是服务端的保留命名空间（列表里不出现），但**按 ID
 * 直读仍然开放** —— 调试与取证需要，子代理的会话窗口也依赖它。
 */
import { useQuery } from '@tanstack/react-query'
import { getThread } from '../threads/api'
import type { MessageResponse } from '../../shared/types/threads'
import styles from './SubagentConversationPanel.module.css'

export interface SubagentConversationPanelProps {
  /** 子任务 ID（sa-xxxxxxxxxxxx） */
  taskId: string
  /** 派发指令（标题用） */
  instruction?: string
  /** 返回上一层（关闭子任务会话，回到原右侧面板） */
  onBack: () => void
}

/** 子代理的隔离线程 ID */
function subagentThreadId(taskId: string): string {
  return `subagent-${taskId}`
}

/** 消息气泡的角色标签 */
const ROLE_LABEL: Record<MessageResponse['role'], string> = {
  system: '系统',
  user: '子任务指令',
  assistant: '子 Agent',
  tool: '工具',
}

export function SubagentConversationPanel({
  taskId,
  instruction,
  onBack,
}: SubagentConversationPanelProps) {
  const threadId = subagentThreadId(taskId)
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['subagent-thread', threadId],
    queryFn: ({ signal }) => getThread(threadId, signal),
  })

  return (
    <div className={styles.panel} data-testid="subagent-conversation">
      <header className={styles.header}>
        <button
          type="button"
          className={styles.back}
          onClick={onBack}
          aria-label="返回"
        >
          ←
        </button>
        <div className={styles.heading}>
          <div className={styles.title}>子 Agent 会话</div>
          <div className={styles.subtitle} title={instruction}>
            {instruction ?? taskId}
          </div>
        </div>
      </header>

      <div className={styles.body}>
        {isLoading ? <p className={styles.hint}>加载子任务会话…</p> : null}

        {isError ? (
          <p className={styles.error} data-testid="subagent-conversation-error">
            无法读取子任务会话：{error?.message ?? '未知错误'}
          </p>
        ) : null}

        {!isLoading && !isError && (data?.messages.length ?? 0) === 0 ? (
          <p className={styles.hint}>这个子任务还没有留下对话记录。</p>
        ) : null}

        {data?.messages.map((message, index) => (
          <div
            key={`${message.role}-${index}`}
            className={styles.message}
            data-role={message.role}
          >
            <div className={styles.role}>{ROLE_LABEL[message.role]}</div>
            <div className={styles.content}>{message.content}</div>
          </div>
        ))}
      </div>
    </div>
  )
}
