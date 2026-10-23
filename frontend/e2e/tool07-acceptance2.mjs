/**
 * TOOL-07 验收补测：查看任务面板的三个页签 + 运行中的实时过程。
 * 用法：node e2e/tool07-acceptance2.mjs
 */
import { chromium } from '@playwright/test'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const BASE = process.env.ACCEPT_URL ?? 'http://localhost:5173'
const API = process.env.ACCEPT_API ?? 'http://127.0.0.1:8123'
const OUT = process.env.ACCEPT_OUT ?? join(process.cwd(), '..', 'tool07-acceptance-shots')
mkdirSync(OUT, { recursive: true })

let n = 20
async function shot(page, label) {
  n += 1
  const file = join(OUT, `${String(n).padStart(2, '0')}-${label}.png`)
  await page.screenshot({ path: file, fullPage: true })
  console.log(`   [截图] ${file}`)
}
async function bodyText(page) {
  return (await page.locator('body').innerText()).replace(/\n{3,}/g, '\n\n')
}

const post = async (p, payload) => {
  const r = await fetch(API + p, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload ?? {}),
  })
  return r.json()
}

async function main() {
  const proj = await post('/api/projects', { name: 'TOOL-07 页签检查' })
  const th = await post('/api/threads', { project_id: proj.project_id })
  const url = `${BASE}/threads/${th.thread_id}`
  console.log('thread url:', url)

  const browser = await chromium.launch()
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } })
  const errors = []
  page.on('console', (m) => { if (m.type() === 'error') errors.push(`[console] ${m.text()}`) })
  page.on('pageerror', (e) => errors.push(`[pageerror] ${e.message}`))

  await page.goto(url, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(1500)

  console.log('\n== 发送编码请求 ==')
  await page.getByRole('textbox').fill('写一个 hello.c')
  await page.getByRole('button', { name: '发送消息' }).click()

  console.log('\n== 运行中：查看任务面板 ==')
  await page.waitForTimeout(4000)
  await page.getByRole('button', { name: '查看任务' }).click()
  await page.waitForTimeout(2500)
  console.log('--- 运行中 查看任务面板 ---')
  console.log(await bodyText(page))
  await shot(page, '运行中-查看任务面板')

  console.log('\n== 运行中：过程页签 ==')
  const eventsTab = page.getByRole('tab', { name: '过程', exact: true })
  if (await eventsTab.count()) {
    await eventsTab.first().click()
    await page.waitForTimeout(1200)
    console.log(await bodyText(page))
    await shot(page, '运行中-过程页签')
  } else {
    console.log('   [warn] 没有「过程」页签')
  }

  console.log('\n== 等到完成 ==')
  await page.getByTestId('task-status-pill').filter({ hasText: '已完成' }).first()
    .waitFor({ timeout: 180_000 }).catch(() => {
      console.log('   [warn] 未等到状态徽标「已完成」')
    })
  await page.waitForTimeout(2000)

  for (const tab of ['变化', '验证', '过程']) {
    const t = page.getByRole('tab', { name: tab, exact: true })
    if (!(await t.count())) { console.log(`   [warn] 无「${tab}」页签`); continue }
    await t.first().click()
    await page.waitForTimeout(1200)
    console.log(`\n--- 完成态「${tab}」页签 ---`)
    console.log(await bodyText(page))
    await shot(page, `完成态-${tab}页签`)
  }

  console.log('\n== 控制台错误 ==')
  console.log(errors.length ? errors.join('\n') : '（无）')
  await browser.close()
}

main().catch((e) => { console.error('FAILED:', e); process.exit(1) })
