/**
 * 后端 Files / Artifacts 类型（20E）
 *
 * 与后端 schema（src/mini_deerflow/api/schemas/files.py）对齐：
 * - FileKind: file / directory
 * - FileMetadata: 单个条目元数据
 * - FileListResponse: 列表响应
 */
export type FileKind = 'file' | 'directory'

export interface FileMetadata {
  /** 相对路径（uploads/ 或 artifacts/ 根下） */
  path: string
  /** 文件名（含扩展名） */
  name: string
  /** 文件大小（字节），目录时通常为 0 */
  size: number
  /** 推断的 MIME 类型，可能为 null */
  media_type: string | null
  /** 条目类型 */
  kind: FileKind
}

export interface FileListResponse {
  entries: FileMetadata[]
  total: number
}
