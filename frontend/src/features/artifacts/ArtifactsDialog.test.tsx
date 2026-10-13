/**
 * ArtifactsDialog 组件测试
 *
 * 覆盖：
 * - open=false 不渲染；关闭按钮 / 背景点击 / Escape → onClose
 * - 顶部「演示数据 · 未接入后端」标注
 * - 筛选 chips（全部 / 笔记 / 代码变更 / 文档），文档筛选命中空态
 * - 点击条目切换预览内容（pre 保留白空格）
 * - 导出按钮：选中后出现，点击触发 Blob + createObjectURL（纯前端，无请求）
 *
 * 说明：mock 掉 mockArtifacts 中的「文档」条目，用于覆盖筛选无结果的空态；
 * 其余数据仍是真实演示数据。
 */
import { beforeAll, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { ArtifactsDialog } from './ArtifactsDialog'

vi.mock('./mockArtifacts', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./mockArtifacts')>()
  return {
    ...actual,
    mockArtifacts: actual.mockArtifacts.filter((a) => a.type !== '文档'),
  }
})

// jsdom 未实现 URL.createObjectURL/revokeObjectURL，且 <a> 点击会触发导航告警
const downloads: string[] = []

beforeAll(() => {
  globalThis.URL.createObjectURL = vi.fn(() => 'blob:mock-url')
  globalThis.URL.revokeObjectURL = vi.fn()
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    downloads.push(this.download)
  })
})

describe('ArtifactsDialog — 打开与关闭', () => {
  it('open=false 时不渲染任何内容', () => {
    const { container } = render(
      <ArtifactsDialog open={false} onClose={vi.fn()} />,
    )
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(container.firstChild).toBeNull()
  })

  it('open=true 渲染标题与演示标注', () => {
    render(<ArtifactsDialog open onClose={vi.fn()} />)
    expect(
      screen.getByRole('dialog', { name: '成果库' }),
    ).toBeInTheDocument()
    expect(screen.getByText('演示数据 · 未接入后端')).toBeInTheDocument()
  })

  it('关闭按钮、背景点击、Escape 均触发 onClose', () => {
    const onClose = vi.fn()
    const { container } = render(<ArtifactsDialog open onClose={onClose} />)

    fireEvent.click(screen.getByRole('button', { name: '关闭' }))
    expect(onClose).toHaveBeenCalledTimes(1)

    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(2)

    // 背景（根节点）点击关闭
    fireEvent.click(container.firstElementChild as Element)
    expect(onClose).toHaveBeenCalledTimes(3)
  })
})

describe('ArtifactsDialog — 筛选', () => {
  it('默认「全部」展示所有条目，chips 可切换', () => {
    render(<ArtifactsDialog open onClose={vi.fn()} />)

    expect(
      screen.getByRole('button', { name: '全部', pressed: true }),
    ).toBeInTheDocument()
    // 测试数据已去掉「文档」条目，共 2 条
    expect(
      screen.getByRole('button', { name: /Agent 评测维度整理/ }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /分页边界修复记录/ }),
    ).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '笔记' }))
    expect(
      screen.getByRole('button', { name: '笔记', pressed: true }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /Agent 评测维度整理/ }),
    ).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: /分页边界修复记录/ }),
    ).not.toBeInTheDocument()
  })

  it('筛选无结果时显示清晰空态', () => {
    render(<ArtifactsDialog open onClose={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: '文档' }))
    expect(screen.getByText(/分类下暂无成果/)).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: /Agent 评测维度整理/ }),
    ).not.toBeInTheDocument()
    // 空态时不显示未选中提示
    expect(screen.queryByText('选择一条成果查看预览')).not.toBeInTheDocument()
  })
})

describe('ArtifactsDialog — 预览与导出', () => {
  it('未选中显示提示；点击条目切换预览内容', () => {
    render(<ArtifactsDialog open onClose={vi.fn()} />)

    expect(
      screen.queryByTestId('artifact-preview'),
    ).not.toBeInTheDocument()
    expect(screen.getByText('选择一条成果查看预览')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Agent 评测维度整理/ }))
    let preview = screen.getByTestId('artifact-preview')
    expect(preview.textContent).toContain('# Agent 评测维度整理')

    fireEvent.click(screen.getByRole('button', { name: /分页边界修复记录/ }))
    preview = screen.getByTestId('artifact-preview')
    expect(preview.textContent).toContain('示例代码变更')
    expect(preview.textContent).not.toContain('# Agent 评测维度整理')
    // 预览为 pre：代码缩进的连续空格原样保留
    expect(preview.tagName).toBe('PRE')
    expect(preview.textContent).toContain('    if page < 1')
  })

  it('选中后出现导出按钮，点击触发 Blob 导出（.md）', () => {
    render(<ArtifactsDialog open onClose={vi.fn()} />)

    expect(screen.queryByRole('button', { name: /导出/ })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Agent 评测维度整理/ }))
    const exportBtn = screen.getByRole('button', { name: /导出/ })
    downloads.length = 0
    const createSpy = vi.spyOn(URL, 'createObjectURL')
    fireEvent.click(exportBtn)

    expect(createSpy).toHaveBeenCalledTimes(1)
    expect(createSpy.mock.calls[0]![0]).toBeInstanceOf(Blob)
    expect(downloads).toEqual(['Agent 评测维度整理.md'])
  })

  it('代码变更条目导出为 .txt', () => {
    render(<ArtifactsDialog open onClose={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: /分页边界修复记录/ }))
    downloads.length = 0
    fireEvent.click(screen.getByRole('button', { name: /导出/ }))

    expect(downloads).toEqual(['分页边界修复记录.txt'])
  })
})
