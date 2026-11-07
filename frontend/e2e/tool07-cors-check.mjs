/**
 * 校验：**生产跨源配置**的实际连通性。
 *
 * 背景：前端改用 `VITE_API_TARGET` 绝对地址后，SSE 与普通 API 都变成
 * **跨源直连**（不走 Vite 代理），于是后端必须允许前端 Origin。
 * 只在注释里写"记得配 CORS"不算证据 —— 本脚本给出真实连通证据。
 *
 * 两个夹具，都是**从实际前端地址（http://localhost:5173）发起的浏览器请求**：
 *
 *   A. 生产入口（`uvicorn mini_deerflow.api.app:app`）
 *      环境变量：MINI_DEERFLOW_CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
 *      —— 走的是生产默认 runtime factory + 生产 _add_cors 中间件，
 *         CORS **只放开这两个前端地址**（不是 `*`，不是全局放开）。
 *      provider 显式设为 fake：**不调用任何模型**，零额度、零外网。
 *
 *   B. 前端当前实际指向的验收后端（ACCEPT_API，默认 8123）
 *      —— 它的 CORS 由 scripts/tool07_acceptance_server.py 放开 5173-5179。
 *      这里用它验证**有活跃 Run 时 stop 返回 200** 的跨源成功取消
 *      （A 的 fake 模型 Run 瞬时结束，拿不到活跃 Run）。
 *
 * 关于「响应带 ACAO」这条断言：
 * `Access-Control-Allow-Origin` **永远不会**出现在 JS 可见的
 * `response.headers` 里（浏览器只把 `Access-Control-Expose-Headers`
 * 列出的头暴露给 JS）。因此：
 *   - **浏览器层证据** = 跨源 fetch **没有被拦截**（resolve 而非 reject，
 *     且 body / X-Run-ID 可读）。被 CORS 拦截时 fetch 会直接 reject。
 *   - **原始响应头证据** = 用 CDP 的 Network.responseReceivedExtraInfo
 *     读浏览器收到的**原始**响应头（含 ACAO / allow-methods）。
 * 两层都断言，才算完整。
 *
 * 本脚本**必须以失败退出**表示回归：只打印结论不算数。
 */
import { chromium } from '@playwright/test'
import { spawn } from 'node:child_process'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'

const FRONTEND = process.env.ACCEPT_URL ?? 'http://localhost:5173'
const ACCEPT_API = process.env.ACCEPT_API ?? 'http://127.0.0.1:8123'
const PROD_API = process.env.PROD_API ?? 'http://127.0.0.1:8124'
const REPO_ROOT = resolve(process.cwd(), '..')
/** 前端的实际 Origin —— CORS 白名单里只放这一个（和它的 127.0.0.1 孪生） */
const FRONTEND_ORIGIN = new URL(FRONTEND).origin
const CORS_ORIGINS = `${FRONTEND_ORIGIN},http://127.0.0.1:5173`

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

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// ---------------------------------------------------------------------------
// 夹具 A：起一个生产入口（真实 create_app + 真实 CORS 中间件）
// ---------------------------------------------------------------------------

const tmpRoot = mkdtempSync(join(tmpdir(), 'tool07-cors-'))

const prodEnv = {
  ...process.env,
  PYTHONPATH: join(REPO_ROOT, 'src'),
  // 显式 fake：只验证连通性，不调用任何模型（零额度、零外网）
  MINI_DEERFLOW_MODEL_PROVIDER: 'fake',
  // 生产 CORS 配置路径：环境变量 → ApiSettings.cors_origins → _add_cors
  // 只放开实际前端地址，不使用 `*`
  MINI_DEERFLOW_CORS_ORIGINS: CORS_ORIGINS,
  MINI_DEERFLOW_DB_PATH: join(tmpRoot, 'cors.db'),
  MINI_DEERFLOW_WORKSPACES_ROOT: join(tmpRoot, 'workspaces'),
}

console.log('启动夹具 A（生产入口 uvicorn mini_deerflow.api.app:app）')
console.log(`  CORS 白名单: ${CORS_ORIGINS}`)
const prodProc = spawn(
  'python',
  [
    '-m',
    'uvicorn',
    'mini_deerflow.api.app:app',
    '--host',
    '127.0.0.1',
    '--port',
    '8124',
    '--log-level',
    'warning',
  ],
  { cwd: REPO_ROOT, env: prodEnv, stdio: ['ignore', 'pipe', 'pipe'] },
)
let prodLog = ''
prodProc.stdout.on('data', (d) => (prodLog += d.toString()))
prodProc.stderr.on('data', (d) => (prodLog += d.toString()))

