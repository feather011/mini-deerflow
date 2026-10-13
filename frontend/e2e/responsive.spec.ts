/**
 * E2E：窄屏 workspace 网格布局（回归：网格行数与可见子元素错配导致塌陷）
 *
 * 背景（本次修复的缺陷）：
 *   `.workspace` 在 ≤768px 时把 sidebar / aux 设为 `display:none`，
 *   而隐藏元素**不参与 grid 布局**，实际只剩 header 与 main 两行。
 *   原先的 `grid-template-rows: auto auto 1fr auto` 是按 4 个子元素写的：
 *   main 落到第 2 行的 `auto`（高度由内容决定），第 3 行 `1fr` 是空行、
 *   白白吞掉剩余高度 → 聊天区塌陷。改为 `auto 1fr` 后 main 吃掉剩余高度。
 *
 * 关于断言强度（反向验证得出的结论）：
 *   塌陷在**空会话**时最明显——内容一多，auto 行的高度恰好填满视口，
 *   几何上看不出异常。因此「占满剩余高度」的主力断言跑在空会话状态，
 *   有消息的状态作为第二道验证。
 *   反向验证：把 CSS 改回 `auto auto 1fr auto` 后，本 spec 的窄屏用例全部失败。
 */
import { test, expect, waitBackendReady } from './helpers'

const CALC_PROMPT = '计算 1+1'
const NARROW = { width: 375, height: 667 }
const DESKTOP = { width: 1280, height: 800 }
type Viewport = { width: number; height: number }

/** workspace 的可见子元素（用于核对「确实只剩 header 与 main」这一前提）。 */
function visibleWorkspaceChildren(page: import('@playwright/test').Page) {
  return page.evaluate(() => {
    const ws = document.querySelector('.workspace')
    if (!ws) return [] as string[]
    return Array.from(ws.children)
      .filter((el) => (el as HTMLElement).offsetParent !== null)
      .map((el) => el.className.split(' ')[0])
  })
}

/**
 * 建一个会话（可选是否发消息），然后切到 `target` 视口。
 *
 * 必须先在桌面宽度操作：≤768px 时侧栏 `display:none`，
 * 「新建会话」按钮根本不可见（那正是本 spec 覆盖的既有响应式行为）。
 */
