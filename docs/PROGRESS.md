# Mini DeerFlow 总进度

状态图例：`⬜ 未开始` `🟡 进行中` `✅ 已完成` `⛔ 阻塞`

更新时间：2026-10-02

---

## 总体阶段

| 阶段 | 内容 | 状态 |
|------|------|------|
| 1–18 | Agent 核心、工具、记忆、Skill、Sandbox、Subagent、Middleware、Thread 状态 | ✅（历史阶段，见各专题文档） |
| 19 | FastAPI + SSE + 运行时集成 | ✅ |
| 20 | React 前端 + 浏览器 E2E | ✅（[frontend/PROGRESS.md](frontend/PROGRESS.md)） |
| 21A | 模型接入与统一 Model Factory | ✅（[phase-21-progress.md](phase-21-progress.md)） |
| 21B | 真实模型 ToolCall 闭环 | ✅ 定向 148 passed |
| 21C | 真实验收（2 轮，底层 3 次请求） | ✅ 纯文本 + 计算器闭环含 126 |
| 22 | MCP 扩展（本地 stdio） | ✅ 定向 46 passed + 本地 smoke |
| 23 | Docker Compose、持久化与最终验收 | ✅ 见下 |
| — | **Mini DeerFlow MVP** | **✅** 自动化 + Docker + 浏览器人工验收全部通过 |

---

## Phase 21B（真实模型 ToolCall 闭环）

数据流与边界见 [model-provider.md](model-provider.md)。

| 门禁 | 实测结果 |
|---|---|
| 定向测试 | `148 passed in 2.75s` |
| 完整 pytest | `1935 passed, 42 subtests passed in 99.55s` |
| `uv pip check` | `Checked 48 packages` / `All installed packages are compatible` |

定向组成：

| 文件 | tests |
|---|---:|
| `test_tool_loop_21b.py` | 29 |
| `test_phase21b_events.py` | 7 |
| `test_anthropic_compat_model.py` | 36 |
| `test_agent.py` | 15 |
| `test_model_config.py` | 36 |
| `test_model_factory.py` | 25 |
| **合计** | **148** |

覆盖：Schema 转换、tool_use/tool_result 往返、call_id 保留、非法参数、
未知工具、工具异常、多 `tool_use` 拒绝、tool→result→final 两轮闭环、
Budget/max_steps、失败不提交、FakeModel 回归、CLI/API/SSE 事件。

本轮修掉的两处真实缺口（都是「包装器丢 `tools` 导致真实模型看不到工具」）：

- `tests/e2e_server.py::SlowFakeModel` 不转发 `tools`
- `subagent_executor._BudgetAwareAgent` 不传 `tools`，且 tool 消息不带 `call_id`

---

## Phase 21C（真实模型收口）

| 项 | 结果 |
|---|---|
| 第 1 轮 纯文本 | `MIMO_TEXT_OK` |
| 第 2 轮 工具闭环 | `SSE: run.started → model.started → model.completed → tool.started → tool.completed → model.started → model.completed → run.completed` |
| 最终答案 | 含 **126**（`(17 + 25) * 3 = 126`） |
| 落库（**当次历史输出**） | `['user', 'tool', 'assistant']`；收口后为 `['user', 'assistant']`，见「最终收口」 |
| 底层模型请求 | 3（1 + 2）；重试 0（`MAX_RETRIES=0`） |
| Key 泄漏 | 无（脚本从未引用 `settings.api_key` 参与输出） |

命令：`env -u MIMO_API_KEY MINI_DEERFLOW_MODEL_MAX_RETRIES=0 uv run python scripts/phase21c_acceptance.py` → `EXIT=0`

> `env -u MIMO_API_KEY` 是必须的：shell 里残留的旧 Key（哈希前缀 `ff7286ee1ab8`）
> 与 `.env` 的 Key（`a4501a275a8e`）不是同一个值，按
> 「系统环境变量 > .env > 默认值」会把正确值挡住。