async function waitForHealth(base, timeoutMs) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const r = await fetch(`${base}/api/health`)
      if (r.ok) return true
    } catch {
      /* 还没起来 */
    }
    await sleep(300)
  }
  return false
}

if (!(await waitForHealth(PROD_API, 60_000))) {
  console.error('夹具 A 启动失败：')
  console.error(prodLog)
  prodProc.kill()
  process.exit(1)
}
console.log('夹具 A 已就绪\n')

// ---------------------------------------------------------------------------
// 浏览器：从实际前端地址发起跨源请求
// ---------------------------------------------------------------------------

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } })
await page.goto(FRONTEND, { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(1500)

// 用 CDP 抓浏览器收到的**原始**响应头（含 ACAO / allow-methods）
const cdp = await page.context().newCDPSession(page)
await cdp.send('Network.enable')
const requestInfo = new Map()
const rawResponses = []
cdp.on('Network.requestWillBeSent', (e) => {
  requestInfo.set(e.requestId, { method: e.request.method, url: e.request.url })
})
cdp.on('Network.responseReceivedExtraInfo', (e) => {
  const info = requestInfo.get(e.requestId)
  rawResponses.push({
    method: info?.method ?? '?',
    url: info?.url ?? '',
    status: e.statusCode,
    headers: e.headers ?? {},
  })
})

/** 在页面里发一个跨源请求，返回状态码、JS 可见响应头与 body 文本 */
async function crossFetch(url, init = {}) {
  return page.evaluate(
    async ([u, i]) => {
      try {
        const r = await fetch(u, i)
        const text = await r.text()
        const headers = {}
        for (const [k, v] of r.headers.entries()) headers[k] = v
        return { ok: r.ok, status: r.status, headers, body: text.slice(0, 400), error: null }
      } catch (e) {
        // 跨源被拦时 fetch 直接 reject（TypeError）——这本身就是失败证据
        return { ok: false, status: 0, headers: {}, body: '', error: String(e) }
      }
    },
    [url, { credentials: 'omit', ...init }],
  )
}

/** 读一段 SSE 流，返回状态码、JS 可见响应头与收到的文本 */
async function readSse(url, message, stopAt = ['run.completed', 'run.cancelled', 'run.failed'], timeoutMs = 60_000) {
  return page.evaluate(
    async ([u, m, stops, t]) => {
      const ctrl = new AbortController()
      const timer = setTimeout(() => ctrl.abort(), t)
      const out = { status: 0, headers: {}, text: '', error: null }
      try {
        const r = await fetch(u, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
          body: JSON.stringify({ message: m }),
          signal: ctrl.signal,
        })
        out.status = r.status
        for (const [k, v] of r.headers.entries()) out.headers[k] = v
        const reader = r.body.getReader()
        const dec = new TextDecoder()
        let acc = ''
        while (acc.length < 8000) {
          const { done, value } = await reader.read()
          if (done) break
          acc += dec.decode(value, { stream: true })
          if (stops.some((s) => acc.includes(s))) break
        }
        try {
          await reader.cancel()
        } catch {
          /* ignore */
        }
        out.text = acc
      } catch (e) {
        out.error = String(e)
      } finally {
        clearTimeout(timer)
      }
      return out
    },
    [url, message, stopAt, timeoutMs],
  )
}

/** 从 SSE 文本里解析出事件列表 [{ event, data }] */
function parseSseEvents(text) {
  const out = []
  for (const chunk of text.split('\n\n')) {
    const ev = { event: '', data: null }
    let raw = null
    for (const line of chunk.split('\n')) {
      if (line.startsWith('event: ')) ev.event = line.slice(7).trim()
      else if (line.startsWith('data: ')) raw = line.slice(6)
    }
    if (!raw) continue
    try {
      ev.data = JSON.parse(raw)
    } catch {
      continue
    }
    out.push(ev)
  }
  return out
}

/** 从 CDP 抓到的原始响应里找一条 */
function findRaw(method, urlSuffix, apiBase) {
  return rawResponses.find(
    (r) => r.method === method && r.url.startsWith(apiBase) && r.url.endsWith(urlSuffix),
  )
}

// ---------------------------------------------------------------------------
// 1) 普通 API（先跑完所有请求，最后统一检查 CDP 抓到的预检与原始响应头）
// ---------------------------------------------------------------------------
console.log('--- 1) 普通 API（夹具 A：生产入口） ---')

const health = await crossFetch(`${PROD_API}/api/health`)
check(health.error === null, `GET /api/health 未被 CORS 拦截（error=${health.error ?? '无'}）`)
check(health.status === 200, `GET /api/health 跨源返回 200（实际 ${health.status}）`)
check(health.body.includes('ok'), 'GET /api/health 的 body 浏览器可读')

const created = await crossFetch(`${PROD_API}/api/threads`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ project_id: '' }),
})
check(created.error === null, `POST /api/threads 未被 CORS 拦截（error=${created.error ?? '无'}）`)
check(created.status > 0, `POST /api/threads 跨源拿到响应（status=${created.status}）`)

