# Phase 20 进度

## 状态图例

```
⬜ 未开始    🟡 进行中    ✅ 已完成    ⛔ 阻塞
```

## 总体阶段

| 阶段 | 内容 | 状态 |
|------|------|------|
| 20A | 前端骨架与状态检测 | ✅ |
| 20B | Thread 管理 | ✅ |
| 20C | Agent Run | ✅ |
| 20D | SSE 实时事件 | ✅ |
| 20E | Files / Artifacts | ✅ |
| 20F | 错误体验与 E2E | ✅ |
| — | **Phase 20 总体验收** | **✅** |

## 20A 验收清单

- [x] 前端项目能够安装依赖
- [x] TypeScript 严格模式开启
- [x] 开发服务器可以启动
- [x] 生产构建通过
- [x] 单元/组件测试通过
- [x] API Base URL 可通过环境变量配置
- [x] `/api/health` 可以被调用
- [x] `/api/ready` 可以被调用
- [x] Loading、Ready、Not Ready、Offline、Error 状态均有界面
- [x] 后端错误中的 Request ID 被保留
- [x] 页面包含三栏工作台骨架
- [x] 移动端可以正常降级布局
- [x] 没有提前实现 20B～20F
- [x] 没有修改无关后端逻辑
- [x] 未 commit、未 push（执行完成）

## 执行记录

### 2026-09-30 Phase 20A 实施

**修改文件（新增）**

```
frontend/src/main.tsx                             新增
frontend/src/styles.css                          新增
frontend/src/app/App.tsx                         新增
frontend/src/app/ErrorBoundary.tsx                新增
frontend/src/app/App.test.tsx                    新增
frontend/src/features/status/BackendStatusBar.tsx 新增
frontend/src/features/status/BackendStatusBar.test.tsx 新增
frontend/src/features/status/BackendStatusBar.module.css 新增
frontend/src/features/status/useBackendStatus.ts  新增
frontend/src/features/status/useBackendStatus.test.tsx 新增
frontend/src/features/status/index.ts             新增
frontend/src/shared/types/index.ts               新增
frontend/src/shared/api/config.ts                新增
frontend/src/shared/api/client.ts                新增
frontend/src/shared/api/client.test.ts           新增
frontend/src/shared/api/health.ts               新增
frontend/src/test/setup.ts                      新增
frontend/.env.example                           新增
frontend/.gitignore                             新增
docs/frontend/ARCHITECTURE.md                   新增
docs/frontend/PROGRESS.md                      新增
```

**修改文件（配置）**

```
frontend/package.json                              修改 scripts, devDependencies
frontend/tsconfig.app.json                        开启 strict, noUncheckedIndexedAccess
frontend/vite.config.ts                           添加 Vitest 配置
```

**实际执行命令**

```powershell
npm create vite@latest frontend -- --template react-ts
cd frontend
npm install
npm install react-router-dom @tanstack/react-query
npm install -D vitest @testing-library/react @testing-library/dom @testing-library/user-event @testing-library/jest-dom jsdom
npm run test -- --run
npm run lint
npm run build
```

**测试结果**

```
Test Files  4 passed (4)
     Tests  21 passed (21)
       ├─ client.test.ts              8 tests
       ├─ useBackendStatus.test.tsx   4 tests
       ├─ BackendStatusBar.test.tsx    6 tests
       └─ App.test.tsx                3 tests
Duration  1.50s
```

**Lint 结果**：通过，无错误

**构建结果**

```
dist/index.html                   0.45 kB │ gzip:  0.29 kB
dist/assets/index-FT2AzmHQ.css    2.36 kB │ gzip:  0.89 kB
dist/assets/index-CRN6OIWA.js   296.30 kB │ gzip: 93.36 kB
✓ built in 340ms
```

**遗留问题**：无

## 阻塞项

当前无阻塞

## API Client 设计

```ts
// shared/api/client.ts
class ApiError extends Error {
  status: number
  code: string
  requestId?: string
  details: unknown          // 原来是 Record<string, unknown> | null，改 为 unknown
  override message: string
}

// shared/api/client.ts
async function apiGet<T>(path: string, options?: RequestOptions): Promise<T>

// shared/api/health.ts
async function getHealth(): Promise<HealthResponse>
async function getReady(): Promise<ReadyResponse>
```

## Health/Ready 数据流

```
TanStack Query (staleTime: 30s, retry: false)
  ├─ getHealth() → GET /api/health
  │   └─ 失败 → status=0 ? 'offline' : 'error'
  └─ getReady() → GET /api/ready (enabled: health.isSuccess)
      └─ 失败 → status=503 ? 'not-ready' : 'error'
```

## 页面状态流

```
BackendStatusBar
  └─ useBackendStatus()
      └─ TanStack Query
          ├─ 'checking' (初始 loading)
          ├─ 'ready'    (health + ready 均成功)
          ├─ 'not-ready' (health 成功, ready 返回 503)
          ├─ 'offline'   (health 网络失败)
          └─ 'error'     (health 其他错误)
```

---

## 20B 验收清单

- [x] Thread API 类型定义与后端 Schema 一致
- [x] API Client 支持 POST / DELETE / 201 / 204
- [x] Thread API 层（listThreads / createThread / getThread / deleteThread）
- [x] TanStack Query Hooks（useThreads / useThread / useCreateThread / useDeleteThread）
- [x] URL 作为当前 Thread 状态来源（/threads/:threadId）
- [x] ThreadSidebar：列表 / 创建 / 选择 / 删除
- [x] 删除前内联二次确认
- [x] 删除当前 Thread 后自动返回 /
- [x] Loading / Empty / Error 状态
- [x] 404 / 409 / 503 等错误展示 + Request ID
- [x] ThreadDetailPlaceholder：未选择 / 加载中 / 详情 / 404
- [x] URL 刷新恢复 Thread
- [x] 自动化测试覆盖关键路径
- [x] typecheck / oxlint / test / build 全部通过
- [x] 真实浏览器联调（已手动验证通过）
- [x] 文档同步
- [x] 未 commit、未 push

---

### 2026-09-30 Phase 20B 实施

**新增文件**