---

## Phase 22（MCP）

架构与安全边界见 [mcp.md](mcp.md)。

| 门禁 | 实测结果 |
|---|---|
| 定向测试 `tests/test_mcp.py` | `46 passed`（连跑 3 次全绿） |
| 完整 pytest | `1935 passed, 42 subtests passed` |
| `uv pip check` | 通过 |
| 本地 MCP 人工 smoke | `scripts/mcp_smoke.py` → `exit=0` |

smoke 实测：

```
[1/4] 发现 4 个 MCP 工具，ToolRegistry 总计 5 个工具（含 calculator）。
[2/4] add(17, 25) = 42   |  echo = MCP_SMOKE_OK
[3/4] 缺参被拒绝: ToolExecutionError
[4/4] 启动期间子进程 3 个，关闭后残留 0 个
```

本轮修掉的真实缺陷：

| 问题 | 处理 |
|---|---|
| 调用超时有两条路径（协议层 `read_timeout_seconds` 与外层 portal），同一语义有时抛 `TimeoutError`、有时抛 MCP 协议错误，**完整跑时随机失败** | 外层放宽 `+_TIMEOUT_BACKSTOP`，并把 `REQUEST_TIMEOUT` 归一为 `TimeoutError` |
| `tests/test_mcp.py` 引用了不存在的 `ContextPolicy` | 改为 `SubagentContextPolicy as ContextPolicy`（不改公共类名） |
| `mcp_errors.py` 里 5 个从未被引用的异常 | 删除，只保留公共基类 `MCPError` |

---

## Phase 23（Docker Compose、持久化、最终验收）

### 后端

| 门禁 | 实测结果 |
|---|---|
| Phase 21 定向 | `148 passed` |
| Phase 22 定向 | `46 passed` |
| Phase 23 定向 | `9 passed` |
| 完整 pytest | `1935 passed, 42 subtests passed in 99.55s` |
| `uv pip check` | `All installed packages are compatible` |

### 前端

| 门禁 | 实测结果 |
|---|---|
| `npm run typecheck` | `exit 0` |
| `npm run lint` | `exit 0` |
| `npm run test -- --run` | `Test Files 25 passed (25)` / `Tests 242 passed (242)` |
| `npm run build` | `exit 0`（326.97 kB JS / 19.37 kB CSS） |

### E2E

| 轮次 | 结果 |
|---|---|
| 收口前（端口被占用时） | `18 passed (24.8s)` / `18 passed (24.3s)` |
| **最终收口复跑（默认端口 8000/5173）** | `18 passed (26.0s)` / `18 passed (24.2s)` |

命令：`npm run test:e2e`（本机 dev server 已停止，直接用默认端口）

> 端口**可覆盖**：开发机的 8000/5173 可能被 dev server 占用，而 Playwright
> `reuseExistingServer:false` 撞端口会直接起不来。为此把两个端口做成了
> 环境变量（**默认值不变**），并给 vite 显式传 `--port`；
> `tests/e2e_server.py` 的 CORS 白名单也跟随 `E2E_FRONTEND_PORT`。

### Docker

| 项 | 结果 |
|---|---|
| `docker compose config` | `CONFIG OK` |
| `docker compose build` | 两个镜像 `Built`，`BUILD_EXIT=0` |
| `docker compose up -d` | `api` healthy / `web` healthy |
| `/api/health`、`/api/ready` | `{"status":"ok"}` / `{"status":"ready"}` |
| Compose smoke（经 nginx 同源反代） | `exit=0`：health/ready、SPA 回落 200、run 答案含 126、SSE 含 `tool.started`/`tool.completed`/唯一终态 |
| 持久化（down → up） | `exit=0`：Thread、`['user','assistant']`、上传文件内容、Artifact 全部一致 |
| **MCP（容器内，默认关闭 → 显式启用）** | 主 Compose 只有 uvicorn 进程（无 MCP 子进程）；叠加 `docker-compose.test.yml` 后出现 `python /mcp/fixtures/mcp_test_server.py`，`/api/ready` 仍 ready。详见「归档前最终检查」的 Compose A/B |
| 镜像自检 | api：uid 10001、无 `.env`、无 `tests/`、无 `*.db`；web：uid 101、无 `node_modules`、`index.html` 在 |

