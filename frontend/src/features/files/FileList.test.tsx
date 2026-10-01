/**
 * FileList 组件测试（20E）
 *
 * 覆盖：
 * - 列表渲染
 * - Uploads 显示删除按钮 + 二次确认
 * - Artifacts 不显示删除按钮
 * - 下载触发（创建 Object URL 并 revoke）
 * - 目录禁用下载
 * - 取消删除
 * - 删除走 props.onDelete（不直接调用 hook）
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { userEvent } from '@testing-library/user-event'
import type { FileMetadata } from '../../shared/types/files'
import { FileList } from './FileList'

// mock api 中的下载
const mockDownloadFile = vi.fn()
const mockDownloadArtifact = vi.fn()

vi.mock('./api', () => ({
  downloadFile: (...args: unknown[]) => mockDownloadFile(...args),
  downloadArtifact: (...args: unknown[]) => mockDownloadArtifact(...args),
  encodeFilePath: (p: string) => p,
}))

beforeEach(() => {
  vi.clearAllMocks()
  // jsdom 默认未实现 URL.createObjectURL/revokeObjectURL
  if (!globalThis.URL.createObjectURL) {
    globalThis.URL.createObjectURL = vi.fn(() => 'blob:mock-url')
  }
  if (!globalThis.URL.revokeObjectURL) {
    globalThis.URL.revokeObjectURL = vi.fn()
  }
})

const sampleFile: FileMetadata = {
  path: 'uploads/hello.txt',
  name: 'hello.txt',
  size: 2048,
  media_type: 'text/plain',
  kind: 'file',
}

const sampleDir: FileMetadata = {
  path: 'uploads/subdir',
  name: 'subdir',
  size: 0,
  media_type: null,
  kind: 'directory',
}

describe('FileList — uploads scope', () => {
  it('渲染文件名、路径、大小、MIME、类型', () => {
    render(<FileList entries={[sampleFile]} scope="uploads" />)
    expect(screen.getByText('hello.txt')).toBeInTheDocument()
    expect(screen.getByText('uploads/hello.txt')).toBeInTheDocument()
    expect(screen.getByText('2.0 KB')).toBeInTheDocument()
    expect(screen.getByText('text/plain')).toBeInTheDocument()
    expect(screen.getByText('file')).toBeInTheDocument()
  })

  it('显示下载按钮', () => {
    render(<FileList entries={[sampleFile]} scope="uploads" />)
    expect(screen.getByRole('button', { name: '下载' })).toBeInTheDocument()
  })

  it('显示删除按钮', () => {
    render(<FileList entries={[sampleFile]} scope="uploads" />)
    expect(screen.getByRole('button', { name: '删除' })).toBeInTheDocument()
  })

  it('点击下载触发 downloadFile 并创建 Object URL', async () => {
    const user = userEvent.setup()
    const blob = new Blob(['hello'])
    mockDownloadFile.mockResolvedValueOnce(blob)
    const createSpy = vi.spyOn(URL, 'createObjectURL')
    const revokeSpy = vi.spyOn(URL, 'revokeObjectURL')

    render(<FileList entries={[sampleFile]} scope="uploads" />)
    await user.click(screen.getByRole('button', { name: '下载' }))

    expect(mockDownloadFile).toHaveBeenCalledWith('uploads/hello.txt')
    expect(createSpy).toHaveBeenCalledWith(blob)
    // revoke 必须被调用，且参数是 createObjectURL 返回的 URL
    expect(revokeSpy).toHaveBeenCalledTimes(1)
    expect(revokeSpy.mock.calls[0]![0]).toBe(createSpy.mock.results[0]!.value as unknown as string)
  })

  it('目录禁用下载和删除', () => {
    render(<FileList entries={[sampleDir]} scope="uploads" />)
    expect(screen.getByRole('button', { name: '目录不可下载' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '删除' })).toBeDisabled()
  })

  it('删除需要二次确认', async () => {
    const user = userEvent.setup()
    const onDelete = vi.fn().mockResolvedValueOnce(undefined)

    render(
      <FileList
        entries={[sampleFile]}
        scope="uploads"
        onDelete={onDelete}
      />,
    )

    // 第一次点击：出现确认/取消
    await user.click(screen.getByRole('button', { name: '删除' }))
    expect(screen.getByRole('button', { name: '确认删除' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '取消' })).toBeInTheDocument()
    expect(onDelete).not.toHaveBeenCalled()

    // 取消：回到初次状态
    await user.click(screen.getByRole('button', { name: '取消' }))
    expect(screen.queryByRole('button', { name: '确认删除' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '删除' })).toBeInTheDocument()

    // 再次点击 + 确认
    await user.click(screen.getByRole('button', { name: '删除' }))
    await user.click(screen.getByRole('button', { name: '确认删除' }))
    expect(onDelete).toHaveBeenCalledWith('uploads/hello.txt')
  })

  it('删除失败时调用 onError 并保留条目', async () => {
    const user = userEvent.setup()
    const onError = vi.fn()
    const onDelete = vi.fn().mockRejectedValueOnce(new Error('boom'))

    render(
      <FileList
        entries={[sampleFile]}
        scope="uploads"
        onError={onError}
        onDelete={onDelete}
      />,
    )
    await user.click(screen.getByRole('button', { name: '删除' }))
    await user.click(screen.getByRole('button', { name: '确认删除' }))

    expect(onError).toHaveBeenCalled()
    expect(screen.getByText('hello.txt')).toBeInTheDocument()
  })

  it('未提供 onDelete 时删除按钮仍然显示但确认无效', async () => {
    const user = userEvent.setup()
    // 不传 onDelete
    render(<FileList entries={[sampleFile]} scope="uploads" />)

    await user.click(screen.getByRole('button', { name: '删除' }))
    await user.click(screen.getByRole('button', { name: '确认删除' }))

    // 没有 onDelete，handleDelete 直接 return，不报错也不调用
    expect(screen.queryByRole('button', { name: '删除' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '确认删除' })).toBeInTheDocument()
  })
})

describe('FileList — artifacts scope', () => {
  it('只显示下载按钮，不显示删除按钮', () => {
    const artifact: FileMetadata = {
      path: 'output.txt',
      name: 'output.txt',
      size: 1024,
      media_type: 'text/plain',
      kind: 'file',
    }
    render(<FileList entries={[artifact]} scope="artifacts" />)
    expect(screen.getByRole('button', { name: '下载' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '删除' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '确认删除' })).not.toBeInTheDocument()
  })

  it('下载 artifact 调用 downloadArtifact', async () => {
    const user = userEvent.setup()
    const blob = new Blob(['artifact'])
    mockDownloadArtifact.mockResolvedValueOnce(blob)

    const artifact: FileMetadata = {
      path: 'output.txt',
      name: 'output.txt',
      size: 8,
      media_type: 'text/plain',
      kind: 'file',
    }
    render(<FileList entries={[artifact]} scope="artifacts" />)
    await user.click(screen.getByRole('button', { name: '下载' }))
    expect(mockDownloadArtifact).toHaveBeenCalledWith('output.txt')
    expect(mockDownloadFile).not.toHaveBeenCalled()
  })
})