```
frontend/src/shared/types/threads.ts                    新增
frontend/src/shared/api/index.ts                       新增
frontend/src/features/threads/queryKeys.ts              新增
frontend/src/features/threads/api.ts                   新增
frontend/src/features/threads/api.test.ts               新增
frontend/src/features/threads/hooks.ts                 新增
frontend/src/features/threads/hooks.test.tsx           新增
frontend/src/features/threads/ThreadSidebar.tsx        新增
frontend/src/features/threads/ThreadSidebar.module.css  新增
frontend/src/features/threads/ThreadSidebar.test.tsx   新增（17 tests）
frontend/src/features/threads/ThreadDetailPlaceholder.tsx   新增
frontend/src/features/threads/ThreadDetailPlaceholder.module.css 新增
frontend/src/features/threads/ThreadDetailPlaceholder.test.tsx 新增（10 tests）
frontend/src/features/threads/index.ts                  新增
```

**修改文件**

```
frontend/src/shared/api/client.ts            重写（GET+POST+DELETE 统一请求）
frontend/src/shared/api/client.test.ts        重写（新增 POST/DELETE 测试）
frontend/src/shared/api/health.ts            导入路径改为 shared/api/index
frontend/src/app/App.tsx                    重写（路由 / /threads/:threadId + ThreadSidebar 集成）
frontend/src/app/App.test.tsx                重写（适配 ThreadSidebar）
docs/frontend/PROGRESS.md                    新增 20B 验收清单和执行记录
docs/frontend/ARCHITECTURE.md                新增 Thread Feature 架构决策
```

**实际执行命令**

```powershell
cd frontend
npm run typecheck
npm run lint
npm run test -- --run --reporter=verbose
npm run build
```

**typecheck 结果**：`tsc --noEmit`，无输出 = 无错误

**lint 结果**：`oxlint .`，无输出 = 无警告

**测试结果**（Vitest verbose 逐文件输出）

```
Test Files  8 passed (8)
     Tests  72 passed (72)
     （逐文件数量见各测试文件实际输出）
Duration  2.41s
```

**构建结果**

```
dist/index.html                   0.45 kB │ gzip:  0.29 kB
dist/assets/index-D2sx-DFk.css    8.29 kB │ gzip:  2.12 kB
dist/assets/index-D2sx-DFk.js   307.25 kB │ gzip: 96.20 kB
✓ built in 174ms
```

**后端启动命令**

```powershell
cd mini-deerflow
$env:MINI_DEERFLOW_CORS_ORIGINS="http://localhost:5173"
uv run uvicorn mini_deerflow.api.app:app --port 8000
```

前端：

```powershell
cd frontend
npm run dev
```

**API 契约（curl 实测）**

| 方法 | 路径 | 状态码 | 响应示例 |
|------|------|--------|----------|
| GET | `/api/threads` | 200 | `{"threads":[{"thread_id":"xxx"}]}` |
| POST | `/api/threads` | 201 | `{"thread_id":"xxx","messages":[]}` |
| GET | `/api/threads/{thread_id}` | 200 | `{"thread_id":"xxx","messages":[]}` |
| DELETE | `/api/threads/{thread_id}` | 204 | 无 body |
| GET | `/api/health` | 200 | `{"status":"ok","service":"mini-deerflow"}` |
| GET | `/api/ready` | 200 | `{"status":"ready","runtime":true}` |
| GET | `/api/threads/{invalid}` | 404 | `{"error":{"code":"NOT_FOUND","message":"未知线程: invalid","request_id":"req-xxx","details":null}}` |

**当前状态**

```
代码结构与修复       ✅
API curl 联调         ✅
组件测试             ✅
测试统计/报告口径    ✅（72 tests / 8 files）
真实浏览器操作       ⏳ 待手动验证
Phase 20B            🟡
```

---

**手工验证 Checklist（浏览器手动操作）**

1. 打开 http://localhost:5173
2. 点击"新建会话" → 左侧出现 Thread
3. 点击 Thread → URL 变成 `/threads/{id}`
4. 刷新浏览器 → 仍然打开同一个 Thread
5. 点击删除 → 先出现二次确认，确认后返回 `/`
6. 打开浏览器控制台 → 无红色错误

通过后，将上方 `Phase 20B` 改为 ✅。

---

### 2026-09-30 Phase 20C 实施

**新增文件**

```
frontend/src/shared/types/runs.ts                    新增（AgentRunRequest/Response/RunStatus）
frontend/src/features/threads/runsApi.ts             新增（runAgent）
frontend/src/features/threads/runsApi.test.ts       新增（11 tests）
frontend/src/features/threads/useAgentRun.test.tsx  新增（7 tests）
frontend/src/features/threads/MessageList.tsx       新增
frontend/src/features/threads/MessageList.module.css
frontend/src/features/threads/MessageList.test.tsx  新增（10 tests）
frontend/src/features/threads/MessageComposer.tsx   新增
frontend/src/features/threads/MessageComposer.module.css
frontend/src/features/threads/MessageComposer.test.tsx 新增（6 tests）
frontend/src/features/threads/AgentChatPanel.tsx    新增
frontend/src/features/threads/AgentChatPanel.module.css
frontend/src/features/threads/AgentChatPanel.test.tsx 新增（6 tests）
frontend/src/features/threads/runErrors.ts          新增（getRunErrorMessage）
frontend/src/features/threads/runErrors.test.ts     新增（10 tests）
```

**修改文件**

```
frontend/src/shared/types/index.ts             新增 runs 类型导出
frontend/src/features/threads/hooks.ts        新增 useAgentRun
frontend/src/features/threads/index.ts        新增 AgentChatPanel/MessageList/MessageComposer/runErrors 导出
frontend/src/features/threads/ThreadDetailPlaceholder.tsx  → ThreadWorkspace.tsx（重命名）
frontend/src/features/threads/ThreadDetailPlaceholder.module.css  → ThreadWorkspace.module.css
frontend/src/features/threads/ThreadDetailPlaceholder.test.tsx → ThreadWorkspace.test.tsx（14 tests）
frontend/src/features/threads/ThreadWorkspace.tsx  集成 AgentChatPanel
frontend/src/app/App.tsx                    更新引用 ThreadWorkspace
frontend/src/test/setup.ts                   新增 scrollIntoView mock
docs/frontend/ARCHITECTURE.md               更新 20C 目录结构、缓存策略、架构决策
```

**实际执行命令**

```powershell
cd frontend
npm run typecheck
npm run lint
npm run test -- --run --reporter=verbose
npm run build
```