端口：`127.0.0.1:18000 → api:8000`、`127.0.0.1:18080 → web:8080`
（默认 `8000` / `8080`；本机 8000 被占用故用 `MINI_DEERFLOW_*_PORT` 覆盖）。

卷：`mini-deerflow_api-data → /data`、`mini-deerflow_api-workspace → /workspace`。

> 部署验收全程使用 **FakeModel**：通过 `MINI_DEERFLOW_ENV_FILE` 指向一个不存在的
> env 文件，容器里 `MINI_DEERFLOW_MODEL_PROVIDER` 不被设置 → 默认 `fake`，
> 零网络、零额度。

---

## 实施中发现并处理的真实问题

| # | 问题 | 证据 | 处理 |
|---|---|---|---|
| 1 | `docker compose config` 把 `.env` 展开，**打印了两个真实 Key** | 完整输出含 `MIMO_API_KEY` / `MINIMAX_API_KEY` 的值 | 后续一律 `--quiet` 或只 grep 不含值的键；**建议轮换这两个 Key** |
| 2 | SQLite 在 FastAPI 线程池下跨线程报错 | `sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in that same thread` | `check_same_thread=False` + `RLock` 串行（`thread_store.py`） |
| 3 | nginx 容器非 root 启动失败 | `open() "/run/nginx.pid" failed (13: Permission denied)` | pid 改 `/tmp`；用正则匹配 pid 行（写死 `/var/run/...` 在当前镜像里根本匹配不上） |
| 4 | Compose `up` 撞宿主端口 8000 | `bind: Only one usage of each socket address` | 宿主端口参数化，**主机 IP 仍写死 `127.0.0.1`**，不可被环境变量改成 `0.0.0.0` |
| 5 | Windows 下 `curl -d` 中文 JSON 报 400 | `There was an error parsing the body` | 验收脚本改用 Python `urllib`（UTF-8 编码可控） |
| 6 | MCP 调用超时两条路径竞争 | 同一用例单跑通过、全量随机失败 | 见 Phase 22 表 |
| 7 | E2E 撞本机已占用的 8000/5173 | Playwright `reuseExistingServer:false` 起不来 | 端口环境变量化（默认值不变） |
| 8 | 两个会话同时在同一工作区实施同一任务 | `mcp_config.py` 在 04:38 被覆盖、`mcp_bridge.py` 被新建 | 已发跨会话协调消息；后续采取「读到最新再改、不回滚对方改动」的方式合并 |
| 9 | Git Bash 把 `/mcp/...` 改写成 `C:/Program Files/Git/mcp/...` | 容器日志 `MCP 配置文件不存在: C:/Program Files/Git/mcp/fixtures/mcp.smoke.json` → lifespan 失败 → healthcheck 不健康 | 传 `MSYS_NO_PATHCONV=1`；shell 路径转换，非代码缺陷 |
| 10 | 落库产生孤立 `tool_result` | Thread 只落 `tool`（不带 `call_id`），下一轮 `working_messages` 无配对 `tool_use` | Thread 改为只落 `user` + 最终 `assistant`，见「最终收口」 |

---

## 文档

