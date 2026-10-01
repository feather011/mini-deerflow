/**
 * 应用根组件
 *
 * 职责：
 * - 组合 React Router、QueryClient、ErrorBoundary
 * - 定义三栏工作台骨架
 * - 路由：
 *   /                  → 未选择 Thread
 *   /threads/:threadId → 已选择 Thread
 */
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ErrorBoundary } from './ErrorBoundary'
import { BackendStatusBar } from '../features/status'
import {
  ThreadSidebar,
  ThreadWorkspace,
} from '../features/threads'
import { FilePanel } from '../features/files'
import '../styles.css'

// 每个浏览器标签独立缓存，避免跨标签状态污染
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: false,
      refetchOnWindowFocus: false,
    },
  },
})

/**
 * Thread 主区域
 *
 * / → 未选择 Thread
 * /threads/:threadId → 已选择 Thread
 */
import { useParams } from 'react-router-dom'

function ThreadMain() {
  const { threadId } = useParams<{ threadId: string }>()
  return <ThreadWorkspace threadId={threadId ?? null} />
}

function WorkspacePage() {
  const { threadId } = useParams<{ threadId: string }>()

  return (
    <div className="workspace">
      {/* 顶部：状态栏 */}
      <div className="workspace-header">
        <BackendStatusBar />
      </div>

      {/* 左侧：Thread 列表（20B） */}
      <aside className="workspace-sidebar">
        <ThreadSidebar activeThreadId={threadId ?? null} />
      </aside>

      {/* 中间：Agent Chat（20C/20D） */}
      <main className="workspace-main">
        <ThreadMain />
      </main>

      {/* 右侧：Files / Artifacts（20E） */}
      <aside className="workspace-aux">
        <FilePanel />
      </aside>
    </div>
  )
}

export default function App() {
  return (
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <Routes>
            <Route path="/" element={<WorkspacePage />} />
            <Route path="/threads/:threadId" element={<WorkspacePage />} />
          </Routes>
        </BrowserRouter>
      </QueryClientProvider>
    </ErrorBoundary>
  )
}
