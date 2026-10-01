/**
 * E2E：Agent Run + SSE 事件流（必测 3、4、5）
 *
 * 这一组同时承担 20D 的浏览器验收：真实 SSE 连接、事件顺序、
 * 唯一终态、终态后停止、以及「停止接收」文案不含「已取消后端 Agent」。
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

  // 终态：执行完成
  await expect(page.getByText('执行完成')).toBeVisible({ timeout: 20_000 })

  // 消息列表出现 user + assistant 两条
  // 答案会同时出现在消息列表与事件面板的 answerBar 中，故用 first()
  await expect(page.getByText(CALC_PROMPT)).toBeVisible()
  await expect(page.getByText(CALC_ANSWER).first()).toBeVisible()

  // 刷新后仍然存在（持久化）
  await page.reload()
  await expect(page.getByText(CALC_PROMPT)).toBeVisible({ timeout: 15_000 })
  await expect(page.getByText(CALC_ANSWER).first()).toBeVisible()

  await console.assertClean()
})

test('4. SSE 事件顺序正确、只有一个终态、终态后不再增加', async ({ page, console }) => {
  await page.getByRole('textbox').fill(CALC_PROMPT)
  await page.getByRole('button', { name: '发送消息' }).click()

  // 首个事件必须是 Run 开始
  await expect(page.getByText('Run 开始')).toBeVisible({ timeout: 20_000 })

  // 终态出现
  await expect(page.getByText('执行完成')).toBeVisible({ timeout: 20_000 })

  // 唯一终态：终态事件标签只出现一次
  await expect(page.getByText('Run 完成', { exact: true })).toHaveCount(1)

  // 事件顺序：run.started 必须排在 model.started 之前
  const labels = await page.locator('[class*="eventLabel"]').allTextContents()
  const idxRunStarted = labels.indexOf('Run 开始')
  const idxModelStarted = labels.indexOf('模型开始')
  expect(idxRunStarted).toBeGreaterThanOrEqual(0)
  expect(idxModelStarted).toBeGreaterThan(idxRunStarted)

  // FakeModel 走 calculator 工具，因此必须出现工具事件
  expect(labels.some((l) => l.startsWith('工具'))).toBe(true)

  // 终态后再等待一段时间，事件数量不应继续增长（流已结束）
  const countAfterTerminal = labels.length
  await page.waitForTimeout(2000)
  const labelsLater = await page.locator('[class*="eventLabel"]').allTextContents()
  expect(labelsLater.length).toBe(countAfterTerminal)

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

  // 事件面板整体卸载：不再显示运行中，也不再显示停止按钮
  await expect(page.getByText('运行中')).toHaveCount(0)
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
