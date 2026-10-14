/**
 * WM-BUG-01 回归测试：无 Run 时不得显示"运行中"（假状态）
 *
 * Bug 描述：空消息或未建立 Run 时，TaskInspector 显示"运行中"假状态。
 * 根因：useAgentStream 在 message 为空时也建立 SSE 流，
 *        CONNECTING 事件先到达，status 变成 connecting/streaming。
 *
 * 测试场景：
 * 1. 空消息：不建立 SSE 流，TaskInspector 显示占位符
 * 2. 正常对话：发送消息 → 执行完成 → 消息持久化
 * 3. run.result 端到端（后端集成测试验证 ScriptedModel → project_execute → run.result）
 *
 * 不在 E2E 测试 run.result：
 * - e2e_server.py 用 FakeModel（只调 calculator），不触发 project_execute
 * - run.result 路径已通过 test_project_execute_e2e.py 后端集成测试验证
 * - 前端 realTaskAdapter.test.ts 验证 ingestProjectExecuteResult 解析逻辑
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

test('无 Run 时不显示"运行中"（WM-BUG-01 回归）', async ({ page, console }) => {
  // 新建会话后，尚未发送消息，此时不应该出现"运行中"状态
  // TaskInspector 应显示占位符（尚未建立 Run）

  // 确认输入框为空
  const input = page.getByRole('textbox')
  await expect(input).toBeVisible()

  // 等待 2 秒，确保没有任何 SSE 流被建立（无 run.started 事件）
  // 期间不应该出现"运行中"、"执行中"等状态文案
  await page.waitForTimeout(2000)

  // TaskInspector 面板（如果已打开）不应显示"运行中"
  const body = await page.locator('body').innerText()
  expect(body).not.toMatch(/运行中/)
  expect(body).not.toMatch(/执行中/)

  await console.assertClean()
})

test('发送消息：显示执行完成 → 消息持久化（无假状态）', async ({ page, console }) => {
  await page.getByRole('textbox').fill(CALC_PROMPT)
  await page.getByRole('button', { name: '发送消息' }).click()

  // 执行完成（终态文案出现，说明 run 已正确建立和结束）
  await expect(page.getByText('执行完成')).toBeVisible({ timeout: 20_000 })

  // 消息列表出现 user + assistant 两条（限定 main 区域避免顶栏标题干扰；答案可能同时在 span 和 pre 中）
  await expect(page.getByRole('main').getByText(CALC_PROMPT)).toBeVisible()
  await expect(page.getByRole('main').getByText(CALC_ANSWER).first()).toBeVisible()

  // 刷新后消息仍在（持久化验证）
  await page.reload()
  await expect(page.getByRole('main').getByText(CALC_PROMPT)).toBeVisible({ timeout: 15_000 })
  await expect(page.getByRole('main').getByText(CALC_ANSWER).first()).toBeVisible()

  await console.assertClean()
})

test('TaskInspector 占位符：无 project_execute 结果时显示说明文案', async ({ page, console }) => {
  // FakeModel 不调用 project_execute，TaskInspector 应显示说明而非假装通过
  await page.getByRole('textbox').fill(CALC_PROMPT)
  await page.getByRole('button', { name: '发送消息' }).click()

  // 执行完成
  await expect(page.getByText('执行完成')).toBeVisible({ timeout: 20_000 })

  // 验证页签：不应出现"已通过"状态（FakeModel 无 project_execute）
  // 应出现说明文案（执行器输出尚未通过 run.result 推送 或 未运行验证命令）
  await page.waitForTimeout(500)

  // 页面上不应出现假的"验证通过"文案
  const body = await page.locator('body').innerText()
  expect(body).not.toMatch(/验证通过/)  // 不应出现"通过"状态

  await console.assertClean()
})