const threadId = (() => {
  try {
    return JSON.parse(created.body).thread_id ?? ''
  } catch {
    return ''
  }
})()
check(threadId !== '', `创建出的 thread 可解析（${threadId || '无'}）`)
// 非空不等于「这是正确的会话 ID」——必须与响应里的 thread_id 字段逐字相等，
// 且不等于同响应里的 project_id。
const createdBody = (() => {
  try {
    return JSON.parse(created.body)
  } catch {
    return {}
  }
})()
check(
  threadId === (createdBody.thread_id ?? ''),
  `使用的会话 ID 就是响应里的 thread_id（${threadId || '无'}）`,
)
check(
  threadId !== (createdBody.project_id ?? ''),
  `会话 ID 与 project_id 不同（thread=${threadId || '无'} / project=${createdBody.project_id ?? '无'}）`,
)

// 预检会缓存，单独用一条此前没访问过的路径触发 DELETE 预检
const delProbe = await crossFetch(`${PROD_API}/api/files/probe-cors-check`, { method: 'DELETE' })
check(delProbe.error === null, `DELETE 跨源未被 CORS 拦截（error=${delProbe.error ?? '无'}）`)

// ---------------------------------------------------------------------------
// 2) SSE
// ---------------------------------------------------------------------------
console.log('\n--- 2) SSE（夹具 A：生产入口，fake 模型不联网） ---')

const sse = await readSse(`${PROD_API}/api/threads/${threadId}/runs/stream`, '你好')
check(sse.error === null, `SSE 未被 CORS 拦截（error=${sse.error ?? '无'}）`)
check(sse.status === 200, `SSE 跨源返回 200（实际 ${sse.status}）`)
check(!!sse.headers['x-run-id'], `X-Run-ID 响应头浏览器可读（${sse.headers['x-run-id'] ?? '无'}）——证明 expose_headers 生效`)
check(sse.text.includes('run.started'), 'SSE 事件流浏览器可读（收到 run.started）')
check(sse.text.includes('run.completed'), 'SSE 事件流读到终态 run.completed')
console.log(`  （SSE 收到 ${sse.text.length} 字节）`)

// 事件必须属于「目标 Run」：事件里的 thread_id === 用的会话 ID，
// 且事件里的 run_id === 响应头 X-Run-ID。
const sseEventsA = parseSseEvents(sse.text)
const startedA = sseEventsA.find((e) => e.event === 'run.started')
check(!!startedA, '解析出 run.started 事件')
if (startedA) {
  check(
    startedA.data?.thread_id === threadId,
    `run.started 的 thread_id 就是目标会话（事件=${startedA.data?.thread_id ?? '无'} / 用的是=${threadId || '无'}）`,
  )
  check(
    !!sse.headers['x-run-id'] && startedA.data?.run_id === sse.headers['x-run-id'],
    `run.started 的 run_id 与 X-Run-ID 一致（事件=${startedA.data?.run_id ?? '无'} / 头=${sse.headers['x-run-id'] ?? '无'}）`,
  )
}

// ---------------------------------------------------------------------------
// 3) stop
// ---------------------------------------------------------------------------
console.log('\n--- 3) stop（夹具 A：生产入口） ---')

