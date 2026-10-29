/**
 * E2E：聊天消息的 Markdown 渲染与排版
 *
 * 全部用**本地夹具**（page.route 拦截），不调用任何真实 Provider：
 * - `/api/threads/{id}` → 注入含 Markdown 的会话详情（模拟"刷新后的历史消息"）
 * - `/api/threads/{id}/runs/stream` → 注入 SSE 事件流（模拟流式到完成）
 *
 * 覆盖：
 * - 真实聊天入口里的 Markdown 排版（标题/列表/引用/表格/链接/代码块/复制按钮）
 * - user 消息保留原文（不被解析成 Markdown）
 * - 流式展示与流式到完成
 * - 刷新后的历史消息
 * - 桌面与窄屏都不产生横向撑破（表格/长代码在自身容器内横向滚动）
 */
import { test, expect } from './helpers'
import type { Page } from '@playwright/test'

const THREAD_ID = 'md-fixture'
const PROMPT = '给我一份解读报告'
/** user 消息里带 Markdown 标记：用来验证 user 侧仍按原文展示 */
const USER_MARKDOWN = '# 用户输入的标题'

const NARROW = { width: 375, height: 667 }
const DESKTOP = { width: 1280, height: 800 }

/** 含全部受支持语法的 assistant 消息（长代码行用于验证局部横向滚动） */
const MARKDOWN = [
  '# 项目解读报告',
  '',
  '正文段落，含 **粗体** 与 `行内代码`。',
  '',
  '## 关键结论',
  '',
  '- 第一条结论',
  '- 第二条结论',
  '',
  '> 引用：本次解读基于当前工作区代码。',
  '',
  '| 文件 | 变更 |',
  '| --- | --- |',
  '| harness_runner.py | 修复控制台弹窗 |',
  '| MessageList.tsx | 支持 Markdown 渲染 |',
  '',
  '[项目仓库](https://example.com/repo)',
  '',
  '```python',
  'def summarize(changes):',
  '    # 这一行故意写得很长很长很长很长很长很长很长，用来验证窄屏下代码块是局部横向滚动而不是把整页撑破',
  '    return changes',
  '```',
].join('\n')

/** 注入会话详情与列表（历史消息来源） */
async function installThreadFixture(
  page: Page,
  opts: { finalAnswer: boolean; userContent?: string },
) {
  const messages = [
    { role: 'user', content: opts.userContent ?? PROMPT },
    ...(opts.finalAnswer ? [{ role: 'assistant', content: MARKDOWN }] : []),
  ]
  await page.route(`**/api/threads/${THREAD_ID}`, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ thread_id: THREAD_ID, messages }),
    }),
  )
  await page.route('**/api/threads', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ threads: [{ thread_id: THREAD_ID }] }),
    }),
  )
}

/** 构造 SSE 事件流正文 */
function sseBody(events: { event_type: string; delta?: string }[]): string {
  return events
    .map((event, index) => {
      const payload = {
        run_id: 'run-fixture',
        thread_id: THREAD_ID,
        event_type: event.event_type,
        sequence: index + 1,
        timestamp: 1700000000 + index,
        answer: null,
        error: null,
        attempt: null,
        operation: null,
        tool_name: null,
        duration_seconds: null,
        ...(event.delta !== undefined ? { delta: event.delta } : {}),
      }
      return `id: ${index + 1}\nevent: ${event.event_type}\ndata: ${JSON.stringify(payload)}\n\n`
    })
    .join('')
}

/** 断言：页面与聊天区都没有横向溢出 */
async function expectNoHorizontalOverflow(page: Page, scenario: string) {
  const metrics = await page.evaluate(() => {
    const doc = document.documentElement
    const main = document.querySelector('.workspace-main') as HTMLElement | null
    return {
      docScroll: doc.scrollWidth,
      docClient: doc.clientWidth,
      mainScroll: main?.scrollWidth ?? 0,
      mainClient: main?.clientWidth ?? 0,
    }
  })
  expect(metrics.docScroll, `${scenario}: 页面出现横向滚动`).toBeLessThanOrEqual(
    metrics.docClient + 1,
  )
  expect(metrics.mainScroll, `${scenario}: 聊天区出现横向滚动`).toBeLessThanOrEqual(
    metrics.mainClient + 1,
  )
}

