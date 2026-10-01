/**
 * features/files/api.ts 单元测试（20E）
 *
 * 覆盖：
 * - encodeFilePath：segment 编码再连接
 * - listUploads / listArtifacts：调用 GET
 * - uploadFile：FormData 请求且不手动设置 Content-Type
 * - downloadFile / downloadArtifact：Blob 响应
 * - deleteFile：DELETE 204
 * - 错误透传：ApiError 抛出
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import {
  encodeFilePath,
  listUploads,
  listArtifacts,
  uploadFile,
  downloadFile,
  downloadArtifact,
  deleteFile,
} from './api'

// mock 整个 shared/api 层（ApiError 是 class，需在 factory 内定义）
vi.mock('../../shared/api', () => {
  class MockApiError extends Error {
    status: number
    code: string
    requestId?: string
    details: unknown
    override message: string
    constructor(opts: {
      status: number; code: string; message: string
      requestId?: string; details?: unknown
    }) {
      super(opts.message)
      this.name = 'ApiError'
      this.status = opts.status
      this.code = opts.code
      this.message = opts.message
      this.requestId = opts.requestId
      this.details = opts.details ?? null
    }
  }
  return {
    apiGet: vi.fn(),
    apiPost: vi.fn(),
    apiDelete: vi.fn(),
    apiUpload: vi.fn(),
    apiDownloadBlob: vi.fn(),
    ApiError: MockApiError,
  }
})

// ApiError 是 class，必须从实现模块值导出（shared/api 只做 type 转出）
import { apiGet, apiUpload, apiDownloadBlob, apiDelete } from '../../shared/api'
import { ApiError } from '../../shared/api/client'

const mockApiGet = vi.mocked(apiGet)
const mockApiUpload = vi.mocked(apiUpload)
const mockApiDownloadBlob = vi.mocked(apiDownloadBlob)
const mockApiDelete = vi.mocked(apiDelete)

beforeEach(() => {
  vi.clearAllMocks()
})

// ---------------------------------------------------------------------------
// encodeFilePath
// ---------------------------------------------------------------------------

describe('encodeFilePath', () => {
  it('普通路径保持不变', () => {
    expect(encodeFilePath('subdir/file.txt')).toBe('subdir/file.txt')
  })

  it('包含空格的 segment 编码', () => {
    expect(encodeFilePath('sub dir/file name.txt')).toBe(
      'sub%20dir/file%20name.txt',
    )
  })

  it('包含 / 的多段路径正确编码', () => {
    expect(encodeFilePath('a/b/c.json')).toBe('a/b/c.json')
  })

  it('中文文件名编码', () => {
    expect(encodeFilePath('文件.txt')).toBe('%E6%96%87%E4%BB%B6.txt')
  })
})

// ---------------------------------------------------------------------------
// listUploads / listArtifacts
// ---------------------------------------------------------------------------

describe('listUploads', () => {
  it('调用 GET /api/files', async () => {
    mockApiGet.mockResolvedValueOnce({ entries: [], total: 0 })
    await listUploads()
    expect(mockApiGet).toHaveBeenCalledWith('/api/files', { signal: undefined })
  })

  it('传递 AbortSignal', async () => {
    mockApiGet.mockResolvedValueOnce({ entries: [], total: 0 })
    const ac = new AbortController()
    await listUploads(ac.signal)
    expect(mockApiGet).toHaveBeenCalledWith('/api/files', { signal: ac.signal })
  })
})

describe('listArtifacts', () => {
  it('调用 GET /api/artifacts', async () => {
    mockApiGet.mockResolvedValueOnce({ entries: [], total: 0 })
    await listArtifacts()
    expect(mockApiGet).toHaveBeenCalledWith('/api/artifacts', { signal: undefined })
  })
})

// ---------------------------------------------------------------------------
// uploadFile — FormData
// ---------------------------------------------------------------------------

describe('uploadFile', () => {
  it('使用 FormData 且不手动设置 Content-Type', async () => {
    mockApiUpload.mockResolvedValueOnce({
      path: 'uploads/test.txt',
      name: 'test.txt',
      size: 12,
      media_type: 'text/plain',
      kind: 'file',
    })

    const file = new File(['hello world!'], 'test.txt', { type: 'text/plain' })
    await uploadFile(file)

    expect(mockApiUpload).toHaveBeenCalledTimes(1)
    const [path, formData, opts] = mockApiUpload.mock.calls[0]!
    expect(path).toBe('/api/files')
    expect(formData).toBeInstanceOf(FormData)
    // 验证 FormData 含 file 字段
    const fileInFd = formData.get('file')
    expect(fileInFd).toBe(file)
    // 验证没有传 dest 参数（opts 没有 dest）
    expect(opts).toEqual({ signal: undefined })
  })

  it('传 dest 时加上 query string', async () => {
    mockApiUpload.mockResolvedValueOnce({
      path: 'uploads/sub/x.txt',
      name: 'x.txt',
      size: 0,
      media_type: 'text/plain',
      kind: 'file',
    })

    const file = new File([''], 'x.txt', { type: 'text/plain' })
    await uploadFile(file, 'sub/x.txt')

    const [path] = mockApiUpload.mock.calls[0]!
    expect(path).toBe('/api/files?dest=sub%2Fx.txt')
  })

  it('dest 中的特殊字符编码', async () => {
    mockApiUpload.mockResolvedValueOnce({
      path: 'uploads/sub/x.txt',
      name: 'x.txt',
      size: 0,
      media_type: 'text/plain',
      kind: 'file',
    })

    const file = new File([''], 'x.txt', { type: 'text/plain' })
    await uploadFile(file, 'sub dir/x.txt')
    const [path] = mockApiUpload.mock.calls[0]!
    expect(path).toBe('/api/files?dest=sub%20dir%2Fx.txt')
  })

  it('201 成功时返回 UploadResponse', async () => {
    const data = {
      path: 'uploads/a.txt',
      name: 'a.txt',
      size: 5,
      media_type: 'text/plain',
      kind: 'file' as const,
    }
    mockApiUpload.mockResolvedValueOnce(data)
    const file = new File(['hello'], 'a.txt', { type: 'text/plain' })
    const result = await uploadFile(file)
    expect(result).toEqual(data)
  })

  it('409 冲突抛出 ApiError', async () => {
    mockApiUpload.mockRejectedValueOnce(new ApiError({
      status: 409, code: 'CONFLICT', message: '同名文件已存在',
    }))
    const file = new File([''], 'dup.txt', { type: 'text/plain' })
    await expect(uploadFile(file)).rejects.toMatchObject({ status: 409 })
  })

  it('413 超限抛出 ApiError', async () => {
    mockApiUpload.mockRejectedValueOnce(new ApiError({
      status: 413, code: 'PAYLOAD_TOO_LARGE', message: '文件超出大小限制',
    }))
    const file = new File([''], 'big.bin', { type: 'application/octet-stream' })
    await expect(uploadFile(file)).rejects.toMatchObject({ status: 413 })
  })
})

// ---------------------------------------------------------------------------
// downloadFile / downloadArtifact — Blob
// ---------------------------------------------------------------------------

describe('downloadFile', () => {
  it('返回 Blob', async () => {
    const blob = new Blob(['data'])
    mockApiDownloadBlob.mockResolvedValueOnce(blob)
    const result = await downloadFile('a.txt')
    expect(result).toBe(blob)
    expect(mockApiDownloadBlob).toHaveBeenCalledWith('/api/files/a.txt', { signal: undefined })
  })

  it('路径含空格时正确编码', async () => {
    mockApiDownloadBlob.mockResolvedValueOnce(new Blob())
    await downloadFile('sub dir/a.txt')
    expect(mockApiDownloadBlob).toHaveBeenCalledWith(
      '/api/files/sub%20dir/a.txt',
      { signal: undefined },
    )
  })

  it('去掉 uploads/ 前缀（FileMetadata.path 总是带前缀）', async () => {
    mockApiDownloadBlob.mockResolvedValueOnce(new Blob())
    await downloadFile('uploads/a.txt')
    expect(mockApiDownloadBlob).toHaveBeenCalledWith(
      '/api/files/a.txt',
      { signal: undefined },
    )
  })

  it('uploads/ 前缀 + 子目录也正确处理', async () => {
    mockApiDownloadBlob.mockResolvedValueOnce(new Blob())
    await downloadFile('uploads/sub dir/a.txt')
    expect(mockApiDownloadBlob).toHaveBeenCalledWith(
      '/api/files/sub%20dir/a.txt',
      { signal: undefined },
    )
  })
})

describe('downloadArtifact', () => {
  it('调用 /api/artifacts/{path}', async () => {
    mockApiDownloadBlob.mockResolvedValueOnce(new Blob())
    await downloadArtifact('output.txt')
    expect(mockApiDownloadBlob).toHaveBeenCalledWith(
      '/api/artifacts/output.txt',
      { signal: undefined },
    )
  })

  it('去掉 artifacts/ 前缀（FileMetadata.path 总是带前缀）', async () => {
    mockApiDownloadBlob.mockResolvedValueOnce(new Blob())
    await downloadArtifact('artifacts/artifact-test.txt')
    expect(mockApiDownloadBlob).toHaveBeenCalledWith(
      '/api/artifacts/artifact-test.txt',
      { signal: undefined },
    )
  })

  it('artifacts/ 前缀 + 子目录也正确处理', async () => {
    mockApiDownloadBlob.mockResolvedValueOnce(new Blob())
    await downloadArtifact('artifacts/sub dir/a.txt')
    expect(mockApiDownloadBlob).toHaveBeenCalledWith(
      '/api/artifacts/sub%20dir/a.txt',
      { signal: undefined },
    )
  })
})

// ---------------------------------------------------------------------------
// deleteFile — 204
// ---------------------------------------------------------------------------

describe('deleteFile', () => {
  it('调用 DELETE /api/files/{path}', async () => {
    mockApiDelete.mockResolvedValueOnce(undefined)
    await deleteFile('a.txt')
    expect(mockApiDelete).toHaveBeenCalledWith('/api/files/a.txt', { signal: undefined })
  })

  it('删除后返回 undefined', async () => {
    mockApiDelete.mockResolvedValueOnce(undefined)
    const result = await deleteFile('a.txt')
    expect(result).toBeUndefined()
  })

  it('路径含空格正确编码', async () => {
    mockApiDelete.mockResolvedValueOnce(undefined)
    await deleteFile('sub dir/a.txt')
    expect(mockApiDelete).toHaveBeenCalledWith(
      '/api/files/sub%20dir/a.txt',
      { signal: undefined },
    )
  })

  it('去掉 uploads/ 前缀（FileMetadata.path 总是带前缀）', async () => {
    mockApiDelete.mockResolvedValueOnce(undefined)
    await deleteFile('uploads/a.txt')
    expect(mockApiDelete).toHaveBeenCalledWith(
      '/api/files/a.txt',
      { signal: undefined },
    )
  })
})