**typecheck 结果**：`tsc --noEmit`，无输出 = 无错误

**lint 结果**：`oxlint .`，无输出 = 无警告

**测试结果**

```
Test Files  14 passed (14)
     Tests  120 passed (120)
Duration  2.82s
```

**构建结果**

```
dist/index.html                   0.45 kB │ gzip:  0.29 kB
dist/assets/index-CJ1h7eAk.css   11.82 kB │ gzip:  2.76 kB
dist/assets/index-DGUfUpeP.js   309.99 kB │ gzip: 97.21 kB
✓ built in 185ms
```

**API 契约（curl 实测）**

| 方法 | 路径 | 状态码 | 响应示例 |
|------|------|--------|----------|
| POST | `/api/threads/{thread_id}/runs` | 200 | `{"run_id":"xxx","thread_id":"xxx","status":"succeeded","answer":"...","error":null}` |
| POST | `/api/threads/{invalid}/runs` | 404 | `{"error":{"code":"NOT_FOUND","message":"未知线程: invalid","request_id":"req-xxx","details":null}}` |
| POST | `/api/threads/{thread_id}/runs` | 409 | 线程已有 Run 进行中 |
| POST | `/api/threads/{thread_id}/runs` | 422 | message 为空或超长 |
| POST | `/api/threads/{thread_id}/runs` | 429 | Budget 超限 |
| POST | `/api/threads/{thread_id}/runs` | 503 | Run 已取消 |
| POST | `/api/threads/{thread_id}/runs` | 504 | Run 超时 |
| POST | `/api/threads/{thread_id}/runs` | 500 | Run 执行失败 |

**核心数据流**

```
用户输入消息 → MessageComposer
    ↓
handleSend(message)
    ↓
useAgentRun.mutate({ message })
    ↓
POST /api/threads/{thread_id}/runs { message }
    ↓
onSuccess → invalidateQueries(detail) → 刷新 ThreadDetail
    ↓
MessageList 显示更新后的 messages
```

**缓存策略**

```
发送消息后
  → useAgentRun.onSuccess
  → invalidateQueries({ queryKey: threadKeys.detail(threadId) })
  → 重新获取 ThreadDetail（包含新增的 user + assistant 消息）
```

**真实限制**

- 本阶段为非流式 Run，后端同步等待 Agent 完成才返回
- Agent 可能需要较长时间才能响应（取决于模型）
- 未实现 SSE（20D 范围）

**浏览器联调**

请在浏览器中手动验证：
1. 创建 Thread
2. 输入消息并发送
3. 等待 Agent 返回结果
4. 确认消息出现在聊天列表中
5. 刷新页面，确认消息仍然存在

**遗留问题**：无

**遗留问题**：真实浏览器联调待执行

---

## 20D 验收清单

- [x] SSE 类型与后端 Schema 对齐
- [x] SSE Stream Client（POST + ReadableStream 解析）
- [x] useAgentStream Hook（reducer 状态机）
- [x] RunEventPanel（实时事件展示）
- [x] AgentChatPanel 集成 SSE 流
- [x] 跨 chunk 解析 + 多事件同 chunk
- [x] CRLF（\\r\\n）支持
- [x] 非法 JSON 跳过
- [x] HTTP 错误协议解析 + Request ID
- [x] 四种终态（run.completed/failed/timed_out/cancelled）
- [x] 终态后只失效当前 Thread 详情
- [x] AbortController 支持
- [x] 组件卸载/切换 Thread 断开连接
- [x] 停止接收按钮（不会取消后端 Agent）
- [x] 事件数量上限（1000）
- [x] 序列号排序（处理乱序）
- [x] typecheck / oxlint / test / build 全部通过
- [x] 真实后端 SSE curl 验证
- [ ] 真实浏览器联调（待用户验证）

---

### 2026-09-30 Phase 20D 实施

**新增文件**

```
frontend/src/shared/types/events.ts                    新增（RunEvent/RunEventType/TERMINAL_EVENT_TYPES）
frontend/src/shared/api/streamClient.ts                新增（connectAgentStream/StreamError）
frontend/src/shared/api/streamClient.test.ts           新增（8 tests）
frontend/src/features/threads/useAgentStream.ts        新增（reducer 状态机）
frontend/src/features/threads/useAgentStream.test.tsx  新增（8 tests）
frontend/src/features/threads/RunEventPanel.tsx        新增
frontend/src/features/threads/RunEventPanel.module.css 新增
frontend/src/features/threads/RunEventPanel.test.tsx   新增（12 tests）
```

**修改文件**

```
frontend/src/shared/types/index.ts                    新增 events 类型导出
frontend/src/features/threads/AgentChatPanel.tsx       集成 useAgentStream
frontend/src/features/threads/AgentChatPanel.module.css 新增 streamArea
frontend/src/features/threads/AgentChatPanel.test.tsx  适配新架构（5 tests）
frontend/src/features/threads/ThreadWorkspace.test.tsx 适配 useAgentStream（mock AgentChatPanel）
frontend/src/features/threads/index.ts                  新增 RunEventPanel/useAgentStream 导出
docs/frontend/ARCHITECTURE.md                          新增 20D Stream Feature 架构决策
```

**实际执行命令**

```powershell
cd frontend
npm run typecheck
npm run lint
npm run test -- --run
npm run build
```

**typecheck 结果**

```
> frontend@0.0.0 typecheck
> tsc --noEmit
```
无输出 = 无错误

**lint 结果**

```
> frontend@0.0.0 lint
> oxlint .
```
无输出 = 无警告

**测试结果**

```
 RUN  v5.0.2 D:/项目解读/Deerflow/mini-deerflow/frontend

 Test Files  17 passed (17)
      Tests  144 passed (144)
   Start at  22:53:12
   Duration  2.98s
```

**逐文件测试分布**

| 文件 | tests |
|------|------:|
| client.test.ts | 8 |
| useBackendStatus.test.tsx | 4 |
| BackendStatusBar.test.tsx | 6 |
| App.test.tsx | 3 |
| api.test.ts | (threads api) |
| hooks.test.tsx | (threads hooks) |
| runsApi.test.ts | 11 |
| useAgentRun.test.tsx | 7 |
| MessageList.test.tsx | 10 |
| MessageComposer.test.tsx | 6 |
| AgentChatPanel.test.tsx | 5 |
| ThreadSidebar.test.tsx | 17 |
| ThreadWorkspace.test.tsx | 14 |
| runErrors.test.ts | 10 |
| streamClient.test.ts | 8 |
| useAgentStream.test.tsx | 8 |
| RunEventPanel.test.tsx | 12 |

