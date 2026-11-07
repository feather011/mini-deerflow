/**
 * 校验：人工验收返修第 2 / 3 项 —— **取消后重试** + **「知道了」能关闭**。
 *
 * 对应验收表里的四行：
 *   - 取消后马上重新发送、故意延迟响应头 → 立即退出旧提示，显示新任务正在连接
 *   - 点击「知道了」                     → 当前提示消失
 *   - 再次执行并取消                     → 新的取消提示正常出现
 *   - 重试任务完成                       → 本轮文件和 diff 正常更新，刷新后保留
 *
 * 「旧连接迟到一个取消事件」是状态机不变量，由
 * `src/features/threads/useAgentStream.test.tsx` 的
 * 「旧连接迟到的取消终态不得覆盖新运行状态」精确覆盖 ——
 * 浏览器里旧 fetch 会被 AbortController 真正取消，构造不出这个竞态。
 * 本脚本覆盖它**可观察到的后果**：新一轮开始后旧提示不复活、状态停在连接中。
 *
 * 本脚本**必须以失败退出**表示回归：只打印结论不算数。
 */
import { chromium } from '@playwright/test'

const BASE = process.env.ACCEPT_URL ?? 'http://localhost:5173'
const API = process.env.ACCEPT_API ?? 'http://127.0.0.1:8123'
/** harness 假 LLM 每次调用有额外延迟，跑完一轮要留足时间 */
const RUN_TIMEOUT = 240_000
/** 人为推迟响应头的时长（秒） */
const HEADER_DELAY_MS = 8_000

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

const proj = await post('/api/projects', { name: 'TOOL-07 取消重试验收' })
const th = await post('/api/threads', { project_id: proj.project_id })
const url = `${BASE}/threads/${th.thread_id}`
console.log('thread:', th.thread_id)

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } })
await page.goto(url, { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(1500)

const composer = page.getByRole('textbox')
const sendBtn = page.getByRole('button', { name: '发送消息' })
const stopBtn = page.getByRole('button', { name: '停止当前任务' })
const cancelNotice = page.getByRole('alert', { name: '已取消提示' })
const runningLine = page.getByLabel('当前运行状态')
const panel = page.locator('aside[aria-label="任务审查面板"]')

// ---------------------------------------------------------------------------
// 第 1 轮：发出去 → 停止 → 后端确认取消
// ---------------------------------------------------------------------------
console.log('\n--- 第 1 轮：执行并取消 ---')
await composer.fill('写一个 hello.c')
await sendBtn.click()
await runningLine.waitFor({ state: 'visible', timeout: 30_000 })
check(true, '发送后立刻出现运行状态行')

await stopBtn.click()
await cancelNotice.waitFor({ state: 'visible', timeout: 90_000 })
check(true, '后端确认取消后显示「已取消当前任务」')

// ---------------------------------------------------------------------------
// 验收表：点击「知道了」 → 当前提示消失
// ---------------------------------------------------------------------------
console.log('\n--- 验收表：点击「知道了」 ---')
await cancelNotice.getByRole('button', { name: '知道了' }).click()
await page.waitForTimeout(500)
check((await cancelNotice.count()) === 0, '点击「知道了」后当前提示消失')

// ---------------------------------------------------------------------------
// 验收表：取消后马上重新发送，并人为推迟响应头
// ---------------------------------------------------------------------------
console.log('\n--- 验收表：延迟响应头后重新发送 ---')

// 只推迟**第一次** SSE：模拟"后端迟迟不回响应头"
let delayedOnce = false
await page.route(
  (u) => u.pathname.endsWith('/runs/stream'),
  async (route) => {
    if (!delayedOnce) {
      delayedOnce = true
      console.log(`  （人为推迟响应头 ${HEADER_DELAY_MS / 1000} 秒）`)
      await new Promise((r) => setTimeout(r, HEADER_DELAY_MS))
    }
    await route.continue()
  },
)

await composer.fill('再写一个 hello.c')
await sendBtn.click()

await page.waitForTimeout(800) // 远小于 HEADER_DELAY_MS，响应头此刻必然还没到
check((await cancelNotice.count()) === 0, '重新发送后立即退出旧的「已取消」提示')

const lineText = await runningLine.innerText().catch(() => '')
check(
  lineText.includes('正在建立连接'),
  `延迟响应头期间显示新任务「正在建立连接」（实际显示「${lineText.trim()}」）`,
)

// ---------------------------------------------------------------------------
// 验收表：重试任务完成 → 本轮文件和 diff 正常更新
// ---------------------------------------------------------------------------
console.log('\n--- 验收表：重试任务完成 ---')
await page.getByRole('button', { name: '查看任务' }).click()
await page
  .getByTestId('task-status-pill')
  .filter({ hasText: '已完成' })
  .first()
  .waitFor({ timeout: RUN_TIMEOUT })
await page.waitForTimeout(1500)

const beforeReload = await panel.innerText()
check(beforeReload.includes('hello.c'), '重试完成后变化页签显示 hello.c')
const rowsBefore = await panel.locator('[data-line-type]').count()
check(rowsBefore > 0, `重试完成后 diff 正文已渲染（${rowsBefore} 行）`)

// ---------------------------------------------------------------------------
// 验收表：刷新后保留
// ---------------------------------------------------------------------------
console.log('\n--- 刷新后保留 ---')
await page.reload({ waitUntil: 'domcontentloaded' })
await page.waitForTimeout(3000)
await page.getByRole('button', { name: '查看任务' }).click()
await page.waitForTimeout(2000)
const afterReload = await panel.innerText()
check(afterReload.includes('hello.c'), '刷新后变化页签仍显示 hello.c')
const rowsAfter = await panel.locator('[data-line-type]').count()
check(rowsAfter > 0, `刷新后 diff 正文仍渲染（${rowsAfter} 行）`)

// ---------------------------------------------------------------------------
// 验收表：再次执行并取消 → 新的取消提示正常出现
// ---------------------------------------------------------------------------
console.log('\n--- 验收表：再次执行并取消 ---')
await composer.fill('再写一次 hello.c')
await sendBtn.click()
await runningLine.waitFor({ state: 'visible', timeout: 30_000 })
check((await cancelNotice.count()) === 0, '新一轮开始时没有残留的旧提示')

await stopBtn.click()
await cancelNotice.waitFor({ state: 'visible', timeout: 90_000 })
check(true, '再次执行并取消：新的取消提示正常出现')

// 新提示同样可以关掉
await cancelNotice.getByRole('button', { name: '知道了' }).click()
await page.waitForTimeout(500)
check((await cancelNotice.count()) === 0, '新提示的「知道了」同样能关闭')

await browser.close()

console.log(`\n结论：${failures.length === 0 ? '全部通过' : `失败 ${failures.length} 项`}`)
if (failures.length > 0) {
  for (const f of failures) console.log(`  - ${f}`)
  process.exit(1)
}