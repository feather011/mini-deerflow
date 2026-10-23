/**
 * 校验：页面刷新后，「查看任务 → 变化」是否仍显示上一轮的 diff。
 *
 * 背景（F6）：AgentChatPanel 里"从 API 响应恢复 run.result"的 effect 若排在
 * 建立 task 的 effect 之前，冷挂载时 ingestProjectExecuteResult 会因缓存为空
 * 早退，把保存的结果丢掉 —— 刷新后文件与 diff 全部消失。
 *
 * 本脚本**必须以失败退出**表示回归：只打印结论不算数。
 */
import { chromium } from '@playwright/test'

const BASE = process.env.ACCEPT_URL ?? 'http://localhost:5173'
const API = process.env.ACCEPT_API ?? 'http://127.0.0.1:8123'

/** 断言失败清单：非空则以退出码 1 结束 */
const failures = []

function check(cond, message) {
  if (cond) {
    console.log(`  [PASS] ${message}`)
  } else {
    console.log(`  [FAIL] ${message}`)
    failures.push(message)
  }
}

const post = async (p, payload) => {
  const r = await fetch(API + p, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload ?? {}),
  })
  return r.json()
}

const proj = await post('/api/projects', { name: 'TOOL-07 刷新保持检查' })
const th = await post('/api/threads', { project_id: proj.project_id })
const url = `${BASE}/threads/${th.thread_id}`
console.log('thread:', th.thread_id)

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } })
await page.goto(url, { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(1500)

await page.getByRole('textbox').fill('写一个 hello.c')
await page.getByRole('button', { name: '发送消息' }).click()
await page.getByRole('button', { name: '查看任务' }).click()
await page.getByTestId('task-status-pill').filter({ hasText: '已完成' }).first()
  .waitFor({ timeout: 180_000 })
await page.waitForTimeout(1500)

const before = await page.locator('aside[aria-label="任务审查面板"]').innerText()
console.log('--- 刷新前 变化页签 ---')
console.log(before.split('\n').filter((l) => l.trim()).join(' | '))
check(before.includes('hello.c'), '刷新前变化页签显示 hello.c')

console.log('\n--- 刷新页面 ---')
await page.reload({ waitUntil: 'domcontentloaded' })
await page.waitForTimeout(3000)
await page.getByRole('button', { name: '查看任务' }).click()
await page.waitForTimeout(2000)
const after = await page.locator('aside[aria-label="任务审查面板"]').innerText()
console.log('--- 刷新后 变化页签 ---')
console.log(after.split('\n').filter((l) => l.trim()).join(' | '))

// F6 的核心断言：刷新后文件列表、diff 正文都必须在
check(after.includes('hello.c'), '刷新后变化页签仍显示 hello.c')
check(/\+\d+\s*−\s*0/.test(after) || after.includes('+6'), '刷新后仍显示新增行计数（+6）')

// diff 正文行：变化页签里应逐行显示 hello.c 的内容
const diffRows = await page.locator('aside[aria-label="任务审查面板"] [data-line-type]').count()
check(diffRows >= 6, `刷新后仍渲染 diff 正文行（实际 ${diffRows} 行，应 ≥ 6）`)

// 验证页签仍如实显示"未运行"（不得因为刷新就谎报通过）
await page.getByRole('tab', { name: '验证', exact: true }).click()
await page.waitForTimeout(500)
// 注意：不能用整块面板文本判断是否含"通过"——页脚固定写着
// "执行完成不等于业务验收通过；不自动提交或推送"，那是说明文案，不是状态。
// 必须精确看验证状态 pill。
const validationPill = page.locator(
  'aside[aria-label="任务审查面板"] [data-testid="validation-pill"]',
)
const validationText = await validationPill.innerText()
check(validationText.includes('未运行'), '刷新后验证页签仍如实显示「未运行」')
check(!validationText.includes('通过'), '刷新后验证页签不谎报「通过」')

await browser.close()

console.log(`\n结论：${failures.length === 0 ? '全部通过' : `失败 ${failures.length} 项`}`)
if (failures.length > 0) {
  for (const f of failures) console.log(`  - ${f}`)
  process.exit(1)
}