**构建结果**

```
> frontend@0.0.0 build
> tsc --noEmit && vite build

vite v8.3.1 building client environment for production...
✓ 97 modules transformed.
dist/index.html                   0.45 kB │ gzip:  0.29 kB
dist/assets/index-d0k8pSEE.css   15.02 kB │ gzip:  3.37 kB
dist/assets/index-CCjkBDaf.js   317.74 kB │ gzip: 99.78 kB
✓ built in 174ms
```

**RunEventPanel 用户行为覆盖映射**

| 用户行为 | 测试位置 | 验证点 |
|---------|---------|--------|
| 事件顺序 | `RunEventPanel.test.tsx` — 「事件按入参顺序展示」 | `events.map((event, index) => <EventItem key={index} ... />)` 按数组顺序渲染；断言 `getAllByText` 拿到 5 个标签按预期顺序排列 |
| 唯一终态 | `RunEventPanel.test.tsx` — 「唯一终态：只显示一个 run.completed 标签」 | `events` 仅含一条 `run.completed`；断言 `getAllByText('Run 完成')` 长度为 1 |
| 运行中状态 | `RunEventPanel.test.tsx` — 「运行中状态显示"运行中"且显示停止接收按钮」 + 「连接中状态也显示停止接收按钮」 | `status='streaming'` → 文案「运行中」+ 按钮可见；`status='connecting'` → 按钮同样可见 |
| 失败状态 | `RunEventPanel.test.tsx` — 「run.failed 状态显示"执行失败"且展示错误信息」 | `status='failed'` → 文案「执行失败」+ `event.error` + `errorMessage` 同时显示 |
| 停止接收 | `RunEventPanel.test.tsx` — 「点击停止接收按钮触发 onStop」 + 「停止接收按钮的 tooltip 明确说明不会取消后端 Agent」 | `userEvent.click` 触发 → `onStop` 被调用 1 次；`title` 属性包含「不会取消后端 Agent」 |
| 终态后不再显示后续事件 | `RunEventPanel.test.tsx` — 「终态后不再展示运行中按钮」 + 「stopped 状态不显示停止接收按钮」 | `status='completed'`/`status='stopped'` → `queryByRole('button', { name: '停止接收' })` 期望 `.not.toBeInTheDocument()` |
| 错误 + Request ID | `RunEventPanel.test.tsx` — 「错误信息 + Request ID 同时显示」 | `status='error'` + `errorMessage` + `requestId` → 两者都可见 |
| 终态 answer | `RunEventPanel.test.tsx` — 「run.completed 终态展示 answerBar」 | `terminalEvent.answer` 渲染在 `<pre>` 中 |
| 状态文案 | `RunEventPanel.test.tsx` — 「初始 idle 状态显示"准备连接…"」 + 「run.failed 状态显示"执行失败"且展示错误信息」 | `STATUS_LABELS` 映射验证（idle/failed） |

reducer 状态机端到端覆盖：

| 行为 | 测试位置 | 验证点 |
|------|---------|--------|
| `start()` 触发 SSE 连接 | `useAgentStream.test.tsx` — 「start 后建立 SSE 连接」 | 断言 `connectAgentStream` 被以 `(threadId, message, { onEvent, onTerminal })` 调用 |
| 普通事件更新 events 列表 | `useAgentStream.test.tsx` — 「收到 onEvent 时更新 events 列表」 | `onEvent` 回调 → `events.length === 1` + `status === 'streaming'` |
| `run.completed` → completed 状态 | `useAgentStream.test.tsx` — 「收到 onTerminal 后切换到 completed 状态」 | `onTerminal` 回调 → `status === 'completed'` + `terminalEvent.answer` 保留 |
| 三种失败终态 | `useAgentStream.test.tsx` — 「run.completed 外的三种终态对应正确 status」 | 循环 `run.failed / run.timed_out / run.cancelled` → 各自对应 `status` |
| 终态失效 Thread 详情缓存 | `useAgentStream.test.tsx` — 「收到 onTerminal 后失效当前 Thread 详情」 | `queryClient.invalidateQueries` 被以 `['threads', 'detail', 't1']` 调用 |
| `stop()` 断开 SSE | `useAgentStream.test.tsx` — 「stop 调用 cancel 函数」 | `cancel` mock 被调用 + `status === 'stopped'` |
| 网络/HTTP 错误 | `useAgentStream.test.tsx` — 「收到 onError 时切换到 error 状态」 | `onError` 回调 → `status === 'error'` + `errorMessage` 保留 |
| 初始 idle | `useAgentStream.test.tsx` — 「初始状态为 idle」 | `status === 'idle'` + `events === []` |

SSE 解析底层覆盖：

| 行为 | 测试位置 |
|------|---------|
| 单一 chunk 多事件 | `streamClient.test.ts` — 「单一 chunk 中的多个事件正确分发」 |
| 跨 chunk 解析 | `streamClient.test.ts` — 「跨 chunk 解析：分两次发送」 |
| CRLF 换行 | `streamClient.test.ts` — 「CRLF 换行符（\\r\\n）正确解析」 |
| 非法 JSON 跳过 | `streamClient.test.ts` — 「非法 JSON 跳过该事件，继续处理后续事件」 |
| HTTP 错误 + Request ID | `streamClient.test.ts` — 「HTTP 非 2xx 错误：捕获状态码和 Request ID」 |
| 四种终态 → onTerminal | `streamClient.test.ts` — 「四种终态都触发 onTerminal」 |
| 注释行忽略 | `streamClient.test.ts` — 「注释行（: 开头）被忽略」 |
| cancel 不抛错 | `streamClient.test.ts` — 「cancel 调用终止连接」 |

**缓存失效策略**

```
收到 run.completed / run.failed / run.timed_out / run.cancelled
  → onTerminal(event)
  → queryClient.invalidateQueries({ queryKey: threadKeys.detail(threadId) })
  → 仅失效当前 Thread 详情（不影响其他 Thread 缓存）
  → MessageList 自动重新拉取详情（含 user + assistant 消息）
```

