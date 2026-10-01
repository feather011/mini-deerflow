/**
 * runErrors.ts 单元测试
 *
 * 测试 getRunErrorMessage 对各 HTTP 状态码的错误文案映射
 */
import { describe, it, expect } from 'vitest'
import { getRunErrorMessage } from './runErrors'
import type { ApiError } from '../../shared/api/client'

describe('getRunErrorMessage', () => {
  const createError = (status: number, message = '后端原始错误'): ApiError =>
    ({
      name: 'ApiError',
      message,
      status,
      code: 'UNKNOWN',
      requestId: 'req-test-123',
      details: null,
    }) as ApiError

  const cases: Array<{ status: number; expected: string }> = [
    { status: 409, expected: '当前会话正在执行，请稍候' },
    { status: 422, expected: '消息内容不符合要求' },
    { status: 429, expected: '本次运行额度已耗尽' },
    { status: 500, expected: 'Agent 执行失败' },
    { status: 503, expected: 'Agent 服务暂不可用' },
    { status: 504, expected: 'Agent 执行超时' },
    { status: 0, expected: '无法连接后端服务' },
  ]

  describe.each(cases)('status=$status', ({ status, expected }) => {
    it(`返回 "${expected}"`, () => {
      expect(getRunErrorMessage(createError(status))).toBe(expected)
    })
  })

  it('未知状态码 fallback 到后端原始 message', () => {
    const error = createError(401, 'Unauthorized')
    expect(getRunErrorMessage(error)).toBe('Unauthorized')
  })

  it('未知状态码且无 message 时返回默认文案', () => {
    const error = createError(418, '')
    expect(getRunErrorMessage(error)).toBe('运行失败，请稍后重试')
  })

  it('忽略后端原始 message，前端依赖稳定的 HTTP 状态码', () => {
    // 无论后端返回什么文案，409 永远显示 "当前会话正在执行，请稍候"
    const error1 = createError(409, 'Thread is already running')
    const error2 = createError(409, '该线程当前有活跃的 Run')
    expect(getRunErrorMessage(error1)).toBe('当前会话正在执行，请稍候')
    expect(getRunErrorMessage(error2)).toBe('当前会话正在执行，请稍候')
  })
})
