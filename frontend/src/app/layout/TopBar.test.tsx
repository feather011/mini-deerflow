/**
 * TopBar 组件测试
 *
 * 覆盖：标题/面包屑渲染、窄屏菜单回调、任务面板开关回调与 aria-pressed、
 * Mock 标识按需显示、可选回调缺省时不渲染对应按钮。
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { TopBar } from './TopBar'

describe('TopBar', () => {
  it('渲染标题与面包屑', () => {
    render(<TopBar title="修复分页边界" breadcrumb="Mini DeerFlow / 修复分页边界" />)
    expect(screen.getByText('修复分页边界')).toBeInTheDocument()
    expect(screen.getByText('Mini DeerFlow / 修复分页边界')).toBeInTheDocument()
  })

  it('标题为空时显示占位', () => {
    render(<TopBar title="" />)
    expect(screen.getByText('Mini DeerFlow')).toBeInTheDocument()
  })

  it('onOpenNav 缺省时不渲染菜单按钮', () => {
    render(<TopBar title="t" />)
    expect(screen.queryByRole('button', { name: '打开导航' })).toBeNull()
  })

  it('菜单按钮点击触发回调', () => {
    const onOpenNav = vi.fn()
    render(<TopBar title="t" onOpenNav={onOpenNav} />)
    fireEvent.click(screen.getByRole('button', { name: '打开导航' }))
    expect(onOpenNav).toHaveBeenCalledTimes(1)
  })

  it('任务面板按钮切换文案与 aria-pressed', () => {
    const onTogglePanel = vi.fn()
    const { rerender } = render(
      <TopBar title="t" onTogglePanel={onTogglePanel} panelOpen={false} />,
    )
    const btn = screen.getByRole('button', { name: '查看任务' })
    expect(btn).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(btn)
    expect(onTogglePanel).toHaveBeenCalledTimes(1)

    rerender(<TopBar title="t" onTogglePanel={onTogglePanel} panelOpen={true} />)
    expect(screen.getByRole('button', { name: '收起任务' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })

  it('showDemoTag=false 时不渲染模拟数据标识', () => {
    render(<TopBar title="t" showDemoTag={false} />)
    expect(screen.queryByText('交互原型 · 模拟数据')).toBeNull()
  })

  it('showDemoTag=true 时渲染模拟数据标识', () => {
    render(<TopBar title="t" showDemoTag={true} />)
    expect(screen.getByText('交互原型 · 模拟数据')).toBeInTheDocument()
  })
})