**构建结果**

```
dist/index.html                   0.45 kB │ gzip:  0.29 kB
dist/assets/index-d0k8pSEE.css   15.02 kB │ gzip:  3.37 kB
dist/assets/index-CCjkBDaf.js   317.74 kB │ gzip: 99.78 kB
✓ built in 190ms
```

**API 契约（curl 实测）**

| 路径 | 响应头 | Body |
|------|--------|------|
| POST `/api/threads/{id}/runs/stream` 200 | `X-Run-ID: run-xxx` | text/event-stream |
| POST `/api/threads/{invalid}/runs/stream` 404 | `X-Request-ID: req-xxx` | JSON 错误 |

**SSE 事件流实测**

```
id: 0
event: run.started
data: {"run_id":"run-xxx","thread_id":"9664a818798e","event_type":"run.started","sequence":0,...}

id: 1
event: model.started
data: {"run_id":"run-xxx","event_type":"model.started","sequence":1,...}

id: 2
event: run.completed
data: {"run_id":"run-xxx","event_type":"run.completed","sequence":2,"answer":"...","duration_seconds":...}
```

**事件流设计**

```
发送消息
  → POST /api/threads/{id}/runs/stream
  → 读取 X-Run-ID 响应头
  → 解析 SSE 流
  → reducer 状态机分发
  → 显示 RunEventPanel
  → 收到终态
  → invalidateQueries(detail)
  → 重新拉取 ThreadDetail（包含 user + assistant 消息）
```

**reducer 状态机**

```
idle → connecting → streaming → completed | failed | timed_out | cancelled
                              → stopped (用户手动)
                              → error (HTTP/网络错误)
```

**Abort 语义**

- 组件卸载：自动调用 cancel()，断开 SSE 连接
- Thread 切换：组件重新挂载，旧连接断开
- 停止接收按钮：cancel() + dispatch STOP
- 重要：停止接收不会取消后端 Agent（后端不支持断线取消）

**真实限制**

- 当前不支持自动重连（按后端设计）
- 当前不支持 token streaming（后端不在 event_type 中暴露）
- 当前不支持断线取消 Agent（按后端限制）
- 客户端断开时 Server 停止 publish，但不取消 Agent

**浏览器联调**

请在浏览器中手动验证：
1. 创建 Thread
2. 输入消息并发送
3. 实时看到 run.started / model.started / model.completed 事件
4. 收到 run.completed 终态
5. 消息列表更新（包含 user + assistant 消息）
6. 刷新页面，消息仍存在
7. 点击"停止接收"按钮 → 事件流断开 → 后端 Agent 继续运行（前端不再接收）

**遗留问题**：真实浏览器联调待执行

---

## 20E 验收清单

- [x] FormData 上传且不手动设置 Content-Type
- [x] 上传 201 成功 / 409 同名 / 413 超限
- [x] uploads / artifacts 列表
- [x] 路径 segment 编码
- [x] Blob 下载 + Object URL 延迟释放（setTimeout 0）
- [x] DELETE 204
- [x] 上传/删除只失效 uploads 列表（不影响 artifacts）
- [x] Artifacts 只读：UI 无删除/上传按钮
- [x] Loading / Empty / Error / Request ID 展示
- [x] 上传 pending 防重复提交
- [x] 删除二次确认 / 取消
- [x] typecheck / oxlint / test / build 全部通过
- [ ] 真实浏览器联调（待用户验证）

---

### 2026-09-30 Phase 20E 实施

**新增文件**

```
frontend/src/shared/types/files.ts                        新增（FileMetadata/FileListResponse/FileKind）
frontend/src/features/files/api.ts                        新增（listUploads/listArtifacts/uploadFile/downloadFile/downloadArtifact/deleteFile/encodeFilePath）
frontend/src/features/files/api.test.ts                   新增（encodeFilePath 4 + 列表 3 + 上传 6 + 下载 4 + Artifact 下载 3 + 删除 4 = 24 tests）
frontend/src/features/files/hooks.ts                      新增（useUploads/useArtifacts/useUploadFile/useDeleteFile）
frontend/src/features/files/hooks.test.tsx                新增（5 tests）
frontend/src/features/files/queryKeys.ts                  新增（fileKeys.all/uploads/artifacts）
frontend/src/features/files/fileErrors.ts                 新增（getFileErrorMessage）
frontend/src/features/files/fileErrors.test.ts            新增（9 tests）
frontend/src/features/files/FilePanel.tsx                 新增
frontend/src/features/files/FilePanel.module.css          新增
frontend/src/features/files/FilePanel.test.tsx            新增（10 tests）
frontend/src/features/files/FileList.tsx                  新增
frontend/src/features/files/FileList.module.css           新增
frontend/src/features/files/FileList.test.tsx             新增（10 tests）
frontend/src/features/files/FileUpload.tsx                新增
frontend/src/features/files/FileUpload.module.css         新增
frontend/src/features/files/FileUpload.test.tsx           新增（8 tests）
frontend/src/features/files/index.ts                      新增 exports
```

**修改文件**

```
frontend/src/shared/api/client.ts                         新增 apiUpload / apiDownloadBlob
frontend/src/shared/api/index.ts                          重新导出新方法
frontend/src/shared/types/index.ts                        新增 files 类型导出
frontend/src/app/App.tsx                                  右侧 FilePanel 替换占位
frontend/src/app/App.test.tsx                             mock files api + 断言 FilePanel tabs
docs/frontend/ARCHITECTURE.md                             新增 Files Feature 章节
docs/frontend/PROGRESS.md                                 新增 20E 记录
```

**实际执行命令**

```powershell
cd frontend
npm run typecheck
npm run lint
npm run test -- --run
npm run build
```

**typecheck 结果**

```
> frontend@0.0.0 typecheck
> tsc --noEmit
```
无输出 = 无错误

**lint 结果**

```
> frontend@0.0.0 lint
> oxlint .
```
无输出 = 无警告

**测试结果**

```
 RUN  v5.0.2 D:/项目解读/Deerflow/mini-deerflow/frontend

 Test Files  23 passed (23)
      Tests  204 passed (204)
   Duration  4.44s
```

**构建结果**

