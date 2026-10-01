/**
 * 全局 React 错误边界
 *
 * 职责：
 * - 捕获子组件渲染阶段的未处理异常
 * - 展示稳定错误标题，不泄漏堆栈
 * - 提供重新加载入口
 * - 不替代 API 错误处理（API 错误通过 ApiError + useQuery 管理）
 */
import { Component, type ReactNode } from 'react'
import { isSafeToDisplay } from '../shared/errors'

interface Props {
  children: ReactNode
}

interface State {
  hasError: boolean
  message: string
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, message: '' }

  static getDerivedStateFromError(err: unknown): State {
    // 渲染异常没有 HTTP 状态码，包装成 status=0 交给统一映射处理。
    // 内部 message 只在通过安全过滤后才保留，否则一律丢弃，避免泄漏堆栈。
    const raw = err instanceof Error ? err.message : ''
    return {
      hasError: true,
      message: raw && isSafeToDisplay(raw) ? raw : '',
    }
  }

  override render(): ReactNode {
    if (this.state.hasError) {
      return (
        <div
          style={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            height: '100vh',
            fontFamily: 'system-ui, sans-serif',
            gap: '1rem',
          }}
        >
          <h2 style={{ fontSize: '1.25rem' }}>页面加载失败</h2>
          <p style={{ color: '#6b7280', fontSize: '0.875rem' }}>
            请尝试刷新页面
          </p>
          <button
            type="button"
            onClick={() => window.location.reload()}
            style={{
              padding: '0.5rem 1.5rem',
              border: '1px solid #d1d5db',
              borderRadius: '6px',
              cursor: 'pointer',
              background: 'white',
            }}
          >
            刷新页面
          </button>
          {import.meta.env.DEV && this.state.message && (
            <pre
              style={{
                marginTop: '1rem',
                padding: '1rem',
                background: '#f3f4f6',
                borderRadius: '6px',
                fontSize: '0.75rem',
                maxWidth: '600px',
                overflow: 'auto',
              }}
            >
              {this.state.message}
            </pre>
          )}
        </div>
      )
    }

    return this.props.children
  }
}