const stopA = await crossFetch(`${PROD_API}/api/threads/${threadId}/runs/stop`, { method: 'POST' })
check(stopA.error === null, `stop 未被 CORS 拦截（error=${stopA.error ?? '无'}）`)
check(stopA.status > 0, `stop 跨源拿到响应（status=${stopA.status}）`)
check(
  stopA.status === 200 || stopA.status === 404,
  `stop 响应是契约内的 200/404（实际 ${stopA.status}）——404 表示该入口无活跃 Run，响应仍可读说明 CORS 放行`,
)

// 前端真实的 stop 请求是 **simple request**（只有 Accept: application/json、
// 没有 body → 浏览器不设 Content-Type → 不触发预检）。
// 上面那次 stop 因此**不会**有 OPTIONS；它的实际响应带 ACAO 即可。
// 这里再补一次「带 Content-Type 强制预检」的 stop，证明该路由
// 在需要预检时同样放行（预检缓存按 header 集合区分，不会命中上一次）。
const stopApre = await crossFetch(`${PROD_API}/api/threads/${threadId}/runs/stop`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: '{}',
})
check(stopApre.error === null, `强制预检的 stop 未被 CORS 拦截（error=${stopApre.error ?? '无'}）`)

// ---------------------------------------------------------------------------
// 4) stop 成功取消（夹具 B：前端当前实际指向的验收后端）
// ---------------------------------------------------------------------------
console.log('\n--- 4) stop 成功取消（夹具 B：前端实际指向的验收后端） ---')

const acceptUp = await waitForHealth(ACCEPT_API, 5_000)
check(acceptUp, `验收后端可达（${ACCEPT_API}）`)

if (acceptUp) {
  const projB = await crossFetch(`${ACCEPT_API}/api/projects`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: 'TOOL-07 CORS 夹具' }),
  })
  const projId = (() => {
    try {
      return JSON.parse(projB.body).project_id ?? ''
    } catch {
      return ''
    }
  })()
  const thB = await crossFetch(`${ACCEPT_API}/api/threads`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project_id: projId }),
  })
  const thIdB = (() => {
    try {
      return JSON.parse(thB.body).thread_id ?? ''
    } catch {
      return ''
    }
  })()
  check(thIdB !== '', `夹具 B 的 thread 创建成功（${thIdB || '无'}）`)
  // 关键：这里拼进 URL 的是**会话 ID**（thread_id），不是 project_id。
  // 非空检查证明不了这一点，所以额外断言它逐字等于响应里的 thread_id、
  // 且与同响应的 project_id 不同。
  const thBodyB = (() => {
    try {
      return JSON.parse(thB.body)
    } catch {
      return {}
    }
  })()
  check(
    thIdB === (thBodyB.thread_id ?? ''),
    `夹具 B 用的就是响应里的 thread_id（${thIdB || '无'}）`,
  )
  check(
    thIdB !== (thBodyB.project_id ?? ''),
    `夹具 B 的会话 ID 与 project_id 不同（thread=${thIdB || '无'} / project=${thBodyB.project_id ?? '无'}）`,
  )
  check(
    thIdB !== projId,
    `夹具 B 的会话 ID 不是项目 ID（${thIdB || '无'} != ${projId || '无'}）`,
  )

  // 触发 harness（假 LLM 有 15s 延迟）→ Run 存活足够长，可以真的取消
  const sseB = await readSse(
    `${ACCEPT_API}/api/threads/${thIdB}/runs/stream`,
    '写一个 hello.c',
    ['tool.started'],
    60_000,
  )
  check(sseB.error === null, `夹具 B 的 SSE 未被 CORS 拦截（error=${sseB.error ?? '无'}）`)
  check(sseB.status === 200, `夹具 B 的 SSE 跨源返回 200（实际 ${sseB.status}）`)
  // 收到的是**目标 Run** 的事件，而不是别的会话的事件
  const sseEventsB = parseSseEvents(sseB.text)
  const startedB = sseEventsB.find((e) => e.event === 'run.started')
  check(!!startedB, `夹具 B 的 SSE 收到 run.started 事件（收到 ${sseEventsB.length} 条事件）`)
  if (startedB) {
    check(
      startedB.data?.thread_id === thIdB,
      `夹具 B 的 run.started 属于目标会话（事件=${startedB.data?.thread_id ?? '无'} / 用的是=${thIdB || '无'}）`,
    )
    check(
      !!sseB.headers['x-run-id'] && startedB.data?.run_id === sseB.headers['x-run-id'],
      `夹具 B 的 run.started 的 run_id 与 X-Run-ID 一致（事件=${startedB.data?.run_id ?? '无'} / 头=${sseB.headers['x-run-id'] ?? '无'}）`,
    )
  }

  const stopB = await crossFetch(`${ACCEPT_API}/api/threads/${thIdB}/runs/stop`, { method: 'POST' })
  check(stopB.error === null, `夹具 B 的 stop 未被 CORS 拦截（error=${stopB.error ?? '无'}）`)
  check(stopB.status === 200, `活跃 Run 的 stop 跨源返回 200（实际 ${stopB.status}）`)
  check(stopB.body.includes('stopped'), `stop 响应体可读且表示已停止（${stopB.body.slice(0, 80)}）`)
  // stop 的响应必须回显同一个会话 ID
  const stopBodyB = (() => {
    try {
      return JSON.parse(stopB.body)
    } catch {
      return {}
    }
  })()
  check(
    stopBodyB.thread_id === thIdB,
    `stop 响应回显的 thread_id 就是目标会话（响应=${stopBodyB.thread_id ?? '无'} / 用的是=${thIdB || '无'}）`,
  )
  check(
    stopBodyB.status === 'stopped',
    `stop 响应 status=stopped（实际 ${stopBodyB.status ?? '无'}）`,
  )
}

