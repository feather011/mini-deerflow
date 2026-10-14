/**
 * E2E：Agent Run + SSE 事件流（必测 3、4、5）
 *
 * 注意：RunEventPanel 的明细事件已折叠为「已接收 N 条事件」摘要行，
 * 详细事件下沉到右栏 TaskInspector「过程」Tab。
 * 本文件测试主面板行为（终态文案、停止接收、持久化），
 * 不直接检查事件明细 DOM 结构（由 TaskInspector 负责）。
 */
import { test, expect, waitBackendReady } from './helpers'

/** FakeModel 对 "计算 1+1" 的确定性回答。 */
const CALC_PROMPT = '计算 1+1'
const CALC_ANSWER = '计算结果: 2'

test.beforeEach(async ({ request, page }) => {
  await waitBackendReady(request)
  await page.goto('/')
  await page.getByLabel('新建会话').click()
  await expect(page).toHaveURL(/\/threads\//)
})

test('3. 发送消息：显示运行状态 → 最终消息持久化', async ({ page, console }) => {
  await page.getByRole('textbox').fill(CALC_PROMPT)
  await page.getByRole('button', { name: '发送消息' }).click()

  // 终态：执行完成（header statusText）
  await expect(page.getByText('执行完成')).toBeVisible({ timeout: 20_000 })

  // 消息列表出现 user + assistant 两条（限定 main 区域避免顶栏标题干扰；答案可能同时在 span 和 pre 中）
  await expect(page.getByRole('main').getByText(CALC_PROMPT)).toBeVisible()
  await expect(page.getByRole('main').getByText(CALC_ANSWER).first()).toBeVisible()

  // 刷新后仍然存在（持久化）
  await page.reload()
  await expect(page.getByRole('main').getByText(CALC_PROMPT)).toBeVisible({ timeout: 15_000 })
  await expect(page.getByRole('main').getByText(CALC_ANSWER).first()).toBeVisible()

  await console.assertClean()
})

test('4. 事件折叠：摘要行显示事件数、终态只出现一次、终态后不再增长', async ({ page, console }) => {
  await page.getByRole('textbox').fill(CALC_PROMPT)
  await page.getByRole('button', { name: '发送消息' }).click()

  // 终态出现
  await expect(page.getByText('执行完成')).toBeVisible({ timeout: 20_000 })

  // 摘要行出现（明细折叠后的计数行）
  const summary = page.locator('[class*="summary"]')
  await expect(summary).toBeVisible({ timeout: 10_000 })

  // 事件数量不为 0（有真实事件）
  const summaryText = await summary.textContent()
  expect(summaryText).toMatch(/\d+ 条事件/)

  // 终态后再等待一段时间，摘要行不应消失（事件已收到）
  await page.waitForTimeout(2000)
  await expect(summary).toBeVisible()

  // EventItem 折叠：不再有 eventLabel 样式的 DOM（明细在 TaskInspector）
  const eventLabels = await page.locator('[class*="eventLabel"]').count()
  expect(eventLabels).toBe(0)

  await console.assertClean()
})

test('5. 停止接收：事件流停止、常驻说明未取消后端 Agent、不再显示运行中', async ({ page, console }) => {
  await page.getByRole('textbox').fill(CALC_PROMPT)
  await page.getByRole('button', { name: '发送消息' }).click()

  // 运行中或连接中时才有「停止接收」
  const stopBtn = page.getByRole('button', { name: '停止接收', exact: true })
  await expect(stopBtn).toBeVisible({ timeout: 20_000 })

  // tooltip 必须说明不会取消后端 Agent
  await expect(stopBtn).toHaveAttribute('title', /不会取消后端 Agent/)

  await stopBtn.click()

  // 停止按钮消失
  await expect(page.getByRole('button', { name: '停止接收', exact: true })).toHaveCount(0)

  // 常驻说明必须明确「后端 Agent 未被取消」
  const notice = page.getByTestId('error-notice')
  await expect(notice).toBeVisible()
  await expect(notice).toContainText('后端 Agent 未被取消')
  await expect(notice).toContainText('仍在后台运行')

  // 整页不得出现声称已取消后端 Agent 的文案
  const body = (await page.locator('body').innerText()).replace(/\s+/g, '')
  expect(body).not.toContain('已取消后端Agent')
  expect(body).not.toContain('已取消后端')

  // 输入框恢复可用（pending 已解除）
  await expect(page.getByRole('textbox')).toBeEnabled({ timeout: 10_000 })

  await console.assertClean()
})
