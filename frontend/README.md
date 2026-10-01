# Mini DeerFlow 前端

React + TypeScript + Vite 前端，与 FastAPI 后端分离运行。

## 快速启动

### 1. 后端（PowerShell）

```powershell
cd D:\项目解读\Deerflow\mini-deerflow
$env:MINI_DEERFLOW_CORS_ORIGINS="http://localhost:5173"
uv run uvicorn mini_deerflow.api.app:app --port 8000 --reload
```

### 2. 前端

```powershell
cd frontend
npm install
npm run dev
```

访问 http://localhost:5173

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `VITE_API_BASE_URL` | `http://127.0.0.1:8000` | 后端 API 地址 |

开发时使用 `.env` 文件：

```
VITE_API_BASE_URL=http://localhost:8000
```

## CORS 配置

后端默认拒绝所有跨域请求。开发时必须设置：

```powershell
$env:MINI_DEERFLOW_CORS_ORIGINS="http://localhost:5173"
```

生产环境请在 `.env` 中配置 `MINI_DEERFLOW_CORS_ORIGINS`。

## 可用脚本

| 命令 | 说明 |
|------|------|
| `npm run dev` | 开发服务器（HMR） |
| `npm run build` | 生产构建 |
| `npm run preview` | 预览生产构建 |
| `npm run typecheck` | TypeScript 类型检查（app / node / e2e 三个 project） |
| `npm run lint` | ESLint（oxlint）检查 |
| `npm run test` | 运行单元/组件测试（单次） |
| `npm run test:watch` | 测试（监听模式） |
| `npm run test:e2e` | 运行 Playwright 浏览器 E2E |

## 测试分层

| 层级 | 工具 | 位置 | 覆盖范围 |
|------|------|------|----------|
| Unit / Component | Vitest + Testing Library | `src/**/*.test.ts(x)` | 纯函数、Hook 状态机、组件渲染与交互 |
| E2E | Playwright | `e2e/*.spec.ts` | 真实浏览器串联 health → Thread → Run/SSE → Files |

`npm run test` 只收集 `src/**`；`e2e/` 归 Playwright 管，两套 runner 不重叠。

## E2E

### 首次准备

```powershell
cd frontend
npx playwright install chromium
```

### 运行

```powershell
npm run test:e2e
npm run test:e2e:ui     # 带 UI 的调试模式
```

Playwright 会自动拉起两个服务，无需手动启动：

| 服务 | 命令 | 端口 |
|------|------|------|
| E2E 专用后端 | `uv run python -m tests.e2e_server --port 8000` | 8000 |
| Vite dev server | `npm run dev` | 5173 |

两个服务都是 `reuseExistingServer: false`，每次运行都是干净环境，测试之间无顺序依赖。

### E2E 后端特性

`tests/e2e_server.py` 复用生产的 `create_app()` 与全部生产路由，只替换三项外部依赖：

| 依赖 | E2E 取值 | 原因 |
|------|----------|------|
| Workspace 根目录 | 系统临时目录 | 可预测、可清理，不污染开发数据 |
| 预置 Artifact | `artifacts/artifact-test.txt` | artifacts 只读视图需要一个可下载样本 |
| 模型 | `FakeModel`（`create_runtime` 默认值） | 纯确定性，**不需要任何 API Key** |

`FakeModel` 对输入 `计算 1+1` 的行为是确定的：返回 `calculator` 工具调用 → 工具结果回灌 → 最终回答 `计算结果: 2`。因此 E2E 稳定覆盖完整的 `run.started → model.* → tool.* → run.completed` 事件序列。

E2E 后端额外把 `max_upload_bytes` 调为 1 MiB，用于稳定复现 413。

### E2E 覆盖流程

| 文件 | 覆盖 |
|------|------|
| `e2e/health-thread.spec.ts` | health/ready 就绪、Thread 创建/选择/刷新恢复/删除（二次确认） |
| `e2e/run-sse.spec.ts` | Run 运行状态、SSE 事件顺序、唯一终态、终态后停止、停止接收文案、消息持久化 |
| `e2e/files.spec.ts` | 上传→列表→下载（校验内容）→删除、pending 防重复、Artifact 下载（校验内容）、Artifacts 无上传/删除入口、uploads/artifacts 缓存隔离 |
| `e2e/errors.spec.ts` | 404、409 + Request ID、413 + Request ID、错误不泄漏内部信息、后端离线与恢复 |

所有 spec 都通过 `console.assertClean()` 断言控制台没有 CORS / React / 未处理 Promise 错误。

### 报告与产物

以下路径已在 `.gitignore` 中，不会进入 Git：

```text
playwright-report/
test-results/
blob-report/
```