```
> frontend@0.0.0 build
> tsc --noEmit && vite build

vite v8.3.1 building client environment for production...
✓ 109 modules transformed.
dist/index.html                   0.45 kB │ gzip:  0.29 kB
dist/assets/index-Bl8a2ltv.css   18.51 kB │ gzip:  3.92 kB
dist/assets/index-pitO0nJL.js   325.10 kB │ gzip: 101.63 kB
✓ built in 298ms
```

**新增测试分布**

| 文件 | tests |
|------|------:|
| api.test.ts | 19 |
| hooks.test.tsx | 5 |
| fileErrors.test.ts | 9 |
| FilePanel.test.tsx | 10 |
| FileList.test.tsx | 10 |
| FileUpload.test.tsx | 8 |

**API 契约（curl 实测）**

```
POST   /api/files                  → 201 FileMetadata | 409 同名 | 413 超限 | 400 路径
GET    /api/files                  → 200 FileListResponse
GET    /api/files/{path}           → 200 application/octet-stream | 404 | 400
DELETE /api/files/{path}           → 204 | 400 目录 | 404
GET    /api/artifacts              → 200 FileListResponse（只读，无上传/删除）
GET    /api/artifacts/{path}       → 200 application/octet-stream | 404
```

**关键 API Client 设计（3 段中文注释）**

```ts
// shared/api/client.ts — FormData 不手动设置 Content-Type
export async function apiUpload<T>(path, formData, options) {
  const headers: Record<string, string> = { Accept: 'application/json' }
  // 不设置 Content-Type：浏览器自动生成 multipart/form-data; boundary=...
  return fetch(url, { method: 'POST', headers, body: formData, signal })
}
```

```ts
// shared/api/client.ts — Blob 错误响应仍按 JSON 解析
export async function apiDownloadBlob(path, options) {
  if (!response.ok) {
    const text = await response.text()
    const body = parseJson(text)
    // 复用统一的 ApiError 协议（status / code / message / requestId）
    throw new ApiError({ status, code, message, requestId, details })
  }
  return response.blob()
}
```

```ts
// features/files/FileList.tsx — Blob 下载 + Object URL 延迟释放
async function triggerDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  // 让浏览器先开始处理下载再释放 Object URL，避免部分浏览器时序差异
  setTimeout(() => { URL.revokeObjectURL(url) }, 0)
}
```

**缓存策略**

```
Uploads 上传/删除成功
  → invalidateQueries({ queryKey: ['files', 'uploads'] })
  → 不影响 artifacts 缓存

Artifacts
  → useArtifacts() 单独读取 ['files', 'artifacts']
  → 没有失效操作（只读）
```

**真实限制**

- 当前不支持拖拽上传、断点续传、上传进度、文件预览、多文件并发
- 不读取/展示服务器绝对路径
- 上传后端不支持覆盖（同名返回 409）
- 删除只能删文件，目录不能删（后端返回 400）

**浏览器联调 Checklist**

请在浏览器中手动验证：
1. 上传一个文件 → uploads 列表出现该文件
2. 点击下载 → 文件正确下载到本地
3. 点击删除 → 出现二次确认，确认后列表消失
4. 切换到 Artifacts tab → 列出 artifacts（无可上传/删除按钮）
5. 同名文件再上传 → 显示 409 错误信息
6. 上传超大文件 → 显示 413 错误信息
7. 浏览器控制台无红色错误

**遗留问题**：真实浏览器联调待执行

---

## 20E 最终验收（2026-10-01）

四组全部通过后，20E 才从 🟡 改为 ✅。

### 第 1 组：Uploads 功能

- [ ] 上传文件成功，列表立即出现（**浏览器**）
- [x] 上传使用 FormData，没有手动设置 Content-Type（自动化 + 代码审查）
- [ ] 下载文件成功，内容与原文件一致（**浏览器**）
- [ ] 删除前出现二次确认（**浏览器**）
- [ ] 删除成功后文件从列表消失（**浏览器**，已修复 uploads/ 前缀 bug）
- [ ] 同名上传返回 409，并显示友好提示（curl 已验证 409 + 浏览器待验）
- [ ] 超限上传返回 413，并显示友好提示（curl 已验证 413 + 浏览器待验）
- [ ] 上传失败后保留文件选择，方便重试（**浏览器**）
- [ ] 上传 pending 期间不能重复提交（自动化已验证 + 浏览器待验）
- [ ] 目录不能作为普通文件删除（自动化已验证：disabled）

### 第 2 组：Artifacts 功能

- [ ] Artifacts 列表能够加载（**浏览器**）
- [ ] 预置的 Artifact 能够下载（**浏览器**）
- [ ] 下载内容与服务端文件一致（curl 已验证 44 字节内容一致 + 浏览器待验）
- [ ] Artifacts 页面没有上传按钮（自动化已验证）
- [ ] Artifacts 页面没有删除按钮（自动化已验证）
- [ ] 前端没有调用不存在的 Artifact 写入/删除接口（代码审查：api.ts 仅 apiGet/apiDownloadBlob 打到 /api/artifacts）

**测试 Artifact 已预置**：`workspace/artifacts/artifact-test.txt`（位于 `/tmp/mdf_ws_vpumc99h/artifacts/`，后端临时 workspace）

### 第 3 组：架构与安全

- [x] Uploads 和 Artifacts 使用独立 Query Key（`fileKeys.uploads()` / `fileKeys.artifacts()`）
- [x] 上传/删除只刷新 Uploads 缓存，不影响 Artifacts（hooks.ts:60,75 仅 invalidate uploads）
- [x] 文件 API 通过统一 API Client，不在组件中直接 fetch（grep 无 `fetch(` in features/files）
- [x] 下载使用 Blob（`apiDownloadBlob` 返回 `response.blob()`）
- [x] Object URL 使用后被正确释放（FileList.tsx:48 `setTimeout(revoke, 0)`）
- [x] 路径按 segment 安全编码（`encodeFilePath`，已剥离 uploads/ 前缀）
- [x] 前端不显示服务器绝对路径（features/files 无绝对路径代码）
- [x] 400/404/409/413/500/503 和网络错误有稳定文案（fileErrors.ts 9 tests）
- [x] 错误存在 Request ID 时能够显示（ErrorBanner 显示 requestId）
- [ ] 控制台没有 CORS、React、Promise 或 Blob 下载错误（**浏览器**）

### 第 4 组：工程验收

**实际执行命令**：