| 文档 | 状态 |
|---|---|
| [README.md](../README.md) | ✅ 开发模式 / 生产 Compose / 环境变量 / 数据卷 / 备份恢复 |
| [deployment.md](deployment.md) | ✅ 架构、端口、镜像、启动、卷、备份、FAQ、限制 |
| [mcp.md](mcp.md) | ✅ 配置、生命周期、安全边界、测试与 smoke |
| [model-provider.md](model-provider.md) | ✅ 21A/21B 数据流与测试分层 |
| [phase-21-progress.md](phase-21-progress.md) | ✅ 21A–21C 全记录 |
| [feature-parity.md](feature-parity.md) | ✅ 已实现 / 部分实现 / 明确不做 |
| [frontend/ARCHITECTURE.md](frontend/ARCHITECTURE.md)、[frontend/PROGRESS.md](frontend/PROGRESS.md) | ✅ 既有 |

---

## 明确的真实限制

- **无认证、无多租户、无分布式锁** —— 禁止直接暴露公网
- **无 token streaming** —— SSE 只发阶段事件
- **无远程 MCP** —— 只支持本地 stdio
- **无并行工具调用** —— 一次一个 `tool_use`
- **单实例** —— SQLite 与进程内协调器都是单实例语义

---

## 最终收口（Phase 21–23 收尾轮）

本轮**不新增功能**，只做两处强制性修复 + 报告口径修正，然后全量复验。

### 一、Subagent `allowed_tools` 运行时强制

**数据流（双层纵深防御）**

```
SubagentContextPolicy.allowed_tools
        │  tools.build_allow_list()
        ▼
   None  → 不设限制（沿用提交时传入的注册表）
   ()    → frozenset() 空白名单（任何工具都不允许）
   (...) → frozenset({...})
        │
        ├─► 第一层 暴露限制  filter_tool_specs(registry.tool_specs(), allowed)
        │        → 只有白名单内的 ToolSpec 交给 model.decide(messages, tools)
        │
        └─► 第二层 执行限制  ensure_tool_allowed(decision.name, allowed)
                 → 越权抛 ToolNotAllowedError（先于 ToolRegistry.execute）
```

- **三类工具同时受限**：过滤与校验都按**工具名**进行，不区分来源，
  本地工具、`mcp__*` 工具、子代理工具一视同仁
- **`None` 与 `[]` 刻意区分**：`None` = 不设限制，`[]` = 全部禁止
- **父子隔离**：过滤只发生在子代理实例内部，**不修改共享的 ToolRegistry**，
  父 Agent 始终看到完整工具集
- `ToolNotAllowedError` ≠ `ToolNotFoundError`：前者是「越权」，后者是
  「名字不存在」，日志与前端可区分
- 传导链：`SubagentManager.submit(context_policy=…)` → `SubagentTask` →
  `SubagentRunner` → `_BudgetAwareAgent(allowed_tools=…)`

### 二、工具消息不再落库

**`working_messages`（喂给模型）与 `ThreadState`（落库）的区别**

| | 内容 | 生命周期 |
|---|---|---|
| `working_messages` | history + `assistant(tool_use, call_id)` + `tool(result, call_id)` | 本轮内累积，run 结束即弃 |
| `ThreadState` | `user` → **最终 `assistant`** | 持久化，跨 run 与跨重启 |

原因：单独落一条不带 `call_id` 的 `tool` 消息，下一轮 `working_messages`
里没有与之配对的 `tool_use`，会形成**孤儿 tool_result**，供应商会拒收该轮请求。

- 工具结果**没有丢**：完整保留在 Trace（`tool_call.detail.result`）与
  SSE（`tool.started` / `tool.completed`）里
- 失败语义不变：未知工具 / 非法参数 / 工具异常 / Budget 超限 / 超步数
  都在 `thread.extend()` 之前抛出，Thread 完全不变
- **`SCHEMA_VERSION` 保持 1，未升级**：旧数据里的 `tool` 消息读取后由
  `provider_messages_from` 按「孤立消息」退回普通文本，向后兼容有真实代码保证
- 未改前端公开 API 契约：`GET /threads/{id}` 的 `messages` 只是少了一段

### 三、新增测试与准确数量

