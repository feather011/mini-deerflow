/**
 * Markdown 渲染的行为用例
 *
 * 覆盖需求里点名的 5 条：
 * 1. assistant 消息含标题/列表/引用/表格/链接/代码块时正确展示
 * 2. 同样内容作为 user 消息时保留原文
 * 3. 分段追加 Markdown（流式）后最终正文完整
 * 4. 代码复制内容准确（复制原始代码，不含语言标记）
 * 5. 原始 HTML 不被执行、危险链接不变成可执行链接
 *
 * 全部经由真实入口 MessageList 渲染（不直接调内部函数）。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MessageList } from './MessageList'
import type { MessageResponse } from '../../shared/types/threads'

/** 一条包含全部受支持语法的 assistant 消息 */
const MARKDOWN = [
  '# 一级标题',
  '',
  '正文段落，含 **粗体** 与 `行内代码`。',
  '',
  '## 二级标题',
  '',
  '- 列表项一',
  '- 列表项二',
  '',
  '> 这是引用。',
  '',
  '| 列 A | 列 B |',
  '| --- | --- |',
  '| 值 1 | 值 2 |',
  '',
  '[示例链接](https://example.com)',
  '',
  '```python',
  'print("hello")',
  '```',
].join('\n')

const CODE = 'print("hello")'

describe('Markdown 渲染', () => {
  it('assistant 消息正确展示标题、列表、引用、表格、链接与代码块', () => {
    const messages: MessageResponse[] = [{ role: 'assistant', content: MARKDOWN }]
    render(<MessageList messages={messages} />)

    // 标题
    expect(screen.getByRole('heading', { level: 1, name: '一级标题' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: '二级标题' })).toBeInTheDocument()

    // 粗体与行内代码
    expect(screen.getByText('粗体').tagName).toBe('STRONG')
    expect(document.querySelector('code')?.textContent).toBe('行内代码')

    // 列表
    expect(screen.getAllByRole('listitem')).toHaveLength(2)
    expect(screen.getByText('列表项一')).toBeInTheDocument()

    // 引用
    expect(document.querySelector('blockquote')?.textContent?.trim()).toBe('这是引用。')

    // 表格
    const table = document.querySelector('table')
    expect(table).not.toBeNull()
    expect(table?.querySelectorAll('th')).toHaveLength(2)
    expect(table?.querySelectorAll('td')).toHaveLength(2)
    expect(screen.getByText('值 1')).toBeInTheDocument()

    // 链接
    const link = screen.getByRole('link', { name: '示例链接' })
    expect(link).toHaveAttribute('href', 'https://example.com')

    // 围栏代码块
    const code = document.querySelector('pre code')
    expect(code?.textContent?.trim()).toBe(CODE)
  })

  it('同样内容作为 user 消息时保留原文（不解析 Markdown）', () => {
    const messages: MessageResponse[] = [{ role: 'user', content: MARKDOWN }]
    render(<MessageList messages={messages} />)

    // 标记以字面文本出现
    expect(screen.getByText('# 一级标题')).toBeInTheDocument()
    expect(screen.getByText('| 列 A | 列 B |')).toBeInTheDocument()

    // 没有被解析成结构化元素
    expect(document.querySelector('h1')).toBeNull()
    expect(document.querySelector('h2')).toBeNull()
    expect(document.querySelector('ul')).toBeNull()
    expect(document.querySelector('blockquote')).toBeNull()
    expect(document.querySelector('table')).toBeNull()
    expect(document.querySelector('pre')).toBeNull()
  })

  it('分段追加 Markdown 后最终正文完整（流式）', () => {
    const { rerender } = render(<MessageList messages={[]} streamingText="# 一级标题" />)
    expect(screen.getByRole('heading', { level: 1, name: '一级标题' })).toBeInTheDocument()

    rerender(<MessageList messages={[]} streamingText={'# 一级标题\n\n正文段落'} />)
    expect(screen.getByText('正文段落')).toBeInTheDocument()

    rerender(<MessageList messages={[]} streamingText={MARKDOWN} />)
    expect(screen.getByRole('heading', { level: 1, name: '一级标题' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: '二级标题' })).toBeInTheDocument()
    // 段落里混排了粗体与行内代码，用 textContent 断言完整正文
    expect(document.querySelector('p')?.textContent).toBe('正文段落，含 粗体 与 行内代码。')
    expect(screen.getAllByRole('listitem')).toHaveLength(2)
    expect(document.querySelector('table')).not.toBeNull()
    expect(document.querySelector('pre code')?.textContent?.trim()).toBe(CODE)
  })

  it('流式内容中的代码围栏尚未闭合时不报错、不丢内容', () => {
    const messages: MessageResponse[] = [
      { role: 'assistant', content: '```python\nprint("partial")' },
    ]
    render(<MessageList messages={messages} />)
    expect(document.querySelector('pre code')?.textContent?.trim()).toBe('print("partial")')
  })

  it('代码复制写入原始代码并给出成功反馈', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    })

    const messages: MessageResponse[] = [{ role: 'assistant', content: MARKDOWN }]
    render(<MessageList messages={messages} />)

    await userEvent.click(screen.getByRole('button', { name: '复制' }))

    // 复制的是原始代码，不含语言标记
    expect(writeText).toHaveBeenCalledWith(CODE)
    expect(await screen.findByRole('button', { name: '已复制' })).toBeInTheDocument()
  })

  it('原始 HTML 不被执行，危险链接不变成可执行链接', () => {
    const messages: MessageResponse[] = [
      {
        role: 'assistant',
        content: [
          '<script>alert("xss")</script>',
          '',
          '<img src=x onerror="alert(1)">',
          '',
          '[点我](javascript:alert(1))',
        ].join('\n'),
      },
    ]
    render(<MessageList messages={messages} />)

    // 没有生成任何可执行元素
    expect(document.querySelector('script')).toBeNull()
    expect(document.querySelector('img')).toBeNull()

    // 原始 HTML 以纯文本呈现
    expect(document.body.textContent).toContain('<script>alert("xss")</script>')
    expect(document.body.textContent).toContain('<img src=x onerror="alert(1)">')

    // 危险协议的链接被清空，不会变成可执行链接
    const link = document.querySelector('a')
    expect(link).not.toBeNull()
    expect(link?.getAttribute('href')).toBe('')
  })
})

describe('复制反馈', () => {
  let originalClipboard: unknown

  beforeEach(() => {
    originalClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
  })

  afterEach(() => {
    if (originalClipboard) {
      Object.defineProperty(navigator, 'clipboard', originalClipboard)
    } else {
      // @ts-expect-error 删除测试注入的 clipboard
      delete navigator.clipboard
    }
  })

  it('剪贴板不可用时如实报告失败', async () => {
    Object.defineProperty(navigator, 'clipboard', {
      value: undefined,
      configurable: true,
    })

    const messages: MessageResponse[] = [{ role: 'assistant', content: MARKDOWN }]
    render(<MessageList messages={messages} />)

    await userEvent.click(screen.getByRole('button', { name: '复制' }))

    expect(await screen.findByRole('button', { name: '复制失败' })).toBeInTheDocument()
  })
})
