/**
 * React 应用入口
 *
 * 职责：
 * - 挂载 #root 节点
 * - 不包含路由或 Provider 配置（由 App.tsx 负责）
 */
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './app/App'

const root = document.getElementById('root')
if (!root) {
  throw new Error('HTML 中未找到 #root 节点')
}

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
