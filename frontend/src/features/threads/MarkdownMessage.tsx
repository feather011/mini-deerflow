/**
 * MarkdownMessage — assistant 消息的 Markdown 渲染组件
 *
 * 职责：
 * - 用 react-markdown + remark-gfm 渲染 Markdown（不自行编写解析器）
 * - 围栏代码块带复制按钮：复制**原始代码**（不含语言标记），成功/失败都有文字反馈
 * - 表格与长代码在自身容器内横向滚动，不撑破窄屏布局
 * - 原始 HTML 不执行：不启用 rehype-raw —— react-markdown 默认把原始 HTML
 *   当作纯文本输出；URL 走默认安全处理（javascript: 等协议会被清空）
 *
 * 首版不做：语法高亮、公式、Mermaid、代码运行按钮、自动折叠。
 */
import { Children, isValidElement, useCallback, useEffect, useState } from 'react'
import type { JSX, ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import type { Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import styles from './MarkdownMessage.module.css'

/** 复制按钮的状态 */
type CopyStatus = 'idle' | 'copied' | 'error'

/** 复制成功/失败的提示文字 */
const COPY_TEXT: Record<CopyStatus, string> = {
  idle: '复制',
  copied: '已复制',
  error: '复制失败',
}

/**
 * 从 React 节点里抽出纯文本。
 *
 * 代码块的 hast 结构是 `pre > code > text`，React 侧对应
 * `pre` 的 children 是 `code` 元素，`code` 的 children 是文本节点。
 * 这里递归取文本，供「复制原始代码」使用。
 */
function textOf(node: ReactNode): string {
  if (node === null || node === undefined || typeof node === 'boolean') return ''
  if (typeof node === 'string') return node
  if (typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  if (isValidElement(node)) {
    const props = node.props as { children?: ReactNode }
    return textOf(props.children)
  }
  return ''
}

/** 围栏代码块：头部（语言 + 复制按钮）+ 可横向滚动的代码区 */
function CodeBlock(props: JSX.IntrinsicElements['pre']) {
  const [status, setStatus] = useState<CopyStatus>('idle')

  // pre 的第一个子元素是 code 元素，语言标记在它的 className 上
  const codeEl = Children.toArray(props.children)[0]
  const codeProps = isValidElement(codeEl)
    ? (codeEl.props as { className?: string; children?: ReactNode })
    : undefined
  const className = codeProps?.className
  const language =
    typeof className === 'string' && className.startsWith('language-')
      ? className.slice('language-'.length)
      : ''
  // remark 给代码块内容的末尾补了一个换行（围栏最后一行行尾），
  // 复制时去掉它，得到的才是原始代码本身。
  const codeText = textOf(codeProps?.children).replace(/\n$/, '')

  const handleCopy = useCallback(async () => {
    // jsdom / 非安全上下文下没有 clipboard，一律如实报告失败
    const clipboard: Clipboard | undefined = navigator.clipboard
    if (!clipboard) {
      setStatus('error')
      return
    }
    try {
      await clipboard.writeText(codeText)
      setStatus('copied')
    } catch {
      setStatus('error')
    }
  }, [codeText])

  // 反馈显示一段时间后复位
  useEffect(() => {
    if (status === 'idle') return
    const timer = setTimeout(() => setStatus('idle'), 2000)
    return () => clearTimeout(timer)
  }, [status])

  return (
    <div className={styles.codeBlock} data-testid="markdown-code-block">
      <div className={styles.codeHeader}>
        <span className={styles.codeLang}>{language || 'text'}</span>
        <button
          type="button"
          className={styles.copyButton}
          onClick={() => {
            void handleCopy()
          }}
          title="复制代码"
          aria-live="polite"
        >
          {COPY_TEXT[status]}
        </button>
      </div>
      <pre className={styles.pre}>{props.children}</pre>
    </div>
  )
}

/** 表格：外面包一层横向滚动容器，避免宽表撑破窄屏 */
function TableBlock(props: JSX.IntrinsicElements['table']) {
  return (
    <div className={styles.tableWrap}>
      <table className={styles.table}>{props.children}</table>
    </div>
  )
}

/**
 * react-markdown 的组件映射（模块级常量，保持引用稳定）。
 *
 * 只覆盖 `pre`（代码块 + 复制按钮）与 `table`（横向滚动容器），
 * 其余元素用默认渲染。
 */
const components: Components = {
  pre: CodeBlock,
  table: TableBlock,
}

export interface MarkdownMessageProps {
  /** Markdown 原文 */
  content: string
}

/**
 * 渲染一段 Markdown。
 *
 * 流式内容、最终消息、刷新后恢复的历史消息都走这个组件，
 * 保证三种来源的排版完全一致。
 */
export function MarkdownMessage({ content }: MarkdownMessageProps) {
  return (
    <div className={styles.markdown} data-testid="markdown-body">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {content}
      </ReactMarkdown>
    </div>
  )
}
