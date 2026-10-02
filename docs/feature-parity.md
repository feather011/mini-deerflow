# 功能对齐表（Feature Parity）

按本项目自己的路线（Phase 1–23）列出：**已实现 / 部分实现 / 明确不做**。
这是诚实的能力清单，不是宣传页；每条都对应仓库里真实存在的代码或测试。

状态图例：`✅ 已实现` `🟡 部分实现` `⛔ 明确不做`

更新时间：2026-10-02

---

## 1. Agent 核心

| 能力 | 状态 | 说明 |
|---|---|---|
| 同步 Agent 循环（模型 → 工具 → 模型 → 最终回答） | ✅ | `agent.py`，受 `max_steps` 约束 |
| ToolRegistry（注册 / 查找 / 执行 / 批量原子注册） | ✅ | `tools.py`；重名抛 `DuplicateToolError` |
| 供应商无关工具协议（`ToolSpec` / `ToolCall` / `Message.tool_*`） | ✅ | Anthropic SDK 类型不进入 Agent/Registry/ThreadStore |
| 真实模型 ToolCall 闭环（Phase 21B） | ✅ | `AnthropicCompatModel` + Agent 两轮收敛，真实验收含 126 |
| Budget（模型调用 / 工具调用 / 步数 / 子任务 / 深度） | ✅ | `middleware.Budget`，原子 consume |
| Middleware 管线（Trace / Logging / Retry / Budget / RunEvent） | ✅ | `middleware.py` + `api/middleware/run_event.py` |
| Trace（内存 JSONL sink，`--trace` 文本轨迹） | ✅ | |
| 失败不半提交（Thread 原子写入） | ✅ | `thread.extend()` 只在收敛后执行，有专门测试 |
| Token streaming（逐 token 推送） | ⛔ | **明确不做**，见 §7 |
| 一次响应多个 `tool_use`（并行工具调用） | ⛔ | **明确不做**：抛 `ModelProtocolError`，不静默丢弃 |

## 2. 工具

| 能力 | 状态 | 说明 |
|---|---|---|
| calculator（ast 安全求值，四则 + 括号） | ✅ | 默认注册 |
| Skill 工具（目录 / 元数据 / 选择器 / 上下文注入） | ✅ | `skills/` + `skill_*.py` |
| Sandbox 工具（本地与 Docker 两种执行后端、路径沙箱） | ✅ | `sandbox_*.py`，默认关闭（需显式 `--sandbox-root`） |
| Subagent 工具（提交子任务、并发/深度/预算上限） | ✅ | `subagent_tools.py` |
| Subagent `allowed_tools` **运行时强制** | ✅ | 双层：暴露限制（`filter_tool_specs`）+ 执行限制（`ensure_tool_allowed` → `ToolNotAllowedError`）；覆盖本地 / `mcp__*` / 子代理工具 |
| 长期记忆（SQLite 存储、提取、去重、上下文注入） | ✅ | `memory_*.py` |
| MCP 工具（本地 stdio 服务器） | ✅ | Phase 22 完成，详见 §5 |
| 远程 HTTP MCP / SSE MCP | ⛔ | **明确不做**（第一版只支持本地 stdio） |

## 3. 会话与持久化

| 能力 | 状态 | 说明 |
|---|---|---|
| Thread 状态机 + 序列化（`schema_version=1`） | ✅ | 新增字段「有值才写」，老数据无需迁移 |
| Thread 只落 `user` + 最终 `assistant` | ✅ | 工具往返只进 `working_messages`/Trace/SSE，避免孤儿 `tool_result` |
| InMemory ThreadStore | ✅ | 开发与测试默认 |
| SQLite ThreadStore | ✅ | CLI 默认；API 侧由 `MINI_DEERFLOW_DB_PATH` 开启（23） |
| 跨线程安全（API 线程池共用连接） | ✅ | `check_same_thread=False` + `RLock`（23 实测修掉） |
| 多会话 CLI（`/new` `/switch` `/list` `/delete`…） | ✅ | |
| 跨重启持久化（Thread / 消息 / 上传 / Artifact） | ✅ | Compose 卷重启验收 + 自动化测试 |

## 4. API 与前端