| 文件 | tests | 覆盖 |
|---|---:|---|
| `tests/test_subagent_allowed_tools.py` | **16** | 允许的本地工具可执行 / 未允许的本地工具被拒 / 允许的 MCP 工具可执行 / 未允许的 MCP 工具被拒 / 空白名单不暴露也不执行 / 伪造工具名无法绕过 / 父子权限互不污染 / `None` 沿用默认 |
| `tests/test_thread_tool_persistence.py` | **10** | 成功后只有 `user`+`assistant` / 失败后 Thread 不变 / 第二轮无孤立 `tool_result` 且正常收敛 / 同一 run 连续两次工具调用 / Trace 与 SSE 仍有 `tool.started`·`tool.completed` / `call_id` 当轮配对 / SQLite 重启后可继续对话 / FakeModel 回归 |
| **新增合计** | **26** | |

同步更新的既有断言（旧契约 → 新契约）：
`test_agent.py`(5)、`test_agent_sandbox_tools.py`(2)、`test_cli.py`(2)、
`test_tool_loop_21b.py`(2)、`test_phase21b_events.py`(1)、`test_phase23_persistence.py`(1)。

### 四、验收类型区分（口径）

| 类别 | 含义 | 本轮是否真实执行 | 结果 |
|---|---|---|---|
| **自动化验收** | `pytest` / `npm run *` / `npm run test:e2e` / `scripts/*.py`，机器可重复 | ✅ 已执行 | 全部通过（见第六节） |
| **Docker 验收** | `docker compose config·build·up·smoke·down`，在真实容器里跑 | ✅ 已执行 | 全部通过（见第六节） |
| **浏览器人工验收** | 人打开页面点选、肉眼确认 UI | ✅ **已执行** | 7 项全部通过（用户执行，2026-10-02） |

> 前两类通过**不等于**第三类通过：Playwright 覆盖的是脚本化断言，
> 不是人工体验验收。因此第三类必须由人真的打开页面确认后才能标 ✅ ——
> 2026-10-02 已由用户执行，7 项全部通过，见「归档前最终检查」。
>
> 注：本项是**生产栈（FakeModel）**上的人工验收；21C 的真实模型调用验收
> 由 `scripts/phase21c_acceptance.py` 脚本化完成，两者不互相替代。

### 五、MCP smoke 口径修正

```
[1/4] 发现 4 个 MCP 工具，ToolRegistry 总计 5 个工具（含 calculator）。
```

**"4"是本次发现并挂载的远端 MCP 工具数；"5"是整个 ToolRegistry 的总数**
（含既有本地 `calculator`）。两个数不是一回事，不要混用。

### 六、本轮真实命令与结果

**后端**

```
$ uv run pytest tests/test_tool_loop_21b.py tests/test_phase21b_events.py \
      tests/test_mcp.py tests/test_phase23_persistence.py -q
91 passed in 19.62s

$ uv run pytest tests/test_subagent_allowed_tools.py tests/test_thread_tool_persistence.py -q
26 passed in 8.66s

$ uv run pytest -q
1961 passed, 42 subtests passed in 107.33s (0:01:47)

$ uv pip check
Checked 48 packages in 6ms
All installed packages are compatible
```

**前端**

```
npm run typecheck      exit 0
npm run lint           exit 0
npm run test -- --run  Test Files 25 passed (25) | Tests 242 passed (242)  exit 0
npm run build          exit 0   index-*.js 326.97 kB / css 19.37 kB
```

**E2E（连续两轮，默认端口）**

```
第 1 轮  18 passed (26.0s)   exit 0
第 2 轮  18 passed (24.2s)   exit 0
```

**MCP 本地 smoke（零外网）**

```
$ uv run python scripts/mcp_smoke.py
[1/4] 发现 4 个 MCP 工具，ToolRegistry 总计 5 个工具（含 calculator）。
      工具清单: ['calculator', 'mcp__smoke__add', 'mcp__smoke__echo',
                 'mcp__smoke__failing_tool', 'mcp__smoke__wait']
[2/4] add(17, 25) = 42   |   echo = MCP_SMOKE_OK
[3/4] 缺参被拒绝: ToolExecutionError
[4/4] 启动期间子进程 3 个，关闭后残留 0 个
exit=0
```