async function createThread(
  page: import('@playwright/test').Page,
  target: Viewport,
  opts: { send?: boolean } = {},
) {
  const { send = true } = opts
  await page.setViewportSize(DESKTOP)
  await page.goto('/')
  await page.getByLabel('新建会话').click()
  await expect(page).toHaveURL(/\/threads\//)
  if (send) {
    await page.getByRole('textbox').fill(CALC_PROMPT)
    await page.getByRole('button', { name: '发送消息' }).click()
    await expect(page.getByText('执行完成')).toBeVisible({ timeout: 30_000 })
  }
  await page.setViewportSize(target)
  await page.waitForTimeout(200)
}

/** 断言 main 吃掉 header 以下的全部剩余高度（塌陷时这里必失败）。 */
async function expectMainFillsRemaining(
  page: import('@playwright/test').Page,
  viewport: Viewport,
  scenario: string,
) {
  const header = await page.locator('.workspace-header').boundingBox()
  const main = await page.locator('.workspace-main').boundingBox()
  expect(header, `${scenario}: header 不可见`).not.toBeNull()
  expect(main, `${scenario}: main 不可见`).not.toBeNull()

  const headerBottom = header!.y + header!.height
  const mainBottom = main!.y + main!.height
  const remaining = viewport.height - headerBottom

  // 紧接 header 之后
  expect(main!.y, `${scenario}: main 应紧接 header`).toBeGreaterThanOrEqual(
    headerBottom - 1,
  )
  // 触底：塌陷时空行 1fr 会把 main 顶到半空，这条最直接
  expect(
    mainBottom,
    `${scenario}: main 底边 ${mainBottom} 应触达视口底 ${viewport.height}`,
  ).toBeGreaterThanOrEqual(viewport.height - 2)
  // 吃掉剩余高度的绝大部分
  expect(
    main!.height,
    `${scenario}: 聊天区高度 ${main!.height} 应接近剩余高度 ${Math.round(remaining)}`,
  ).toBeGreaterThan(remaining * 0.85)

  // main 自身不应因内容撑出内部滚动（塌陷时这条不成立也无妨，
  // 真正的滚动容器在消息列表，另测）
  const inner = await page.locator('.workspace-main').evaluate((el) => ({
    clientHeight: el.clientHeight,
    scrollHeight: el.scrollHeight,
  }))
  expect(
    inner.scrollHeight,
    `${scenario}: main 内部不应产生整页滚动`,
  ).toBeLessThanOrEqual(inner.clientHeight + 2)
}

/** 从任意一条消息向上找到真正的滚动容器（不依赖 CSS Modules 的 hash 类名）。 */
function findScrollContainer(page: import('@playwright/test').Page) {
  return page.evaluate(() => {
    const item = document.querySelector('[data-role]')
    if (!item) return null
    let node = item.parentElement as HTMLElement | null
    while (node) {
      const overflowY = getComputedStyle(node).overflowY
      if (overflowY === 'auto' || overflowY === 'scroll') {
        return {
          className: node.className,
          overflowY,
          clientHeight: node.clientHeight,
          scrollHeight: node.scrollHeight,
        }
      }
      node = node.parentElement
    }
    return null
  })
}

test.beforeEach(async ({ request }) => {
  await waitBackendReady(request)
})

test('窄屏：媒体查询下只剩 header 与 main 参与 grid 布局', async ({ page }) => {
  await createThread(page, NARROW)

  const visible = await visibleWorkspaceChildren(page)
  expect(visible).toEqual(['workspace-header', 'workspace-main'])
  // 核对实现前提：sidebar / aux 确实被隐藏
  expect(visible).not.toContain('workspace-sidebar')
  expect(visible).not.toContain('workspace-aux')

  // 该前提下 grid 行模板必须是两行（不是按 4 子元素写的 4 行）
  const rows = await page.evaluate(() => {
    const ws = document.querySelector('.workspace') as HTMLElement
    return getComputedStyle(ws).gridTemplateRows
  })
  expect(rows.split(' ').length, '窄屏下应只有两行').toBe(2)
})

test('窄屏：空会话下聊天区占满剩余高度、输入框贴底（塌陷最敏感的场景）', async ({
  page,
}) => {
  await createThread(page, NARROW, { send: false })
  await expectMainFillsRemaining(page, NARROW, '窄屏空会话')

  // 输入框贴在视口下半部——塌陷时它会跑到页面顶部
  const textarea = await page.getByRole('textbox').boundingBox()
  expect(textarea).not.toBeNull()
  expect(
    textarea!.y + textarea!.height,
    `空会话输入框底边 ${textarea!.y + textarea!.height} 应在视口下半部`,
  ).toBeGreaterThan(NARROW.height * 0.5)
  const sendBtn = await page.getByRole('button', { name: '发送消息' }).boundingBox()
  expect(sendBtn).not.toBeNull()
  expect(sendBtn!.y + sendBtn!.height).toBeLessThanOrEqual(NARROW.height)
})

test('窄屏：有消息时聊天区仍占满剩余高度', async ({ page }) => {
  await createThread(page, NARROW, { send: true })
  await expectMainFillsRemaining(page, NARROW, '窄屏有消息')
})

test('窄屏：输入框与发送按钮位于底部区域且都在视口内', async ({ page }) => {
  await createThread(page, NARROW, { send: true })

  const textarea = await page.getByRole('textbox').boundingBox()
  const sendBtn = await page.getByRole('button', { name: '发送消息' }).boundingBox()
  expect(textarea).not.toBeNull()
  expect(sendBtn).not.toBeNull()

  // 都在视口内
  expect(textarea!.y + textarea!.height).toBeLessThanOrEqual(NARROW.height)
  expect(sendBtn!.y + sendBtn!.height).toBeLessThanOrEqual(NARROW.height)
  expect(textarea!.x).toBeGreaterThanOrEqual(0)

  // 位于底部区域（塌陷时它们会跑到页面上半部）
  expect(
    textarea!.y + textarea!.height,
    `输入框底边 ${textarea!.y + textarea!.height} 应在视口下半部`,
  ).toBeGreaterThan(NARROW.height * 0.5)
  // 发送按钮在输入框下方
  expect(sendBtn!.y).toBeGreaterThanOrEqual(textarea!.y + textarea!.height - 1)

})

test('窄屏：消息区可滚动（滚动容器真实可用）', async ({ page }) => {
  await createThread(page, NARROW, { send: true })

  // 追加长消息把内容撑高，制造真实溢出
  const longText = '长消息 ' + '内容'.repeat(400)
  for (let i = 0; i < 2; i++) {
    await page.getByRole('textbox').fill(longText)
    await page.getByRole('button', { name: '发送消息' }).click()
    await expect(page.getByText('执行完成')).toBeVisible({ timeout: 30_000 })
  }

  const scroll = await findScrollContainer(page)
  expect(scroll, '未找到消息区的滚动容器').not.toBeNull()
  expect(['auto', 'scroll']).toContain(scroll!.overflowY)
  expect(
    scroll!.scrollHeight,
    '消息内容应超出可视高度以形成可滚动区',
  ).toBeGreaterThan(scroll!.clientHeight)

  // 且真的能滚动：写入 scrollTop 后应保持（塌陷/被裁时会是 0）
  const after = await page.evaluate(() => {
    const item = document.querySelector('[data-role]')!
    let node = item.parentElement as HTMLElement | null
    while (node) {
      const overflowY = getComputedStyle(node).overflowY
      if (overflowY === 'auto' || overflowY === 'scroll') {
        node.scrollTop = 9999
        return { top: node.scrollTop, max: node.scrollHeight - node.clientHeight }
      }
      node = node.parentElement
    }
    return { top: -1, max: -1 }
  })
  expect(after.top).toBeGreaterThan(0)
  expect(after.top).toBeLessThanOrEqual(after.max)
})

test('窄屏：上下文浮层与发送按钮可用', async ({ page, console }) => {
  await createThread(page, NARROW)

  const ring = page.getByRole('button', { name: /上下文用量/ })
  await expect(ring).toBeVisible()
  await ring.click()

  const popup = page.getByRole('dialog', { name: '上下文用量详情' })
  await expect(popup).toBeVisible()
  const popBox = await popup.boundingBox()
  expect(popBox).not.toBeNull()
  expect(popBox!.x).toBeGreaterThanOrEqual(0)
  expect(popBox!.x + popBox!.width).toBeLessThanOrEqual(NARROW.width + 1)
  expect(popBox!.y).toBeGreaterThanOrEqual(0)
  expect(popBox!.y + popBox!.height).toBeLessThanOrEqual(NARROW.height + 1)
  // 浮层里的统计内容完整可见
  await expect(popup.getByText(/输入预算/)).toBeVisible()

  await page.keyboard.press('Escape')
  await expect(popup).toHaveCount(0)

  // 发送按钮可实际发送
  await page.getByRole('textbox').fill(CALC_PROMPT)
  const sendBtn = page.getByRole('button', { name: '发送消息' })
  await expect(sendBtn).toBeEnabled()
  await sendBtn.click()
  await expect(page.getByText('执行完成')).toBeVisible({ timeout: 30_000 })

  await console.assertClean()
})

test('桌面布局保持不变：三栏 grid 与 main 占满剩余高度', async ({ page }) => {
  await createThread(page, DESKTOP)

  const visible = await visibleWorkspaceChildren(page)
  expect(visible).toEqual([
    'workspace-header',
    'workspace-sidebar',
    'workspace-main',
    'workspace-aux',
  ])

  const cols = await page.evaluate(
    () =>
      getComputedStyle(document.querySelector('.workspace') as HTMLElement)
        .gridTemplateColumns,
  )
  expect(cols.split(' ').length, '桌面应保持三栏').toBe(3)

  await expectMainFillsRemaining(page, DESKTOP, '桌面有消息')
})