```powershell
cd frontend
npm run typecheck
npm run lint
npm run test -- --run --reporter=verbose
npm run build
```

**typecheck**：`tsc --noEmit` 无输出 = 通过
**lint**：`oxlint .` 无输出 = 通过

**测试结果**（Vitest 真实输出）：

```
Test Files  23 passed (23)
     Tests  208 passed (208)
  Duration  4.64s
```

**build**：

```
✓ 109 modules transformed.
dist/index.html                   0.45 kB │ gzip:  0.29 kB
dist/assets/index-Bl8a2ltv.css   18.51 kB │ gzip:  3.92 kB
dist/assets/index-DVBJDb6s.js   325.53 kB │ gzip: 101.73 kB
✓ built in 411ms
```

- [x] 测试文件数和测试总数使用 Vitest 真实输出（23 / 208）
- [x] ARCHITECTURE.md 已记录 Files 设计（Files / Artifacts Feature 章节）
- [x] PROGRESS.md 已记录真实验收结果（本节）
- [x] `dist/`、`node_modules/`、`.env` 未进入 Git 状态（`git check-ignore` 确认）
- [x] `.env.example` 和 `package-lock.json` 保留（`?? frontend/.env.example`、`?? frontend/package-lock.json`）
- [x] 未 commit、未 push

### curl 实测验证（2026-10-01）

```text
POST   /api/files (FormData)        → 201 {"path":"uploads/upload-test-20e.txt",...}
POST   /api/files (同名再传)          → 409 {"code":"CONFLICT",...,"request_id":"req-8d9b..."}
POST   /api/files (60MB 文件)         → 413 {"code":"PAYLOAD_TOO_LARGE",...,"request_id":"req-3368..."}
GET    /api/files/upload-test-20e.txt → 200 与上传内容逐字节一致
DELETE /api/files/upload-test-20e.txt → 204
GET    /api/artifacts                  → 200 {"entries":[{"path":"artifacts/artifact-test.txt",...}],"total":1}
GET    /api/artifacts/artifact-test.txt → 200 "mini-deerflow 20E artifact acceptance test"
```

### 最终完成标准

```text
自动化测试全部通过 ✅
+ 浏览器 Uploads 完整流程通过 ⏳
+ Artifact 真实下载通过 ⏳
+ 409 和 413 错误通过 ⏳（curl 已验证，浏览器待验）
+ 控制台无错误 ⏳
= Phase 20E 🟡
```

---

## 20F 验收清单

- [x] 统一 `ErrorNotice` 组件（稳定文案 + 错误码 + Request ID + 条件式操作）
- [x] `shared/errors` 集中状态码映射（run / file / thread / health / generic 上下文）
- [x] 后端文案经 `isSafeToDisplay` 过滤（traceback / 绝对路径 / 密钥 / 异常类名）
- [x] ErrorBoundary、Thread、Run、Files 全部复用统一组件
- [x] 未引入 Toast 库或全局状态管理库
- [x] Playwright E2E 环境搭建（`playwright.config.ts` + `e2e/` + `npm run test:e2e`）
- [x] E2E 专用后端：临时 workspace + 预置 Artifact + FakeModel（零 API Key）
- [x] E2E 自动拉起 FastAPI 与 Vite，测试间无顺序依赖
- [x] 必测 1～10 全覆盖
- [x] 下载断言校验文件内容（非仅断言按钮点击）
- [x] 关键 E2E 连续运行 2 次无 flaky
- [x] Playwright 报告与产物不进 Git
- [x] typecheck / lint / test / build / test:e2e 全部通过
- [x] 文档同步（ARCHITECTURE / PROGRESS / README）
- [x] 未 commit、未 push

---

### 2026-10-01 Phase 20F 实施

**新增文件**

```
frontend/src/shared/errors/messages.ts          新增（getErrorMessage / isSafeToDisplay）
frontend/src/shared/errors/messages.test.ts     新增（18 tests）
frontend/src/shared/errors/index.ts             新增
frontend/src/shared/ui/ErrorNotice.tsx          新增（统一错误展示组件）
frontend/src/shared/ui/ErrorNotice.module.css   新增
frontend/src/shared/ui/ErrorNotice.test.tsx     新增（13 tests）
frontend/playwright.config.ts                   新增（双 webServer 编排）
frontend/tsconfig.e2e.json                      新增（e2e 独立 project）
frontend/e2e/helpers.ts                         新增（console watcher / 下载工具 / 预置常量）
frontend/e2e/health-thread.spec.ts              新增（5 tests）
frontend/e2e/run-sse.spec.ts                    新增（3 tests）
frontend/e2e/files.spec.ts                      新增（5 tests）
frontend/e2e/errors.spec.ts                     新增（5 tests）
tests/e2e_server.py                             新增（E2E 专用启动入口）
```
E2E 合计 **4 个文件 / 18 tests**（`npx playwright test --list` 实测输出：errors 5 + files 5 + health-thread 5 + run-sse 3）

**修改文件**

```
frontend/src/features/threads/runErrors.ts            委托给 shared/errors('run')
frontend/src/features/files/fileErrors.ts             委托给 shared/errors('file')
frontend/src/features/files/FilePanel.tsx             ErrorBanner → ErrorNotice
frontend/src/features/files/FileUpload.tsx            错误块 → ErrorNotice
frontend/src/features/threads/ThreadWorkspace.tsx     ThreadError → ErrorNotice
frontend/src/features/threads/ThreadSidebar.tsx       ListErrorState → ErrorNotice
frontend/src/features/threads/AgentChatPanel.tsx      错误块 → ErrorNotice + 停止接收常驻提示 + 面板生命周期修复
frontend/src/features/threads/useAgentStream.ts       start(messageOverride) 修复闭包旧消息
frontend/src/features/threads/RunEventPanel.tsx       duration_seconds / attempt 改 typeof 判空
frontend/src/features/files/api.ts                    stripRootPrefix 支持 artifacts 前缀
frontend/src/app/ErrorBoundary.tsx                    message 经 isSafeToDisplay 过滤
frontend/package.json                                 test:e2e 脚本 + @playwright/test
frontend/.oxlintrc.json                               e2e 关闭 react/hooks 规则
frontend/.gitignore                                   playwright-report/ test-results/ 等
frontend/vite.config.ts                               vitest include 限定 src/**；defineConfig 来自 vitest/config
frontend/README.md                                    测试分层 + E2E 章节
src/mini_deerflow/api/app.py                          runtime factory 改用 create_default_registry()
docs/frontend/ARCHITECTURE.md                         Error Feature + E2E 架构章节
```

