/**
 * fileErrors.ts 单元测试（20E）
 */
import { describe, it, expect } from 'vitest'
import { ApiError } from '../../shared/api/client'
import { getFileErrorMessage } from './fileErrors'

function makeErr(status: number, message = 'oops'): ApiError {
  return new ApiError({ status, code: 'X', message })
}

describe('getFileErrorMessage', () => {
  it('400 路径非法', () => {
    expect(getFileErrorMessage(makeErr(400))).toBe('路径非法或目标不是文件')
  })

  it('404 文件不存在', () => {
    expect(getFileErrorMessage(makeErr(404))).toBe('文件不存在')
  })

  it('409 同名文件已存在', () => {
    expect(getFileErrorMessage(makeErr(409))).toBe('同名文件已存在')
  })

  it('413 文件超出大小限制', () => {
    expect(getFileErrorMessage(makeErr(413))).toBe('文件超出大小限制')
  })

  it('500 文件服务异常', () => {
    expect(getFileErrorMessage(makeErr(500))).toBe('文件服务异常')
  })

  it('503 文件服务暂不可用', () => {
    expect(getFileErrorMessage(makeErr(503))).toBe('文件服务暂不可用')
  })

  it('0 网络错误', () => {
    expect(getFileErrorMessage(makeErr(0))).toBe('无法连接后端服务')
  })

  it('其他状态码使用 message', () => {
    expect(getFileErrorMessage(makeErr(418, '自定义错误'))).toBe('自定义错误')
  })

  it('其他状态码且无 message 使用默认文案', () => {
    expect(getFileErrorMessage(makeErr(418, ''))).toBe('操作失败，请稍后重试')
  })
})
