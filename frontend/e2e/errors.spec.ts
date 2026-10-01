/**
 * E2E：错误展示与后端离线恢复（必测 8、9）
 */
import { test, expect, waitBackendReady, makeTempFile, BACKEND_URL } from './helpers'

test.beforeEach(async ({ request, page }) => {
  await waitBackendReady(request)
  await page.goto('/')
})

test('8a. 404：访问不存在的 Thread 显示稳定文案', async ({ page, console }) => {
  await page.goto('/threads/does-not-exist-12345')

  await expect(page.getByText('会话不存在或已被删除')).toBeVisible({ timeout: 15_000 })
  await expect(page.getByRole('button', { name: '返回会话列表' })).toBeVisible()

  await console.assertClean()
})

test('8b. 409：同名上传显示稳定文案与 Request ID', async ({ page, console }) => {
  const name = `e2e-conflict-${Date.now()}.txt`
  const filePath = makeTempFile(name, 'conflict\n')

  // 第一次上传成功
  await page.getByLabel('选择要上传的文件').setInputFiles(filePath)
  await page.getByRole('button', { name: '上传', exact: true }).click()
  await expect(page.getByText(name, { exact: true })).toBeVisible({ timeout: 15_000 })

  // 第二次同名上传 → 409
  await page.getByLabel('选择要上传的文件').setInputFiles(filePath)
  await page.getByRole('button', { name: '上传', exact: true }).click()

  const notice = page.getByTestId('error-notice')
  await expect(notice).toBeVisible({ timeout: 15_000 })
  await expect(notice).toContainText('同名文件已存在')
  // Request ID 必须展示，便于关联后端日志
  await expect(page.getByTestId('error-request-id')).toBeVisible()
  await expect(page.getByTestId('error-request-id')).toContainText('req-')

  // 失败后保留文件选择，便于重试
  await expect(page.getByText(new RegExp(`已选择：${name}`))).toBeVisible()

  // 清理
  const row = page.locator('li', { hasText: name })
  await row.getByRole('button', { name: '删除', exact: true }).click()
  await row.getByRole('button', { name: '确认删除', exact: true }).click()

  await console.assertClean()
})

test('8c. 413：超限上传显示稳定文案与 Request ID', async ({ page, console }) => {
  // E2E 后端把 max_upload_bytes 设为 1 MiB
  const bigName = `e2e-toobig-${Date.now()}.bin`
  const bigPath = makeTempFile(bigName, Buffer.alloc(2 * 1024 * 1024, 0x41))

  await page.getByLabel('选择要上传的文件').setInputFiles(bigPath)
  await page.getByRole('button', { name: '上传', exact: true }).click()

  const notice = page.getByTestId('error-notice')
  await expect(notice).toBeVisible({ timeout: 20_000 })
  await expect(notice).toContainText('文件超出大小限制')
  await expect(page.getByTestId('error-request-id')).toContainText('req-')

  await console.assertClean()
})

test('8d. 错误提示不泄漏内部信息（路径、异常类名、密钥）', async ({ page }) => {
  const name = `e2e-leak-${Date.now()}.txt`
  const filePath = makeTempFile(name, 'leak\n')

  // 先成功上传一次，制造 409（走 ErrorNotice 渲染路径）
  await page.getByLabel('选择要上传的文件').setInputFiles(filePath)
  await page.getByRole('button', { name: '上传', exact: true }).click()
  await expect(page.getByText(name, { exact: true })).toBeVisible({ timeout: 15_000 })

  await page.getByLabel('选择要上传的文件').setInputFiles(filePath)
  await page.getByRole('button', { name: '上传', exact: true }).click()

  const notice = page.getByTestId('error-notice')
  await expect(notice).toBeVisible({ timeout: 15_000 })

  const text = (await notice.innerText()).replace(/\s+/g, ' ')
  // 不得出现 traceback、绝对路径、Python 异常类名或密钥
  expect(text).not.toMatch(/Traceback \(most recent call last\)/i)
  expect(text).not.toMatch(/[A-Za-z]:\\/)
  expect(text).not.toMatch(/\/(home|root|usr|var|etc|tmp)\//)
  expect(text).not.toMatch(/\w*(Error|Exception)\s*:/)
  expect(text).not.toMatch(/sk-[A-Za-z0-9]{16,}/)

  // 清理
  const row = page.locator('li', { hasText: name })
  await row.getByRole('button', { name: '删除', exact: true }).click()
  await row.getByRole('button', { name: '确认删除', exact: true }).click()
})

test('9. 后端离线时显示离线状态，恢复后显示就绪', async ({ page }) => {
  // 拦截所有后端请求并返回网络错误，模拟离线
  await page.route(`${BACKEND_URL}/**`, (route) => route.abort('failed'))

  await page.goto('/', { waitUntil: 'domcontentloaded' })

  const statusBar = page.getByRole('status')
  await expect(statusBar).toContainText(/后端离线|后端异常/, { timeout: 20_000 })

  // 恢复：解除拦截并重试
  await page.unroute(`${BACKEND_URL}/**`)
  await page.getByRole('button', { name: '重试' }).click()

  await expect(statusBar).toContainText('就绪', { timeout: 20_000 })
})