// ---------------------------------------------------------------------------
// 5) CDP 原始响应头：预检与实际响应都带正确的 ACAO
// ---------------------------------------------------------------------------
console.log('\n--- 5) 原始响应头（CDP 抓取） ---')
await sleep(500)

console.log(`  （共抓到 ${rawResponses.length} 条带原始头的响应）`)
for (const r of rawResponses) {
  const acao = r.headers['access-control-allow-origin'] ?? '无'
  console.log(`    ${r.method} ${r.status} ${r.url} → ACAO=${acao}`)
}

/** 断言某条预检：200 + ACAO + 允许 POST */
function checkPreflight(label, urlSuffix, apiBase) {
  const pf = findRaw('OPTIONS', urlSuffix, apiBase)
  check(!!pf, `抓到了 ${label} 的预检响应`)
  if (!pf) return
  check(pf.status === 200, `${label} 预检返回 200（实际 ${pf.status}）`)
  check(
    pf.headers['access-control-allow-origin'] === FRONTEND_ORIGIN,
    `${label} 预检 ACAO 正确（实际 ${pf.headers['access-control-allow-origin'] ?? '无'}）`,
  )
  check(
    (pf.headers['access-control-allow-methods'] ?? '').includes('POST'),
    `${label} 预检允许 POST（实际 ${pf.headers['access-control-allow-methods'] ?? '无'}）`,
  )
  check(
    (pf.headers['access-control-allow-headers'] ?? '').toLowerCase().includes('content-type'),
    `${label} 预检允许 content-type（实际 ${pf.headers['access-control-allow-headers'] ?? '无'}）`,
  )
}

/** 断言某条实际响应带 ACAO */
function checkActualAca(label, urlSuffix, apiBase) {
  const r = findRaw('POST', urlSuffix, apiBase) ?? findRaw('GET', urlSuffix, apiBase)
  check(!!r, `抓到了 ${label} 的实际响应`)
  if (!r) return
  check(
    r.headers['access-control-allow-origin'] === FRONTEND_ORIGIN,
    `${label} 实际响应带 ACAO=${FRONTEND_ORIGIN}（实际 ${r.headers['access-control-allow-origin'] ?? '无'}）`,
  )
}

checkPreflight('threads POST', '/api/threads', PROD_API)
checkPreflight('SSE POST', '/runs/stream', PROD_API)
checkPreflight('stop POST（强制预检）', '/runs/stop', PROD_API)
checkPreflight('files DELETE', '/api/files/probe-cors-check', PROD_API)

checkActualAca('health GET', '/api/health', PROD_API)
checkActualAca('SSE POST', '/runs/stream', PROD_API)
checkActualAca('stop POST', '/runs/stop', PROD_API)

await browser.close()
prodProc.kill()
await sleep(300)

console.log(`\n结论：${failures.length === 0 ? '全部通过' : `失败 ${failures.length} 项`}`)
if (failures.length > 0) {
  for (const f of failures) console.log(`  - ${f}`)
  process.exit(1)
}
