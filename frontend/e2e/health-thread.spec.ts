/**
 * E2E：健康检查与 Thread 生命周期（必测 1、2）
 */
import { test, expect, waitBackendReady } from './helpers'

test.beforeEach(async ({ request }) => {
  await waitBackendReady(request)
})

test('1. health/ready 显示就绪', async ({ page, console }) => {
  await page.goto('/')

  // 状态栏 role="status"，就绪文案为「就绪」
  const statusBar = page.getByRole('status')
  await expect(statusBar).toBeVisible()
  await expect(statusBar).toContainText('就绪', { timeout: 15_000 })

  await console.assertClean()
})

test('2a. 创建 Thread 后 URL 变为 /threads/:id 且显示空态', async ({ page, console }) => {
  await page.goto('/')
  await expect(page.getByText('选择或创建一个会话')).toBeVisible()

  await page.getByLabel('新建会话').click()

  await expect(page).toHaveURL(/\/threads\/[A-Za-z0-9_-]+/)
  await expect(page.getByText('还没有消息，发送第一条消息开始对话')).toBeVisible()

  await console.assertClean()
})

test('2b. 刷新浏览器后仍停留在同一 Thread', async ({ page, console }) => {
  await page.goto('/')
  await page.getByLabel('新建会话').click()
  await expect(page).toHaveURL(/\/threads\/[A-Za-z0-9_-]+/)

  const urlBefore = page.url()
  await page.reload()

  // URL 是选择状态的唯一事实来源，刷新后应仍在同一 Thread
  await expect(page).toHaveURL(urlBefore)
  await expect(page.getByRole('textbox')).toBeVisible({ timeout: 15_000 })

  await console.assertClean()
})

test('2c. 切换 Thread：点击侧栏另一项即导航', async ({ page, console }) => {
  await page.goto('/')

  await page.getByLabel('新建会话').click()
  // 等导航真正完成再读 URL，否则 split 拿到 undefined
  await expect(page).toHaveURL(/\/threads\/[A-Za-z0-9_-]+/)
  const firstUrl = page.url()
  const firstId = firstUrl.split('/threads/')[1]!

  await page.getByLabel('新建会话').click()
  await expect.poll(() => page.url()).not.toBe(firstUrl)
  const secondUrl = page.url()
  expect(secondUrl).not.toBe(firstUrl)

  // 侧栏条目的 aria-label 是「会话 <id>」，当前激活项会追加「（当前）」后缀，
  // 且另一个按钮是「删除会话 <id>」——故用 ^…$ 锚定的正则精确匹配。
  const sidebarItem = (id: string) =>
    page.getByRole('button', { name: new RegExp(`^会话 ${id}(（当前）)?$`) })

  const secondId = secondUrl.split('/threads/')[1]!

  // 侧栏应同时存在两个会话
  await expect(sidebarItem(firstId)).toBeVisible()
  await expect(sidebarItem(secondId)).toBeVisible()

  // 点击第一个 → 回到它的 URL
  await sidebarItem(firstId).click()
  await expect(page).toHaveURL(firstUrl)

  await console.assertClean()
})

test('2d. 删除 Thread 需二次确认，确认后返回列表首页', async ({ page, console }) => {
  await page.goto('/')
  await page.getByLabel('新建会话').click()
  await expect(page).toHaveURL(/\/threads\//)
  const threadId = page.url().split('/threads/')[1]!

  // 第一次点击删除 → 出现二次确认
  await page.getByLabel(`删除会话 ${threadId}`).click()
  await expect(page.getByLabel('确认删除')).toBeVisible()
  await expect(page.getByLabel('取消删除')).toBeVisible()

  // 取消 → 不删除
  await page.getByLabel('取消删除').click()
  await expect(page.getByLabel(`删除会话 ${threadId}`)).toBeVisible()
  await expect(page).toHaveURL(new RegExp(`/threads/${threadId}$`))

  // 再次删除并确认 → 导航回首页
  await page.getByLabel(`删除会话 ${threadId}`).click()
  await page.getByLabel('确认删除').click()

  await expect(page).toHaveURL(/\/$/)
  await expect(page.getByText('选择或创建一个会话')).toBeVisible()

  await console.assertClean()
})