| 能力 | 状态 | 说明 |
|---|---|---|
| Threads CRUD、Runs（同步）、Runs/Stream（SSE） | ✅ | 统一 `/api` 前缀 |
| Files（上传 / 列表 / 下载 / 删除）与 Artifacts（只读） | ✅ | |
| Health / Ready | ✅ | `/api/health` 存活；`/api/ready` 就绪 |
| 统一错误信封 + Request ID 贯通 | ✅ | 所有状态码统一 `ErrorResponse` |
| SSE 事件（run/model/tool 的 started/completed/failed，唯一终态） | ✅ | 同一 `run_id`，序列号连续 |
| React 前端（会话、SSE 状态机、文件、错误分层） | ✅ | TanStack Query + React Router |
| 前端单元测试 / typecheck / lint / build | ✅ | 242 tests / 25 files |
| Playwright E2E | ✅ | 18 个用例，连续两轮全绿 |
| 生产同源反代（nginx `/api` → api） | ✅ | 生产无需 CORS；开发仍跨源 |
| SPA 路由刷新 | ✅ | `try_files … /index.html` |

## 5. MCP（Phase 22）

| 能力 | 状态 | 说明 |
|---|---|---|
| 本地 stdio MCP Server 连接与工具发现 | ✅ | `MCPClientManager`，启动期一次性发现 |
| `MCPToolAdapter`（schema → `ToolSpec`，命名空间 `mcp__server__tool`） | ✅ | 冲突在注册前检测，不自动改名 |
| 同步 Agent ↔ 异步 MCP 的生命周期化桥（禁止每次 `asyncio.run`） | ✅ | `mcp_bridge.AsyncPortal`（常驻事件循环线程） |
| required fail-closed / optional 记录安全错误并跳过 | ✅ | |
| 调用超时（协议层 + 外层兜底，统一归一为 `TimeoutError`） | ✅ | 见 `mcp_manager._invoke` |
| 输出大小上限、参数校验、子进程 env 白名单 | ✅ | `max_output_bytes`、`validate_arguments`、`build_child_env` |
| 关闭后无残留子进程 | ✅ | smoke 与集成测试都断言 |
| 默认关闭、显式启用 | ✅ | 未配置 `MINI_DEERFLOW_MCP_CONFIG` 即不启动 |
| 客户端 HTTP 请求不能控制 command/args | ✅ | 配置只来自文件，无任何 HTTP 入口 |
| 远程 HTTP MCP / OAuth / 动态市场 | ⛔ | **明确不做** |

> 详细设计见 [docs/mcp.md](mcp.md)；人工 smoke：`uv run python scripts/mcp_smoke.py`。

## 6. 部署（Phase 23）

| 能力 | 状态 | 说明 |
|---|---|---|
| Backend Dockerfile（非 root、无凭据入镜像） | ✅ | `python:3.12-slim` + `uv sync --frozen` |
| Frontend production Dockerfile（非 root） | ✅ | `node:24-alpine` 构建 → `nginx:alpine` 运行 |
| docker-compose.yml（api + web + 卷 + healthcheck + 依赖条件） | ✅ | |
| `.dockerignore`（不打包 `.env` / `node_modules` / `dist` / 测试结果） | ✅ | Dockerfile 另有显式 `COPY` 兜底 |
| 数据卷持久化（SQLite + uploads + artifacts） | ✅ | `down`→`up` 验收通过 |
| 端口只绑定 `127.0.0.1` | ✅ | 主机 IP 写死，不可被环境变量改成 `0.0.0.0` |
| Redis / PostgreSQL | ⛔ | **明确不做**：现有架构不需要 |

## 7. 明确不做的事（真实限制）

| 限制 | 原因 |
|---|---|
| **无认证、无授权** | MVP 定位；因此禁止直接暴露公网 |
| **无多租户** | 所有 Thread 属于同一个使用者 |
| **无分布式锁** | 同 Thread 并发靠进程内 `ThreadRunCoordinator` 判 409 |
| **无 token streaming** | SSE 只发阶段事件（`model.*` / `tool.*` / `run.*`），不逐 token 推送 |
| **无远程 MCP** | 第一版只支持本地 stdio；HTTP MCP / OAuth 不做 |
| **无并行工具调用** | 一次只允许一个 `tool_use`，多的直接抛协议错误 |
| **无水平扩容** | SQLite 与进程内协调器都是单实例语义 |
| **不支持跨提供方凭据别名** | 每个 provider 只认自己的变量，避免串用凭据 |
| **无角色/用户维度的 ACL** | `allowed_tools` 是按子任务的白名单，不是按用户的访问控制 |

---

## 测试与验收计数（真实命令输出）

| 范围 | 数量 | 命令 |
|---|---|---|
| 后端全量 | 见 [docs/PROGRESS.md](PROGRESS.md) | `uv run pytest -q` |
| 前端单测 | 242（25 files） | `npm run test -- --run` |
| 前端 typecheck / lint / build | 全部 exit 0 | `npm run typecheck` / `npm run lint` / `npm run build` |
| Playwright E2E | 18 × 2 轮全绿 | `npm run test:e2e` |

具体数字以 [PROGRESS.md](PROGRESS.md) 中记录的真实命令输出为准，不做估算。
