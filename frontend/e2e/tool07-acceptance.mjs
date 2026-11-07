/**
 * TOOL-07 浏览器人工验收驱动脚本（一次性辅助，非生产代码）。
 *
 * 复用已在运行的两个服务：
 *   - 后端 http://127.0.0.1:8123（scripts/tool07_acceptance_server.py，假 LLM，不联网不计费）
 *   - 前端 http://localhost:5173（vite dev，/api 代理到 8123）
 *
 * 走完四件事并截图：派发 / 进度 / 停止 / 结果。
 * 用法：node e2e/tool07-acceptance.mjs
 */
import { chromium } from '@playwright/test'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const BASE = process.env.ACCEPT_URL ?? 'http://localhost:5173'
const OUT = process.env.ACCEPT_OUT ?? join(process.cwd(), '..', 'tool07-acceptance-shots')
mkdirSync(OUT, { recursive: true })

let n = 0
async function shot(page, label) {
  n += 1
  const file = join(OUT, `${String(n).padStart(2, '0')}-${label}.png`)
  await page.screenshot({ path: file, fullPage: true })
  console.log(`   [截图] ${file}`)
}

async function bodyText(page) {
  return (await page.locator('body').innerText()).replace(/\n{3,}/g, '\n\n')
}

const errors = []

async function main() {
  const browser = await chromium.launch()
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } })
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(`[console] ${m.text()}`)
  })
  page.on('pageerror', (e) => errors.push(`[pageerror] ${e.message}`))

  const PROJECT_NAME = 'TOOL-07 浏览器验收'

  console.log('== 打开应用 ==')
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(1500)

  // ------------------------------------------------------------------
  // 0) 建项目 + 项目会话
  // ------------------------------------------------------------------
  console.log('== 切到「项目」页签并新建项目 ==')
  await page.getByRole('button', { name: '项目', exact: true }).first().click()
  await page.waitForTimeout(500)
  await page.getByLabel('新建项目').click()
  await page.locator('#project-name').fill(PROJECT_NAME)
  await page.getByRole('button', { name: '创建', exact: true }).click()
  await page.waitForTimeout(1500)

  console.log('== 在「' + PROJECT_NAME + '」项目内新建会话 ==')
  const nameBtn = page.getByRole('button', { name: PROJECT_NAME, exact: true }).last()
  const row = nameBtn.locator('xpath=../..')
  await row.getByTitle('新建会话').first().click()
  await page.waitForTimeout(1500)
  const threadUrl = page.url()
  console.log('   项目会话 URL:', threadUrl)
  await shot(page, '项目会话已创建')

  // ------------------------------------------------------------------
  // 1) 派发：讨论 → 只读；编码 → 派发 harness
  // ------------------------------------------------------------------
  console.log('\n== 1a. 派发：普通会话的只读讨论 ==')
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(1000)
  await page.getByLabel('新建会话').click()
  await page.waitForTimeout(1000)
  await page.getByRole('textbox').fill('这个仓库是做什么的？先聊聊，不要改任何文件。')
  await page.getByRole('button', { name: '发送消息' }).click()
  await page.waitForTimeout(5000)
  console.log('--- 页面文本 ---')
  console.log(await bodyText(page))
  await shot(page, '派发-只读讨论')

  console.log('\n== 1b. 派发：项目会话的明确编码请求 ==')
  await page.goto(threadUrl, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(1500)
  await page.getByRole('textbox').fill('写一个 hello.c')
  await page.getByRole('button', { name: '发送消息' }).click()

  // ------------------------------------------------------------------
  // 2) 进度（运行中）
  // ------------------------------------------------------------------
  console.log('\n== 2. 进度：运行中界面 ==')
  await page.waitForTimeout(3000)
  console.log('--- 运行中页面文本 ---')
  console.log(await bodyText(page))
  await shot(page, '进度-运行中')

  // ------------------------------------------------------------------
  // 3) 停止
  // ------------------------------------------------------------------
  console.log('\n== 3. 停止：点击「停止当前任务」 ==')
  const stopBtn = page.getByRole('button', { name: '停止当前任务' })
  await stopBtn.waitFor({ timeout: 30_000 })
  await stopBtn.click()
  await page.waitForTimeout(1200)
  console.log('--- 点击停止后页面文本 ---')
  console.log(await bodyText(page))
  await shot(page, '停止-已点击')

  await page.getByText('已取消当前任务').first().waitFor({ timeout: 30_000 }).catch(() => {
    console.log('   [warn] 未等到「已取消当前任务」')
  })
  await page.waitForTimeout(1500)
  console.log('--- 取消终态页面文本 ---')
  console.log(await bodyText(page))
  await shot(page, '停止-已取消')

  console.log('\n== 3b. 停止后再次执行 ==')
  await page.getByRole('textbox').fill('写一个 hello.c')
  await page.getByRole('button', { name: '发送消息' }).click()
  await page.getByText('执行完成', { exact: false }).first().waitFor({ timeout: 180_000 }).catch(() => {
    console.log('   [warn] 未等到「执行完成」')
  })
  await page.waitForTimeout(2000)
  console.log('--- 再次执行后页面文本 ---')
  console.log(await bodyText(page))
  await shot(page, '停止-再次执行成功')

  // ------------------------------------------------------------------
  // 4) 结果：查看任务面板
  // ------------------------------------------------------------------
  console.log('\n== 4. 结果：查看任务面板 ==')
  await page.getByRole('button', { name: '查看任务' }).click()
  await page.waitForTimeout(2500)
  console.log('--- 查看任务面板文本 ---')
  console.log(await bodyText(page))
  await shot(page, '结果-查看任务面板')

  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(3000)
  console.log('--- 刷新后页面文本 ---')
  console.log(await bodyText(page))
  await shot(page, '结果-刷新后')

  console.log('\n== 控制台错误 ==')
  console.log(errors.length ? errors.join('\n') : '（无）')

  await browser.close()
}

main().catch((e) => {
  console.error('FAILED:', e)
  process.exit(1)
})
