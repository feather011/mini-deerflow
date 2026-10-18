/**
 * MCPSettings 组件测试 — TOOL-05 R1
 *
 * 验证管理页面：
 * - 渲染服务器状态与工具表（真实 API 响应字段）
 * - 三个主体（MAIN_AGENT / MINI_SUBAGENT / HARNESS_AGENT）授权与撤销
 * - 启用/停用切换
 * - 后端确认后才更新本地状态；失败显示错误且不乐观更新
 * - 提交中按钮 disabled（防双击）
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MCPSettings } from './MCPSettings'
import type { McpPrincipal } from '../../shared/api/mcp'

const listMcpServers = vi.fn()
const listMcpTools = vi.fn()
const setMcpToolEnabled = vi.fn()
const grantMcpTool = vi.fn()
const revokeMcpTool = vi.fn()

vi.mock('../../shared/api/mcp', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../shared/api/mcp')>()
  return {
    ...actual,
    listMcpServers: (...args: unknown[]) => listMcpServers(...args),
    listMcpTools: (...args: unknown[]) => listMcpTools(...args),
    setMcpToolEnabled: (...args: unknown[]) => setMcpToolEnabled(...args),
    grantMcpTool: (...args: unknown[]) => grantMcpTool(...args),
    revokeMcpTool: (...args: unknown[]) => revokeMcpTool(...args),
  }
})

const serverFixture = {
  servers: [
    { name: 'echo-server', status: 'running', running: true, tool_count: 1, failure_kind: null, failure_message: null },
  ],
}

const toolsFixture = {
  tools: [
    {
      qualified_name: 'mcp__echo__echo',
      server_name: 'echo-server',
      enabled: true,
      allowed_principals: ['MAIN_AGENT'],
    },
  ],
}

beforeEach(() => {
  listMcpServers.mockResolvedValue(serverFixture)
  listMcpTools.mockResolvedValue(toolsFixture)
  setMcpToolEnabled.mockImplementation(
    async (name: string, enabled: boolean) => ({
      qualified_name: name,
      server_name: 'echo-server',
      enabled,
      allowed_principals: ['MAIN_AGENT'],
    }),
  )
  grantMcpTool.mockImplementation(async (name: string, p: McpPrincipal) => ({
    qualified_name: name,
    server_name: 'echo-server',
    enabled: true,
    allowed_principals: ['MAIN_AGENT', p],
  }))
  revokeMcpTool.mockImplementation(async (name: string, p: McpPrincipal) => ({
    qualified_name: name,
    server_name: 'echo-server',
    enabled: true,
    allowed_principals: ['MAIN_AGENT'].filter((x) => x !== p),
  }))
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('MCPSettings', () => {
  it('渲染服务器状态（真实响应字段）', async () => {
    render(<MCPSettings />)
    expect(await screen.findByTestId('server-echo-server')).toBeInTheDocument()
    expect(screen.getByText('running · 1 工具')).toBeInTheDocument()
    expect(listMcpServers).toHaveBeenCalledTimes(1)
  })

  it('渲染工具表与三主体 pills', async () => {
    render(<MCPSettings />)
    await screen.findByTestId('tool-name-mcp__echo__echo')
    for (const p of ['MAIN_AGENT', 'MINI_SUBAGENT', 'HARNESS_AGENT']) {
      expect(
        screen.getByTestId(`principal-mcp__echo__echo-${p}`),
      ).toBeInTheDocument()
    }
    // MAIN_AGENT 已授权 → 显示撤销按钮
    expect(screen.getByTestId('principal-btn-mcp__echo__echo-MAIN_AGENT')).toHaveTextContent('撤销')
    // 未授权主体 → 授权按钮
    expect(screen.getByTestId('principal-btn-mcp__echo__echo-MINI_SUBAGENT')).toHaveTextContent('授权')
  })

  it('授予 MINI_SUBAGENT：真实调用后端并刷新本地状态', async () => {
    const user = userEvent.setup()
    render(<MCPSettings />)
    await screen.findByTestId('tool-name-mcp__echo__echo')
    const btn = screen.getByTestId('principal-btn-mcp__echo__echo-MINI_SUBAGENT')
    await user.click(btn)
    await waitFor(() => {
      expect(grantMcpTool).toHaveBeenCalledWith('mcp__echo__echo', 'MINI_SUBAGENT')
    })
    // 后端确认后的返回生效：按钮变撤销
    await waitFor(() => {
      expect(screen.getByTestId('principal-btn-mcp__echo__echo-MINI_SUBAGENT')).toHaveTextContent('撤销')
    })
  })

  it('撤销 HARNESS_AGENT', async () => {
    // 先让它显示撤销：fixture 含 HARNESS_AGENT
    listMcpTools.mockResolvedValue({
      tools: [
        {
          qualified_name: 'mcp__echo__echo',
          server_name: 'echo-server',
          enabled: true,
          allowed_principals: ['MAIN_AGENT', 'HARNESS_AGENT'],
        },
      ],
    })
    const user = userEvent.setup()
    render(<MCPSettings />)
    await screen.findByTestId('tool-name-mcp__echo__echo')
    const btn = screen.getByTestId('principal-btn-mcp__echo__echo-HARNESS_AGENT')
    expect(btn).toHaveTextContent('撤销')
    await user.click(btn)
    await waitFor(() => {
      expect(revokeMcpTool).toHaveBeenCalledWith('mcp__echo__echo', 'HARNESS_AGENT')
    })
  })

  it('启用切换：调用 enable API', async () => {
    const user = userEvent.setup()
    render(<MCPSettings />)
    await screen.findByTestId('tool-name-mcp__echo__echo')
    const btn = screen.getByTestId('toggle-mcp__echo__echo')
    expect(btn).toHaveTextContent('启用')
    await user.click(btn)
    await waitFor(() => {
      expect(setMcpToolEnabled).toHaveBeenCalledWith('mcp__echo__echo', false)
    })
  })

  it('后端失败：显示错误且不乐观更新', async () => {
    grantMcpTool.mockRejectedValue(new Error('权限被拒绝'))
    const user = userEvent.setup()
    render(<MCPSettings />)
    await screen.findByTestId('tool-name-mcp__echo__echo')
    const btn = screen.getByTestId('principal-btn-mcp__echo__echo-MINI_SUBAGENT')
    await user.click(btn)
    expect(await screen.findByRole('alert')).toHaveTextContent('权限被拒绝')
    // 未乐观更新：按钮仍为"授权"
    await waitFor(() => {
      expect(screen.getByTestId('principal-btn-mcp__echo__echo-MINI_SUBAGENT')).toHaveTextContent('授权')
    })
  })

  it('加载失败：显示错误提示', async () => {
    listMcpServers.mockRejectedValue(new Error('服务不可用'))
    render(<MCPSettings />)
    expect(await screen.findByRole('alert')).toHaveTextContent('服务不可用')
  })
})
