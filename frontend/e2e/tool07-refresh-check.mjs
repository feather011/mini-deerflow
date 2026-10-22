/**
 * 校验：页面刷新后，「查看任务 → 变化」是否仍显示上一轮的 diff。
 * （AgentChatPanel 里"从 API 响应恢复 run.result"的 effect 排在
 *   建立 task 的 effect 之前，可能被 ingestProjectExecuteResult 早退丢弃。）
 */
import { chromium } from '@playwright/test'

const BASE = process.env.ACCEPT_URL ?? 'http://localhost:5173'
const API = process.env.ACCEPT_API ?? 'http://127.0.0.1:8123'

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

console.log('\n--- 刷新页面 ---')
await page.reload({ waitUntil: 'domcontentloaded' })
await page.waitForTimeout(3000)
await page.getByRole('button', { name: '查看任务' }).click()
await page.waitForTimeout(2000)
const after = await page.locator('aside[aria-label="任务审查面板"]').innerText()
console.log('--- 刷新后 变化页签 ---')
console.log(after.split('\n').filter((l) => l.trim()).join(' | '))

console.log('\n结论：刷新后是否仍显示 hello.c 的 diff =', after.includes('hello.c'))

await browser.close()
