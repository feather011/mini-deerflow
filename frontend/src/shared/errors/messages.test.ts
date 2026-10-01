/**
 * 统一错误文案映射测试（20F）
 */
import { describe, it, expect } from 'vitest'
import { getErrorMessage, isSafeToDisplay } from './messages'

describe('getErrorMessage — 默认文案', () => {
  it('网络错误（status=0）', () => {
    expect(getErrorMessage({ status: 0 })).toBe('无法连接后端服务')
  })

  it('400 / 404 / 409 / 413', () => {
    expect(getErrorMessage({ status: 400 })).toBe('请求内容不符合要求')
    expect(getErrorMessage({ status: 404 })).toBe('请求的资源不存在')
    expect(getErrorMessage({ status: 409 })).toBe('操作与当前状态冲突')
    expect(getErrorMessage({ status: 413 })).toBe('内容超出大小限制')
  })

  it('422 / 429 / 500 / 503 / 504', () => {
    expect(getErrorMessage({ status: 422 })).toBe('请求参数校验失败')
    expect(getErrorMessage({ status: 429 })).toBe('请求过于频繁，请稍候')
    expect(getErrorMessage({ status: 500 })).toBe('服务内部错误')
    expect(getErrorMessage({ status: 503 })).toBe('服务暂不可用')
    expect(getErrorMessage({ status: 504 })).toBe('服务响应超时')
  })
})

describe('getErrorMessage — 上下文覆盖', () => {
  it('run 上下文', () => {
    expect(getErrorMessage({ status: 409 }, 'run')).toBe('当前会话正在执行，请稍候')
    expect(getErrorMessage({ status: 422 }, 'run')).toBe('消息内容不符合要求')
    expect(getErrorMessage({ status: 429 }, 'run')).toBe('本次运行额度已耗尽')
    expect(getErrorMessage({ status: 504 }, 'run')).toBe('Agent 执行超时')
  })

  it('file 上下文', () => {
    expect(getErrorMessage({ status: 400 }, 'file')).toBe('路径非法或目标不是文件')
    expect(getErrorMessage({ status: 404 }, 'file')).toBe('文件不存在')
    expect(getErrorMessage({ status: 409 }, 'file')).toBe('同名文件已存在')
    expect(getErrorMessage({ status: 413 }, 'file')).toBe('文件超出大小限制')
  })

  it('thread 上下文', () => {
    expect(getErrorMessage({ status: 404 }, 'thread')).toBe('会话不存在或已被删除')
  })

  it('health 上下文', () => {
    expect(getErrorMessage({ status: 503 }, 'health')).toBe('后端尚未就绪')
  })

  it('上下文未覆盖的状态码回落到默认文案', () => {
    // run 上下文没有定义 400
    expect(getErrorMessage({ status: 400 }, 'run')).toBe('请求内容不符合要求')
  })
})

describe('getErrorMessage — 未知状态码', () => {
  it('后端文案安全时直接使用', () => {
    expect(getErrorMessage({ status: 418, message: '自定义提示' })).toBe('自定义提示')
  })

  it('后端文案含内部信息时改用上下文兜底', () => {
    expect(
      getErrorMessage({ status: 418, message: 'ValueError: bad input' }, 'file'),
    ).toBe('操作失败，请稍后重试')
  })

  it('无 message 时使用上下文兜底', () => {
    expect(getErrorMessage({ status: 418 })).toBe('操作失败，请稍后重试')
    expect(getErrorMessage({ status: 418 }, 'thread')).toBe('加载失败，请稍后重试')
  })
})

describe('isSafeToDisplay', () => {
  it('放行普通中文文案', () => {
    expect(isSafeToDisplay('文件已存在')).toBe(true)
  })

  it('拦截 Python traceback', () => {
    expect(isSafeToDisplay('Traceback (most recent call last): ...')).toBe(false)
  })

  it('拦截异常类名 + 冒号', () => {
    expect(isSafeToDisplay('KeyError: missing')).toBe(false)
    expect(isSafeToDisplay('RuntimeError: boom')).toBe(false)
  })

  it('拦截 Windows 绝对路径', () => {
    expect(isSafeToDisplay('失败于 C:\\Users\\x\\file.txt')).toBe(false)
  })

  it('拦截 POSIX 绝对路径', () => {
    expect(isSafeToDisplay('失败于 /home/user/workspace/a.txt')).toBe(false)
    expect(isSafeToDisplay('失败于 /tmp/mdf_ws_x/uploads/a.txt')).toBe(false)
  })

  it('拦截疑似密钥', () => {
    expect(isSafeToDisplay('sk-abcdefghijklmnopqrstuvwxyz')).toBe(false)
    expect(isSafeToDisplay('api_key=SECRET123')).toBe(false)
  })

  it('放行不含敏感信息的英文短句', () => {
    expect(isSafeToDisplay('Thread not found')).toBe(true)
  })
})
