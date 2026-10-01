/**
 * File / Artifact 错误文案映射
 *
 * 20F 起委托给 shared/errors 的统一映射，保留本文件作为 files feature 的入口，
 * 避免破坏既有 import 路径与测试。
 */
import { getErrorMessage } from '../../shared/errors'
import type { ApiError } from '../../shared/api/client'

export function getFileErrorMessage(error: ApiError): string {
  return getErrorMessage(error, 'file')
}