test.describe('Markdown 渲染', () => {
  test('桌面：历史消息正确渲染标题/列表/引用/表格/链接/代码块', async ({ page }) => {
    await page.setViewportSize(DESKTOP)
    await installThreadFixture(page, { finalAnswer: true, userContent: USER_MARKDOWN })
    await page.goto(`/threads/${THREAD_ID}`)

    // 标题
    await expect(page.getByRole('heading', { level: 1, name: '项目解读报告' })).toBeVisible()
    await expect(page.getByRole('heading', { level: 2, name: '关键结论' })).toBeVisible()

    // 列表
    await expect(page.getByText('第一条结论')).toBeVisible()
    await expect(page.getByText('第二条结论')).toBeVisible()

    // 引用
    await expect(page.locator('blockquote')).toBeVisible()

    // 表格
    await expect(page.locator('table')).toBeVisible()
    await expect(page.getByRole('cell', { name: 'harness_runner.py' })).toBeVisible()

    // 链接
    const link = page.getByRole('link', { name: '项目仓库' })
    await expect(link).toHaveAttribute('href', 'https://example.com/repo')

    // 代码块 + 复制按钮
    await expect(page.locator('pre code')).toBeVisible()
    await expect(page.getByRole('button', { name: '复制' })).toBeVisible()

    // user 消息仍按原文展示：标记没有被解析成标题
    await expect(page.locator('[data-role="user"] h1')).toHaveCount(0)
    await expect(page.locator('[data-role="user"]')).toContainText(USER_MARKDOWN)

    await expectNoHorizontalOverflow(page, '桌面')
  })

  test('刷新后历史消息仍是同样的 Markdown 排版', async ({ page }) => {
    await page.setViewportSize(DESKTOP)
    await installThreadFixture(page, { finalAnswer: true })
    await page.goto(`/threads/${THREAD_ID}`)
    await expect(page.getByRole('heading', { level: 1, name: '项目解读报告' })).toBeVisible()

    await page.reload()

    await expect(page.getByRole('heading', { level: 1, name: '项目解读报告' })).toBeVisible()
    await expect(page.locator('table')).toBeVisible()
    await expect(page.locator('pre code')).toBeVisible()
    await expectNoHorizontalOverflow(page, '刷新后')
  })

  test('窄屏：不产生横向撑破，长代码在代码块内横向滚动', async ({ page }) => {
    await page.setViewportSize(NARROW)
    await installThreadFixture(page, { finalAnswer: true })
    await page.goto(`/threads/${THREAD_ID}`)

    await expect(page.getByRole('heading', { level: 1, name: '项目解读报告' })).toBeVisible()
    await expectNoHorizontalOverflow(page, '窄屏')

    // 长代码确实是「局部横向滚动」：代码块自身可横向滚动
    const pre = await page.locator('pre').evaluate((el) => ({
      scrollWidth: el.scrollWidth,
      clientWidth: el.clientWidth,
    }))
    expect(pre.scrollWidth, '窄屏下长代码应超出代码块宽度').toBeGreaterThan(
      pre.clientWidth,
    )
  })

  test('流式：token 逐段到达时 Markdown 正确渲染', async ({ page }) => {
    await page.setViewportSize(DESKTOP)
    await installThreadFixture(page, { finalAnswer: false })
    // 分段推送 markdown，模拟真实的 token 流
    const chunks = MARKDOWN.match(/[^\n]*\n|[^\n]+/g) ?? []
    await page.route(`**/api/threads/${THREAD_ID}/runs/stream`, (route) =>
      route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        headers: { 'X-Run-ID': 'run-fixture' },
        body: sseBody(
          chunks.map((delta) => ({ event_type: 'model.token', delta })),
        ),
      }),
    )

    await page.goto(`/threads/${THREAD_ID}`)
    await page.getByRole('textbox').fill(PROMPT)
    await page.getByRole('button', { name: '发送消息' }).click()

    // 流式气泡里的 Markdown 正确渲染
    await expect(page.getByRole('heading', { level: 1, name: '项目解读报告' })).toBeVisible()
    await expect(page.getByText('第一条结论')).toBeVisible()
    await expect(page.locator('table')).toBeVisible()
    await expect(page.locator('pre code')).toBeVisible()
  })

  test('流式到完成：终态后展示最终消息', async ({ page }) => {
    await page.setViewportSize(DESKTOP)
    await installThreadFixture(page, { finalAnswer: true })
    await page.route(`**/api/threads/${THREAD_ID}/runs/stream`, (route) =>
      route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        headers: { 'X-Run-ID': 'run-fixture' },
        body: sseBody([
          { event_type: 'model.token', delta: '# 项目解读报告\n\n' },
          { event_type: 'model.token', delta: '- 第一条结论\n' },
          { event_type: 'run.completed' },
        ]),
      }),
    )

    await page.goto(`/threads/${THREAD_ID}`)
    await page.getByRole('textbox').fill(PROMPT)
    await page.getByRole('button', { name: '发送消息' }).click()

    // 终态：输入框恢复可写（submitStatus 复位）
    await expect(page.getByRole('textbox')).toBeEnabled({ timeout: 15_000 })

    // 最终消息（来自历史）渲染为 Markdown
    await expect(page.locator('table')).toBeVisible()
    await expect(page.getByRole('heading', { level: 1, name: '项目解读报告' })).toBeVisible()
    // 流式气泡已被终态清空，不与历史消息重复
    await expect(page.getByRole('heading', { level: 1, name: '项目解读报告' })).toHaveCount(1)
    await expectNoHorizontalOverflow(page, '流式完成后')
  })
})
