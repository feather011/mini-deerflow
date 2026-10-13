/**
 * 应用根组件
 *
 * 职责：
 * - 组合 React Router、QueryClient、ErrorBoundary
 * - 定义工作台骨架：顶栏（TopBar + 后端状态）+ 左导航 + 聊天 + 右面板
 * - 左侧栏支持「会话 / 项目」Tab 切换；窄屏为抽屉
 * - 右栏按需打开任务审查面板（TaskInspector），默认显示文件面板
 * - 成果库 / 设置对话框由侧栏 footer 回调打开
 * - 路由：
 *   /                  → 未选择 Thread
 *   /threads/:threadId → 已选择 Thread（可能属于项目）
 *   /projects/:projectId → 项目详情页（概览入口）
 */
import { BrowserRouter, Routes, Route, useParams, useNavigate } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useState } from 'react'
import { ErrorBoundary } from './ErrorBoundary'
import { TopBar } from './layout/TopBar'
import { BackendStatusBar } from '../features/status'
import { ThreadSidebar, ThreadWorkspace, useThread } from '../features/threads'
import { FilePanel } from '../features/files'
import { ProjectSidebar, ProjectDetail, ProjectFilePanel, useProjects } from '../features/projects'
import { TaskInspector } from '../features/inspector'
import { ArtifactsDialog } from '../features/artifacts'
import { SettingsDialog } from '../features/settings'
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
function ThreadMain() {
  const { threadId } = useParams<{ threadId: string }>()
  return <ThreadWorkspace threadId={threadId ?? null} />
}

type SidebarTab = 'threads' | 'projects'

/** 顶栏标题：优先显示项目名 + 会话首句；无会话时显示「新会话」。 */
function useTopTitle(threadId: string | null): string {
  const { data } = useThread(threadId)
  if (!data) return '新会话'

  // 如果有会话，优先显示会话首句
  const firstUser = data.messages.find((m) => m.role === 'user')
  const text = firstUser?.content ?? ''
  const truncated = text.length > 40 ? text.slice(0, 40) + '…' : text || '新会话'

  // 如果有项目信息，在标题前显示项目名
  if (data.project_id) {
    // 可以从项目列表中查找（这里简化处理）
    return truncated
  }

  return truncated
}

/** 获取当前会话所属的项目 ID */
function useCurrentProjectId(): string | null {
  const { data } = useThread(useParams<{ threadId: string }>().threadId ?? null)
  return data?.project_id ?? null
}