**Docker**

```
docker compose config --quiet                                  CONFIG OK
docker compose build                                           BUILD_EXIT=0
docker compose up -d                                           api healthy / web healthy
curl /api/health → {"status":"ok"}   /api/ready → {"status":"ready",...}
compose_persistence_check.py smoke http://127.0.0.1:18080       SMOKE_EXIT=0
  health/ready · SPA 回落 200 · run 答案含 126 · SSE 含 tool.started/completed/唯一终态
默认（MCP 关闭）容器内进程：仅 uvicorn，无 mcp_test_server
显式启用 MCP 后容器内进程：uvicorn + `python /mcp/fixtures/mcp_test_server.py`
seed → docker compose down → up(叠加 docker-compose.test.yml) → verify   VERIFY_EXIT=0
  Thread 9c4d3142fe3b · ['user', 'assistant'] · 上传文件内容一致 · Artifact 一致
docker compose down
  残留容器 [] · 残留网络 [] · 保留卷 api-data / api-workspace
```

> 启用 MCP 的命令需要 `MSYS_NO_PATHCONV=1`：Git Bash 会把 `/mcp/...`
> 改写成 `C:/Program Files/Git/mcp/...`，导致容器内找不到配置文件。

**测试总数闭合**：1935（收口前）+ 16 + 10 = **1961** ✓

### 七、本轮状态

```
Subagent allowed_tools 运行时强制         ✅  16 tests
本地与 MCP 工具均无法越权                 ✅  同上（含真实本地 MCP 子进程）
Thread 不保存孤立 tool 消息               ✅  10 tests
同一 Thread 工具调用后能继续第二轮对话     ✅  含 SQLite 重启后
后端 / 前端 / E2E / Docker 验收            ✅  见第六节
文档与真实结果一致                         ✅  mcp.md / model-provider.md /
                                             feature-parity.md / PROGRESS.md /
                                             phase-21-progress.md（历史输出已标注）
浏览器人工验收（7 项）                     ✅  用户于 2026-10-02 执行，见「归档前最终检查」
Phase 19–20 tag                           未改动（仓库仅有 phase-19-20-mvp）
```

---

## 归档前最终检查（2026-10-02）

### 一、生产 URL 重复 `/api` 的根因与修复（新发现的真 bug）

**症状**：`http://127.0.0.1:8080` 打开后页面白屏/全错误，
控制台 4 条 404，全部请求打到 `/api/api/...`。

**根因**：调用方传的路径**本身已带 `/api` 前缀**，而生产构建把 base 也设成了 `/api`：

```ts
const url = `${API_BASE_URL}${path}`      // client.ts
apiGet('/api/health')                     // health.ts 等
```

| 场景 | base | 结果 |
|---|---|---|
| 开发 | `http://127.0.0.1:8000` | `http://127.0.0.1:8000/api/health` ✅ |
| 生产（修复前） | `/api` | `/api/api/health` ❌ 404 |
| 生产（修复后） | `''`（由 `/` 归一化而来） | `/api/health` ✅ |

**为什么既有门禁没抓到**：开发模式 base 是绝对地址、Playwright E2E 注入的也是
`http://127.0.0.1:8000`，两者都不会拼出重复前缀 —— 这个 bug **只在
Docker 同源反代模式下出现**，属于「测试盲区」，因此补了针对性的永久回归测试。

**修复（2 处）**

| 文件 | 改动 |
|---|---|
| `frontend/src/shared/api/config.ts` | `normalizeBaseUrl` 区分「未设置」与「显式设置」：未设置 → 回退开发默认；显式设置（含 `''`、`'/'`）→ 按同源归一，`'/'` 去尾斜杠后为空串 |
| `frontend/Dockerfile` | `VITE_API_BASE_URL=/api` → `VITE_API_BASE_URL=/`（选 `/` 而非空串：空环境变量有被 Vite 丢弃、进而回退成跨源的风险） |

