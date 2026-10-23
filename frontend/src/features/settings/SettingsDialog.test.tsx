/**
 * SettingsDialog 组件测试
 *
 * 覆盖：
 * - open=false 不渲染；关闭按钮 / 背景点击 / Escape → onClose
 * - 四个分组页签渲染与切换（默认「模型」）
 * - 开关只改变本地状态（不发请求）
 * - 下拉只改变本地状态
 * - 底部固定提示文案
 * - 「保存」只弹本地提示，不发请求、不暗示已保存到后端
 */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { SettingsDialog } from './SettingsDialog'

describe('SettingsDialog — 打开与关闭', () => {
  it('open=false 时不渲染任何内容', () => {
    const { container } = render(
      <SettingsDialog open={false} onClose={vi.fn()} />,
    )
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(container.firstChild).toBeNull()
  })

  it('关闭按钮、背景点击、Escape 均触发 onClose', () => {
    const onClose = vi.fn()
    const { container } = render(<SettingsDialog open onClose={onClose} />)

    fireEvent.click(screen.getByRole('button', { name: '关闭' }))
    expect(onClose).toHaveBeenCalledTimes(1)

    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(2)

    fireEvent.click(container.firstElementChild as Element)
    expect(onClose).toHaveBeenCalledTimes(3)
  })
})

describe('SettingsDialog — 页签与表单', () => {
  it('渲染四个分组页签，默认选中「模型」，底部有固定提示', () => {
    render(<SettingsDialog open onClose={vi.fn()} />)

    expect(screen.getByRole('dialog', { name: '设置' })).toBeInTheDocument()
    expect(
      screen.getByRole('tab', { name: '模型', selected: true }),
    ).toBeInTheDocument()
    for (const name of ['上下文与记忆', '工具与MCP', '权限与日志']) {
      expect(screen.getByRole('tab', { name })).toBeInTheDocument()
    }
    expect(
      screen.getByText(
        '原型设置：仅影响本页演示状态，不保存、不验证连接',
      ),
    ).toBeInTheDocument()
    expect(screen.getByLabelText('主 Agent 模型')).toBeInTheDocument()
  })

  it('切换页签显示对应表单行', () => {
    render(<SettingsDialog open onClose={vi.fn()} />)

    fireEvent.click(screen.getByRole('tab', { name: '上下文与记忆' }))
    expect(
      screen.getByRole('tab', { name: '上下文与记忆', selected: true }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('checkbox', { name: /启用历史压缩/ }),
    ).toBeInTheDocument()
    expect(screen.queryByLabelText('主 Agent 模型')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: '权限与日志' }))
    expect(screen.getByLabelText('日志级别')).toBeInTheDocument()
    expect(
      screen.getByRole('checkbox', { name: /限制在绑定工作区/ }),
    ).toBeInTheDocument()
  })

  it('开关只改变本地状态，不发任何请求', () => {
    vi.stubGlobal('fetch', vi.fn())
    render(<SettingsDialog open onClose={vi.fn()} />)

    fireEvent.click(screen.getByRole('tab', { name: '上下文与记忆' }))
    const box = screen.getByRole('checkbox', { name: /启用历史压缩/ })
    expect(box).toBeChecked()

    fireEvent.click(box)
    expect(box).not.toBeChecked()

    // 其他开关互不影响 — 工具与MCP 页签显示 MCP 管理组件
    fireEvent.click(screen.getByRole('tab', { name: '工具与MCP' }))
    // MCPSettings 真实组件：调用后端 listMcpServers / listMcpTools
    vi.unstubAllGlobals()
  })

  it('下拉只改变本地状态', () => {
    render(<SettingsDialog open onClose={vi.fn()} />)

    const select = screen.getByLabelText('主 Agent 模型') as HTMLSelectElement
    expect(select.value).toBe('默认模型')

    fireEvent.change(select, { target: { value: '高质量模型（示例）' } })
    expect(select.value).toBe('高质量模型（示例）')
  })

  it('「保存」只显示本地提示，不暗示已保存到后端', () => {
    vi.stubGlobal('fetch', vi.fn())
    render(<SettingsDialog open onClose={vi.fn()} />)

    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '保存' }))

    const toast = screen.getByRole('status')
    expect(toast).toHaveTextContent('演示设置已确认 · 未保存到后端')
    expect(toast).toHaveTextContent('未保存到后端')
    expect(globalThis.fetch).not.toHaveBeenCalled()
    vi.unstubAllGlobals()
  })
})