function WorkspacePage() {
  const { threadId } = useParams<{ threadId: string }>()
  const [activeTab, setActiveTab] = useState<SidebarTab>('threads')
  // 窄屏抽屉导航（桌面 CSS 下不影响布局）
  const [navOpen, setNavOpen] = useState(false)
  // 右栏任务审查面板（默认关 → 显示文件面板）
  const [panelOpen, setPanelOpen] = useState(false)
  const [artifactsOpen, setArtifactsOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)

  const title = useTopTitle(threadId ?? null)
  const projectId = useCurrentProjectId()

  return (
    <div className="workspace">
      {/* 顶部：顶栏 + 后端状态 */}
      <div className="workspace-header">
        <TopBar
          title={title}
          breadcrumb={threadId ? (projectId ? '项目会话' : '会话') : undefined}
          onOpenNav={() => setNavOpen(true)}
          onTogglePanel={() => setPanelOpen((v) => !v)}
          panelOpen={panelOpen}
          showDemoTag={panelOpen}
        />
        <BackendStatusBar />
      </div>

      {/* 窄屏抽屉遮罩（桌面无样式，见 styles.css 媒体查询） */}
      {navOpen && (
        <button
          type="button"
          className="workspace-shade"
          aria-label="关闭导航"
          onClick={() => setNavOpen(false)}
        />
      )}

      {/* 左侧：Tab 切换 + 内容 */}
      <aside
        className={`workspace-sidebar${navOpen ? ' workspace-sidebar-open' : ''}`}
        aria-label="会话与项目导航"
      >
        {/* Tab 切换 */}
        <div className="sidebar-tabs">
          <button
            type="button"
            className={`sidebar-tab ${activeTab === 'threads' ? 'sidebar-tab-active' : ''}`}
            onClick={() => setActiveTab('threads')}
          >
            会话
          </button>
          <button
            type="button"
            className={`sidebar-tab ${activeTab === 'projects' ? 'sidebar-tab-active' : ''}`}
            onClick={() => setActiveTab('projects')}
          >
            项目
          </button>
        </div>

        {/* Tab 内容 */}
        {activeTab === 'threads' ? (
          <ThreadSidebar
            activeThreadId={threadId ?? null}
            onOpenArtifacts={() => setArtifactsOpen(true)}
            onOpenSettings={() => setSettingsOpen(true)}
          />
        ) : (
          <ProjectSidebar
            activeThreadId={threadId}
            onCloseDrawer={() => setNavOpen(false)}
          />
        )}
      </aside>

      {/* 中间：Agent Chat（20C/20D） */}
      <main className="workspace-main">
        <ThreadMain />
      </main>

      {/* 右侧：任务审查（按需）/ 文件面板 */}
      <aside
        className={`workspace-aux${
          panelOpen ? '' : ' workspace-aux-mobile-hidden'
        }`}
        aria-label="任务与文件面板"
      >
        {panelOpen ? (
          <TaskInspector
            open
            onClose={() => setPanelOpen(false)}
            threadId={threadId ?? null}
          />
        ) : projectId ? (
          // 项目会话：显示项目工作区文件浏览
          <ProjectFilePanel projectId={projectId} />
        ) : (
          // 普通会话：显示上传/产物面板
          <FilePanel />
        )}
      </aside>

      {/* 对话框 */}
      <ArtifactsDialog
        open={artifactsOpen}
        onClose={() => setArtifactsOpen(false)}
      />
      <SettingsDialog
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
      />
    </div>
  )
}

// ---------------------------------------------------------------------------
// 项目详情页面（概览入口）
// ---------------------------------------------------------------------------

function ProjectDetailPage() {
  const { projectId } = useParams<{ projectId: string }>()
  const { data: projects, isLoading, isError } = useProjects()
  const navigate = useNavigate()

  if (isLoading) {
    return (
      <div className="workspace">
        <div className="workspace-main" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <span className="loading">加载中...</span>
        </div>
      </div>
    )
  }

  if (isError || !projects) {
    return (
      <div className="workspace">
        <div className="workspace-main" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <span className="error">加载项目失败</span>
        </div>
      </div>
    )
  }

  const project = projects.find(p => p.project_id === projectId)
  if (!project) {
    return (
      <div className="workspace">
        <div className="workspace-main" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <span className="error">项目不存在</span>
        </div>
      </div>
    )
  }

  return (
    <div className="workspace">
      <div className="workspace-header">
        <TopBar
          title={project.name}
          breadcrumb="项目详情"
          onOpenNav={() => {}}
          onTogglePanel={() => {}}
          panelOpen={false}
          showDemoTag={false}
        />
        <BackendStatusBar />
      </div>

      <aside className="workspace-sidebar">
        <div className="sidebar-tabs">
          <button
            type="button"
            className="sidebar-tab"
            onClick={() => navigate('/')}
          >
            会话
          </button>
          <button
            type="button"
            className="sidebar-tab sidebar-tab-active"
          >
            项目
          </button>
        </div>
        <ProjectSidebar
          activeThreadId={null}
          onCloseDrawer={() => {}}
        />
      </aside>

      <main className="workspace-main">
        <ProjectDetail project={project} />
      </main>

      <aside className="workspace-aux">
        <ProjectFilePanel projectId={project.project_id} />
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
            <Route path="/projects/:projectId" element={<ProjectDetailPage />} />
          </Routes>
        </BrowserRouter>
      </QueryClientProvider>
    </ErrorBoundary>
  )
}
