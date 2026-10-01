/**
 * E2E：Uploads 与 Artifacts（必测 6、7）
 *
 * 下载测试一律校验落盘文件内容，而不是只断言按钮被点击。
 */
import { test, expect, waitBackendReady, makeTempFile, readDownload, ARTIFACT_CONTENT, ARTIFACT_NAME } from './helpers'

const UPLOAD_TEXT = 'hello-from-20f-e2e\n'
const UPLOAD_NAME = `e2e-upload-${Date.now()}.txt`

test.beforeEach(async ({ request, page }) => {
  await waitBackendReady(request)
  await page.goto('/')
})

test('6a. 上传文件 → 列表出现 → 下载内容一致 → 删除后消失', async ({ page, console }) => {
  const filePath = makeTempFile(UPLOAD_NAME, UPLOAD_TEXT)

  // 上传
  await page.getByLabel('选择要上传的文件').setInputFiles(filePath)
  await page.getByRole('button', { name: '上传', exact: true }).click()

  // 列表出现
  await expect(page.getByText(UPLOAD_NAME, { exact: true })).toBeVisible({ timeout: 15_000 })

  // 下载并校验内容逐字节一致
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.getByRole('button', { name: '下载', exact: true }).first().click(),
  ])
  expect(download.suggestedFilename()).toBe(UPLOAD_NAME)
  const downloaded = readDownload(await download.path()!)
  expect(downloaded.toString('utf-8')).toBe(UPLOAD_TEXT)

  // 删除：先二次确认
  const row = page.locator('li', { hasText: UPLOAD_NAME })
  await row.getByRole('button', { name: '删除', exact: true }).click()
  await expect(row.getByRole('button', { name: '确认删除', exact: true })).toBeVisible()
  await row.getByRole('button', { name: '确认删除', exact: true }).click()

  // 列表消失
  await expect(page.getByText(UPLOAD_NAME, { exact: true })).toHaveCount(0, { timeout: 15_000 })

  await console.assertClean()
})

test('6b. 上传 pending 期间按钮锁定，不能重复提交', async ({ page, console }) => {
  const filePath = makeTempFile(`e2e-pending-${Date.now()}.txt`, 'x\n')

  // 延迟上传响应，人为制造可观察的 pending 窗口
  let releaseUpload: (() => void) | null = null
  const gate = new Promise<void>((resolve) => { releaseUpload = resolve })
  await page.route('**/api/files', async (route) => {
    if (route.request().method() === 'POST') {
      await gate
    }
    await route.continue()
  })

  await page.getByLabel('选择要上传的文件').setInputFiles(filePath)
  const uploadBtn = page.getByRole('button', { name: '上传', exact: true })
  await expect(uploadBtn).toBeEnabled()
  await uploadBtn.click()

  // pending：文案切换 + 按钮锁定 + 文件输入锁定
  const pendingBtn = page.getByRole('button', { name: '上传中…', exact: true })
  await expect(pendingBtn).toBeVisible()
  await expect(pendingBtn).toBeDisabled()
  await expect(page.getByLabel('选择要上传的文件')).toBeDisabled()

  // 重复点击不可能生效（按钮已 disabled）
  await pendingBtn.click({ force: true }).catch(() => {})

  // 释放响应 → pending 解除
  releaseUpload!()
  await expect(page.getByText(filePath.split(/[\\/]/).pop()!, { exact: true })).toBeVisible({ timeout: 15_000 })
  await expect(page.getByRole('button', { name: '上传', exact: true })).toBeVisible()

  await page.unroute('**/api/files')

  // 清理
  const name = filePath.split(/[\\/]/).pop()!
  const row = page.locator('li', { hasText: name })
  await row.getByRole('button', { name: '删除', exact: true }).click()
  await row.getByRole('button', { name: '确认删除', exact: true }).click()

  await console.assertClean()
})

test('7a. Artifacts 列表可见、可下载且内容一致', async ({ page, console }) => {
  await page.getByRole('tab', { name: 'Artifacts' }).click()

  const row = page.locator('li', { hasText: ARTIFACT_NAME })
  await expect(row).toBeVisible({ timeout: 15_000 })

  const [download] = await Promise.all([
    page.waitForEvent('download'),
    row.getByRole('button', { name: '下载', exact: true }).click(),
  ])
  const downloaded = readDownload(await download.path()!)
  expect(downloaded.toString('utf-8')).toBe(ARTIFACT_CONTENT)

  await console.assertClean()
})

test('7b. Artifacts 无上传与删除入口', async ({ page, console }) => {
  await page.getByRole('tab', { name: 'Artifacts' }).click()

  // 无上传组件
  await expect(page.getByRole('button', { name: '上传', exact: true })).toHaveCount(0)
  await expect(page.getByLabel('选择要上传的文件')).toHaveCount(0)

  // 无删除按钮
  await expect(page.getByRole('button', { name: '删除', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '确认删除', exact: true })).toHaveCount(0)

  await console.assertClean()
})

test('7c. Uploads 切换回 Artifacts 后缓存独立（artifacts 不受上传影响）', async ({ page, console }) => {
  // 在 Uploads 上传一个文件
  const filePath = makeTempFile(`e2e-iso-${Date.now()}.txt`, 'iso\n')
  await page.getByLabel('选择要上传的文件').setInputFiles(filePath)
  await page.getByRole('button', { name: '上传', exact: true }).click()

  const name = filePath.split(/[\\/]/).pop()!
  await expect(page.getByText(name, { exact: true })).toBeVisible({ timeout: 15_000 })

  // 切到 Artifacts：不应出现 uploads 的文件
  await page.getByRole('tab', { name: 'Artifacts' }).click()
  await expect(page.getByText(name, { exact: true })).toHaveCount(0)
  // 预置 artifact 仍在
  await expect(page.getByText(ARTIFACT_NAME, { exact: true })).toBeVisible()

  await console.assertClean()
})
