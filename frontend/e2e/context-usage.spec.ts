/**
 * E2E：上下文用量指示器（圆环 + 浮层）
 *
 * 覆盖：
 * - 新会话未发送 → 「暂无统计」（区别于请求失败）
 * - 发送后流终态 → 圆环显示百分比，浮层含「输入预算」「估算」「最近一次发送」
 * - 分类来自后端真实 9 分区；压缩前后仅在后端可用时展示
 * - 键盘打开（Enter）与 Escape 关闭
 * - 切换会话不串数据（A 有统计、B 无统计）
 * - 桌面与窄屏布局：浮层完全落在视口内、不与输入框重叠裁切
 */
import { test, expect, waitBackendReady, BACKEND_URL } from './helpers'

const CALC_PROMPT = '计算 1+1'

/** 指示器圆环按钮（唯一一个带该无障碍名的按钮）。 */
function ring(target: Pick<import('@playwright/test').Page, 'getByRole'>) {
  return target.getByRole('button', { name: /上下文用量/ })
}

async function openPopup(page: import('@playwright/test').Page) {
  await ring(page).click()
  return page.getByRole('dialog', { name: '上下文用量详情' })
}

test.beforeEach(async ({ request }) => {
  await waitBackendReady(request)
})

test('未发送消息时显示「暂无统计」，非请求失败', async ({ page, console }) => {
  await page.goto('/')
  await page.getByLabel('新建会话').click()
  await expect(page).toHaveURL(/\/threads\//)

  const r = ring(page)
  await expect(r).toBeVisible()
  await expect(r).toContainText('—')
  await expect(r).toContainText('暂无统计')

  const popup = await openPopup(page)
  await expect(popup).toBeVisible()
  await expect(popup.getByText('暂无统计')).toBeVisible()
  // 必须与「请求失败」区分
  await expect(popup.getByText('请求失败')).toHaveCount(0)

  await console.assertClean()
})

test('端点返回 null 快照时前端显示「暂无统计」', async ({ page, request, console }) => {
  // 直接验证后端契约：未知 thread 返回 200 + snapshot=null（不是 404）
  const resp = await request.get(`${BACKEND_URL}/api/threads/no-such-thread/context/usage`)
  expect(resp.status()).toBe(200)
  expect(await resp.json()).toEqual({ snapshot: null })

  await page.goto('/')
  await page.getByLabel('新建会话').click()
  await expect(page).toHaveURL(/\/threads\//)
  await expect(ring(page)).toContainText('暂无统计')

  await console.assertClean()
})

test('发送消息后：流终态触发刷新，浮层显示真实统计', async ({ page, console }) => {
  await page.goto('/')
  await page.getByLabel('新建会话').click()
  await expect(page).toHaveURL(/\/threads\//)

  // 初始：暂无统计
  await expect(ring(page)).toContainText('暂无统计')

  // 发送 → 等流终态
  await page.getByRole('textbox').fill(CALC_PROMPT)
  await page.getByRole('button', { name: '发送消息' }).click()
  await expect(page.getByText('执行完成')).toBeVisible({ timeout: 30_000 })

  // 流终态后 refresh 已发生：圆环变为百分比 + 状态文字（不再是占位）
  await expect(ring(page)).toContainText('%', { timeout: 15_000 })
  await expect(ring(page)).not.toContainText('暂无统计')

  const popup = await openPopup(page)
  await expect(popup.getByText(/最近一次发送/)).toBeVisible()
  await expect(popup.getByText(/输入预算/)).toBeVisible()
  await expect(popup.getByText(/估算/)).toBeVisible()

  // 分类来自后端真实分区（至少能看到「近期对话」「当前输入」「工具描述」之一）
  const list = popup.getByRole('list', { name: '分区明细' })
  await expect(list).toBeVisible()
  const items = await list.getByRole('listitem').allTextContents()
  // 9 个真实分区全部出现——口径完整，0 值也不隐藏
  expect(items).toHaveLength(9)
  expect(
    items.some((t) => /近期对话|当前输入|工具描述|进行中工具/.test(t)),
  ).toBe(true)
  // 「系统提示」恒为 0（仓库无固定 system 段），但同样必须可见
  const systemRow = items.find((t) => t.includes('系统提示'))
  expect(systemRow).toBeTruthy()
  expect(systemRow).toContain('0')
  // 头部：参考图式的大号占用率 + 已使用/预算
  expect(popup.getByText(/已使用/)).toBeTruthy()
  expect(popup.getByText(/估算/)).toBeTruthy()

  // 未触发压缩 → 不显示「压缩前」
  expect(items.every((t) => t.length > 0)).toBe(true)
  await expect(popup.getByText(/压缩前/)).toHaveCount(0)

  await console.assertClean()
})

test('键盘：Enter 打开浮层，Escape 关闭并把焦点还给圆环', async ({ page, console }) => {
  await page.goto('/')
  await page.getByLabel('新建会话').click()
  await expect(page).toHaveURL(/\/threads\//)

  const r = ring(page)
  await r.focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('dialog', { name: '上下文用量详情' })).toBeVisible()

  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog')).toHaveCount(0)
  // 焦点回到圆环
  await expect(r).toBeFocused()

  // Space 同样可打开
  await page.keyboard.press(' ')
  await expect(page.getByRole('dialog', { name: '上下文用量详情' })).toBeVisible()

  await console.assertClean()
})

test('切换会话不串数据：A 有统计、B 仍为「暂无统计」', async ({ page, console }) => {
  // 会话 A：发送并得到统计
  await page.goto('/')
  await page.getByLabel('新建会话').click()
  await expect(page).toHaveURL(/\/threads\//)
  const threadA = new URL(page.url()).pathname.split('/').pop()

  await page.getByRole('textbox').fill(CALC_PROMPT)
  await page.getByRole('button', { name: '发送消息' }).click()
  await expect(page.getByText('执行完成')).toBeVisible({ timeout: 30_000 })
  await expect(ring(page)).toContainText('%', { timeout: 15_000 })

  // 会话 B：新建（从没发送过）。URL 变化必须被等待——否则读到的仍是 A。
  await page.getByLabel('新建会话').click()
  await expect
    .poll(() => new URL(page.url()).pathname.split('/').pop(), {
      message: '点击新建会话后 URL 未切换到新 thread',
    })
    .not.toBe(threadA)
  const threadB = new URL(page.url()).pathname.split('/').pop()
  expect(threadB).not.toBe(threadA)

  // B 必须是「暂无统计」，而不是沿用 A 的数据
  await expect(ring(page)).toContainText('暂无统计')
  const popupB = await openPopup(page)
  await expect(popupB.getByText('暂无统计')).toBeVisible()
  await expect(popupB.getByText(/输入预算/)).toHaveCount(0)
  await page.keyboard.press('Escape')

  // 切回 A：切换会立即拉取 A 自己的统计 → 重新出现百分比
  await page.goto(`/threads/${threadA}`)
  await expect(ring(page)).toContainText('%', { timeout: 15_000 })
  const popupA = await openPopup(page)
  await expect(popupA.getByText(/输入预算/)).toBeVisible()
  await expect(popupA.getByText('暂无统计')).toHaveCount(0)

  await console.assertClean()
})

test('桌面布局：圆环与浮层都在视口内且不重叠发送按钮', async ({ page, console }) => {
  await page.setViewportSize({ width: 1280, height: 800 })
  await page.goto('/')
  await page.getByLabel('新建会话').click()
  await expect(page).toHaveURL(/\/threads\//)

  const r = ring(page)
  await expect(r).toBeVisible()
  const ringBox = await r.boundingBox()
  expect(ringBox).not.toBeNull()
  const vp = page.viewportSize()!
  expect(ringBox!.x).toBeGreaterThanOrEqual(0)
  expect(ringBox!.x + ringBox!.width).toBeLessThanOrEqual(vp.width)
  expect(ringBox!.y + ringBox!.height).toBeLessThanOrEqual(vp.height)

  // 圆环与发送按钮不重叠
  const sendBox = await page.getByRole('button', { name: '发送消息' }).boundingBox()
  expect(sendBox).not.toBeNull()
  const overlaps =
    ringBox!.x < sendBox!.x + sendBox!.width &&
    sendBox!.x < ringBox!.x + ringBox!.width &&
    ringBox!.y < sendBox!.y + sendBox!.height &&
    sendBox!.y < ringBox!.y + ringBox!.height
  expect(overlaps, '圆环不应与发送按钮重叠').toBe(false)

  // 浮层完全在视口内，且不遮挡发送按钮
  const popup = await openPopup(page)
  const popBox = await popup.boundingBox()
  expect(popBox).not.toBeNull()
  expect(popBox!.x).toBeGreaterThanOrEqual(0)
  expect(popBox!.x + popBox!.width).toBeLessThanOrEqual(vp.width + 1)
  expect(popBox!.y).toBeGreaterThanOrEqual(0)
  expect(popBox!.y + popBox!.height).toBeLessThanOrEqual(vp.height + 1)

  const overlapsSend =
    popBox!.x < sendBox!.x + sendBox!.width &&
    sendBox!.x < popBox!.x + popBox!.width &&
    popBox!.y < sendBox!.y + sendBox!.height &&
    sendBox!.y < popBox!.y + popBox!.height
  expect(overlapsSend, '浮层不应遮挡发送按钮').toBe(false)

  await console.assertClean()
})

test('窄屏布局（375×667）：浮层不超出视口、不遮挡输入区、文本不横向溢出', async ({
  page,
  console,
}) => {
  // 注意：≤768px 时侧边栏 display:none（既有响应式设计），
  // 因此先在桌面宽度建立会话并拿到真实数据，再切窄屏检查布局——
  // 顺带覆盖「有数据时」的窄屏形态，比空态更有意义。
  await page.setViewportSize({ width: 1280, height: 800 })
  await page.goto('/')
  await page.getByLabel('新建会话').click()
  await expect(page).toHaveURL(/\/threads\//)
  await page.getByRole('textbox').fill(CALC_PROMPT)
  await page.getByRole('button', { name: '发送消息' }).click()
  await expect(page.getByText('执行完成')).toBeVisible({ timeout: 30_000 })
  await expect(ring(page)).toContainText('%', { timeout: 15_000 })

  // 切到窄屏
  await page.setViewportSize({ width: 375, height: 667 })

  const r = ring(page)
  await expect(r).toBeVisible()
  const ringBox = await r.boundingBox()
  expect(ringBox).not.toBeNull()
  expect(ringBox!.x).toBeGreaterThanOrEqual(0)
  expect(ringBox!.x + ringBox!.width).toBeLessThanOrEqual(375)

  const popup = await openPopup(page)
  const popBox = await popup.boundingBox()
  expect(popBox).not.toBeNull()
  // 水平方向完全在视口内
  expect(popBox!.x).toBeGreaterThanOrEqual(0)
  expect(popBox!.x + popBox!.width).toBeLessThanOrEqual(375 + 1)
  // 垂直方向完全在视口内（不被顶部/底部裁掉）
  expect(popBox!.y).toBeGreaterThanOrEqual(0)
  expect(popBox!.y + popBox!.height).toBeLessThanOrEqual(667 + 1)
  // 浮层在圆环上方（向上弹出），不遮挡圆环
  expect(popBox!.y + popBox!.height).toBeLessThanOrEqual(ringBox!.y + 1)
  // 浮层内容不横向溢出
  const overflow = await popup.evaluate((el) => el.scrollWidth - el.clientWidth)
  expect(overflow, '浮层内容不应横向溢出').toBeLessThanOrEqual(1)
  // 有数据时仍能看到核心信息
  await expect(popup.getByText(/输入预算/)).toBeVisible()
  await expect(popup.getByRole('list', { name: '分区明细' })).toBeVisible()

  await console.assertClean()
})