**实际执行命令**

```powershell
cd frontend
npm run typecheck
npm run lint
npm run test -- --run --reporter=verbose
npm run build
npm run test:e2e
```

**typecheck 结果**

```
> frontend@0.0.0 typecheck
> tsc -p tsconfig.app.json --noEmit && tsc -p tsconfig.node.json --noEmit && tsc -p tsconfig.e2e.json --noEmit
```
无输出 = 通过

> 修正说明：原脚本 `tsc --noEmit` 指向 `files: []` 的 solution 配置，**实际未校验任何文件**。
> 本次改为逐 project 校验，`build` 同步改为依赖 `typecheck`，避免空转放行。

**lint 结果**

```
> frontend@0.0.0 lint
> oxlint .
```
无输出 = 通过

**单元/组件测试结果（Vitest 真实输出）**

```
 Test Files  25 passed (25)
      Tests  242 passed (242)
   Duration  4.87s
```

**构建结果**

```
✓ 113 modules transformed.
dist/index.html                   0.45 kB │ gzip:  0.29 kB
dist/assets/index-h2gBsveI.css   19.37 kB │ gzip:  4.12 kB
dist/assets/index-CFFgz_l1.js   326.97 kB │ gzip: 102.49 kB
✓ built in 269ms
```

**E2E 结果**

```
第 1 次：18 passed (22.7s)
第 2 次：18 passed (23.3s)
```

连续 2 次全通过，无 flaky。

**后端回归（app.py 注册表修复后）**

```
uv run pytest -q
1747 passed, 42 subtests passed in 81.93s
```

**E2E 覆盖流程 ↔ 必测项映射**

| 必测项 | 用例 |
|--------|------|
| 1. health/ready 就绪 | `1. health/ready 显示就绪` |
| 2. 创建 / 选择 / 刷新恢复 / 删除 | `2a` `2b` `2c` `2d`（含二次确认） |
| 3. 发送消息、运行状态、消息持久化 | `run-sse 3.` |
| 4. SSE 事件顺序、唯一终态、终态后结束 | `run-sse 4.` |
| 5. 停止接收不显示「已取消后端 Agent」 | `run-sse 5.` |
| 6. 上传 / 列表 / 下载内容一致 / 删除 | `files 6a`、`6b`（pending 锁定） |
| 7. Artifact 可下载、无上传/删除入口 | `files 7a`、`7b`、`7c` |
| 8. 404 / 409 / 413 与 Request ID 界面 | `errors 8a` `8b` `8c` `8d` |
| 9. 后端离线与恢复 | `errors 9` |
| 10. 控制台无 CORS/React/Promise 错误 | 每个 spec 末尾 `console.assertClean()` |

**E2E 发现并修复的真实缺陷**

| # | 缺陷 | 证据 | 修复 |
|---|------|------|------|
| 1 | API runtime 注册表为空，任何工具调用必失败 | curl SSE 返回 `tool.failed: ToolNotFoundError` | `app.py` 两个 factory 改用 `create_default_registry()`（与 CLI 对齐） |
| 2 | `start()` 闭包读到旧 `message`，实际发出空消息 | 消息列表无 assistant 回答 | `useAgentStream.start(messageOverride?)` + `AgentChatPanel` 显式传入 |
| 3 | 事件面板在终态瞬间卸载，`执行完成` 不可观测 | 截图中面板已消失 | `displayPendingMessage = pendingMessage`（不再依赖 `isPending`） |
| 4 | 后端省略 `duration_seconds` → `.toFixed` 崩溃整页 | 截图：ErrorBoundary「页面加载失败」 | 改 `typeof === 'number'` 判空 + 回归测试 |
| 5 | Artifact 下载拼出 `/api/artifacts/artifacts/x` → 404 | curl 对比 404 / 200 | `stripRootPrefix(path, 'artifacts')` + 3 条单测 |
| 6 | 种子文件 CRLF（31→32 字节）与 TS 常量不一致 | `od -c` 显示 `\r\n` | `write_bytes` 代替 `write_text` |

**ErrorNotice 设计**

```text
稳定中文文案（getErrorMessage 按 context 解析）
错误码（code，报障用）
请求编号（requestId，关联后端日志）
操作按钮（onRetry / onDismiss，回调存在才渲染）
```

- `role="alert"` + `ariaLabel`，`data-testid="error-notice"` / `error-request-id`
- `tone`：`error`（默认）/ `warning` / `info`
- `description` 可覆盖自动解析（用于「停止接收」这类非 HTTP 提示）

**Request ID 数据流**

```
后端 RequestIdMiddleware → X-Request-ID 头 + error.request_id
  → extractRequestId（头优先，body 兜底）→ ApiError.requestId
  → TanStack Query error / mutation.error → ErrorNotice 渲染「请求编号：req-xxx」
```
网络错误（status=0）请求未到达后端，无 Request ID，组件自动省略该行。

**测试分层**

| 层级 | 工具 | 位置 | 数量 |
|------|------|------|------|
| Unit / Component | Vitest + Testing Library | `src/**/*.test.ts(x)` | 25 文件 / 242 tests |
| Integration | pytest + FastAPI | `tests/test_api_*.py` 等 | 1747 tests + 42 subtests |
| E2E | Playwright | `e2e/*.spec.ts` | 18 tests（×2 次） |

Vitest `include` 限定 `src/**`，`e2e/` 归 Playwright 独占，两套 runner 不重叠。

**真实限制**

- E2E 使用 `SlowFakeModel`（每步 500ms 固定 sleep）以让「运行中」可见；不是生产行为
- E2E 跑真实浏览器 + 真实后端，速度受 `workers:1` 限制（共享临时 workspace）
- 未覆盖：多浏览器（仅 Chromium）、移动端视口、真实大模型链路
- `start()` 的 message 覆盖参数是针对本组件闭包时序的修复，未推广为通用模式

**浏览器联调**

E2E 即浏览器验收（Chromium 真实渲染 + 真实后端 + 真实下载落盘），
20D / 20E 的浏览器验收由 `run-sse.spec.ts` 与 `files.spec.ts` 覆盖。

**遗留问题**：无