**永久回归测试**：`frontend/src/shared/api/config.test.ts`（**8** 项）

```
生产配置 "/" 归一化为空串                       ✅
显式空串同样归一化为空串（不回退到本机后端）      ✅
带尾斜杠的绝对地址只去尾斜杠                     ✅
未设置时回退开发默认值                           ✅
/api/health 最终仍是 /api/health                ✅
所有实际调用路径都保持原样、无 /api/api/         ✅
SSE 流式地址同样不重复                          ✅
反例：base 写成 /api 必然得到 /api/api/（记录事故） ✅
```

**修复后的无头浏览器复验**：

```
#root 长度   : 11574（修复前 2565，且全是错误态）
状态栏       : 就绪
侧栏         : 17 个会话正常列出
事件         : (无 console / pageerror / 失败请求)   ← 修复前 4 条 404
GET /api/threads   -> 200   threads   : 20
GET /api/files     -> 200   uploads   : 6
GET /api/artifacts -> 200   artifacts : 2
```

### 二、部署层与测试层分离

`docker-compose.yml` 里的 `./tests/fixtures → /mcp/fixtures` 属于**测试资源**，
不该进入正常生产 Compose。已拆为两个文件：

```
docker-compose.yml              正常部署：只有具名卷，不挂任何仓库内宿主目录
docker-compose.test.yml         MCP smoke 覆盖层：+只读挂载 +MINI_DEERFLOW_MCP_CONFIG
```

```bash
docker compose up -d                                                            # 正常运行
docker compose -f docker-compose.yml -f docker-compose.test.yml up -d           # MCP smoke
# Git Bash 必须加 MSYS_NO_PATHCONV=1，否则 /mcp/... 会被改写成 C:/Program Files/Git/...
```

**配置层校验（只计数，不打印配置值）**

| 检查项 | 主文件 | 叠加覆盖层 |
|---|---|---|
| `docker compose config --quiet` | OK | OK |
| bind mount 数量 | **0** | 1（`target: /mcp/fixtures`） |
| 具名卷数量 | 2 | 2（不变） |
| `MINI_DEERFLOW_MCP_CONFIG` | **未设置** | 已设置 |

### 三、Compose A/B 运行时验收

**轮 A —— 仅 `docker-compose.yml`**

| 检查 | 结果 |
|---|---|
| `docker compose up -d` | `api` healthy / `web` healthy |
| health（直连 + 经 nginx） | `{"status":"ok"}` / `{"status":"ok"}` |
| ready（直连 + 经 nginx） | `{"status":"ready","runtime":true}` ×2 |
| 容器内进程 | 仅 uvicorn，**MCP 测试服务器: 无** |
| `docker compose down` | 残留容器 `[]` · 残留网络 `[]` · 卷保留 |

**轮 B —— `docker-compose.yml` + `docker-compose.test.yml`**

| 检查 | 结果 |
|---|---|
| 双文件 `up -d` | `api` healthy / `web` healthy |
| health / ready（直连 + 经 nginx） | 全部 `ok` / `ready` |
| 容器内进程 | uvicorn **+ `python /mcp/fixtures/mcp_test_server.py`** |
| **容器内 MCP smoke** | `exit=0`：发现 4 个 MCP 工具，ToolRegistry 总计 5 个工具（含 calculator）；`add(17,25)='42'`、`echo='MCP_SMOKE_OK'`；`stop()` 后 `started=False` |
| **宿主机 MCP smoke** | `scripts/mcp_smoke.py` → `exit=0`，启动期子进程 3 个、关闭后残留 0 |
| 双文件 `down` | 残留容器 `[]` · 残留网络 `[]` · 卷保留 |

**数据卷**（两轮 down 后均保留）：`mini-deerflow_api-data`、`mini-deerflow_api-workspace`

> 部署验收全程 **FakeModel**（`MINI_DEERFLOW_ENV_FILE` 指向不存在的 env 文件
> → 容器内 provider 未设置 → 默认 `fake`），**未运行任何真实付费模型**。

### 四、浏览器人工验收（用户执行，7 项全部通过）

| # | 项 | 状态 |
|---:|---|---|
| 1 | 页面正常渲染（无白屏 / 无 404 / 控制台干净） | ✅ |
| 2 | 完整对话：新建会话 → 发送计算 → 运行状态 → SSE 事件 → 答案 → 刷新后消息仍在 | ✅ |
| 3 | 多会话侧栏：新建 / 切换 / 删除（二次确认）/ URL 与高亮一致 | ✅ |
| 4 | 文件与产物：上传 → 列表 → 下载内容一致 → 删除；Artifacts 只读 | ✅ |
| 5 | 错误与离线：404 稳定文案 + 请求编号；停 `api` 显示离线、恢复后就绪 | ✅ |
| 6 | SSE 中断语义：停止接收 → 事件流停止、常驻说明、不再显示「运行中」 | ✅ |
| 7 | MCP：容器内出现测试 MCP Server 进程，服务仍健康 | ✅ |

> 本项在**生产栈（FakeModel）**上执行；21C 的真实模型调用验收由
> `scripts/phase21c_acceptance.py` 脚本化完成，两者不互相替代。

### 五、最终门禁（全部真实执行）

**后端**

```
$ uv run pytest tests/test_tool_loop_21b.py tests/test_phase21b_events.py \
      tests/test_mcp.py tests/test_phase23_persistence.py \
      tests/test_subagent_allowed_tools.py tests/test_thread_tool_persistence.py -q
117 passed in 24.43s

$ uv run pytest -q
1961 passed, 42 subtests passed in 105.06s (0:01:45)

$ uv pip check
Checked 48 packages in 2ms
All installed packages are compatible
```

**前端**

```
npm run typecheck      exit 0
npm run lint           exit 0
npm run test -- --run  Test Files 26 passed (26) | Tests 250 passed (250)   exit 0
                       （242 + 新增 URL 回归 8）
npm run build          exit 0   index-I1iGWms0.js 326.97 kB / css 19.37 kB
```

**Playwright E2E（配置改动后回归）**

```
18 passed (26.2s)   exit 0
```

**测试总数闭合**

```
后端 1961 = 1935（收口前）+ 16（allowed_tools）+ 10（持久化）
前端  250 = 242（收口前）+ 8（URL 回归）
```

### 六、最终状态

```
Phase 21（21A / 21B / 21C）            ✅
Phase 22（MCP）                         ✅
Phase 23（Docker / 持久化 / 验收）       ✅
Mini DeerFlow MVP                      ✅

Subagent allowed_tools 运行时强制        ✅  16 tests
Thread 不保存孤立 tool 消息              ✅  10 tests
生产 URL 无重复 /api（永久回归）          ✅   8 tests
部署层与测试层分离                        ✅  主 Compose 0 个 bind mount
后端 / 前端 / E2E / Docker A/B 验收       ✅  见上
浏览器人工验收 7 项                       ✅  用户执行
未运行真实付费模型                        ✅  全程 FakeModel
未 commit / 未 push / 未移动旧 tag         ✅  HEAD 仍 4b81225，tag 仅 phase-19-20-mvp
```

**仍待人工的一项（非 MVP 完成标准）**：`docs/phase-21-progress.md` 里 21A 遗留的
「用真实模型在浏览器/CLI 里对话」尚未由人做过 —— 21C 的真实模型验收是脚本化的
（`scripts/phase21c_acceptance.py`，真实调用 3 次）。该项如实保留为 ⏳，未标 ✅。
