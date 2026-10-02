# 部署（Phase 23）

单机私有 MVP 的部署说明。**不是**公网多用户系统的部署手册。

## 0. 定位与安全边界

| 项目 | 现状 |
|---|---|
| 认证 / 授权 | **无** |
| 多租户 | **无**（所有 Thread 属于同一个"用户"） |
| 分布式锁 | **无**（同 Thread 并发 run 由进程内的 `ThreadRunCoordinator` 判 409） |
| 端口绑定 | 默认 **仅 `127.0.0.1`** |

**因此：**

- 仅限本机、或在可信内网 + 反向代理 + 认证的条件下使用
- **没有认证，禁止直接把 8080/8000 暴露到公网**
- 真要对外，前置一层带认证的反向代理（OAuth2 Proxy、Caddy forward_auth 等），
  并确认代理会限制上传体积与超时

## 1. 架构与端口

```
浏览器
  │  http://127.0.0.1:8080
  ▼
web（nginx, 非 root, 容器内 8080）
  ├── /            → 静态 React 构建产物（SPA，刷新回落 index.html）
  └── /api/*       → 同源反代 → api:8000
                          │
                          ▼
                    api（uvicorn + FastAPI, 非 root, 容器内 8000）
                          ├── SQLite   → 卷 /data
                          ├── uploads  → 卷 /workspace/uploads
                          └── artifacts→ 卷 /workspace/artifacts
```

同源反代意味着**生产环境不需要 CORS**；
开发环境前端在 `5173`、后端在 `8000`，仍需 `MINI_DEERFLOW_CORS_ORIGINS`。

## 2. 镜像

| 文件 | 产物 | 要点 |
|---|---|---|
| `Dockerfile` | `mini-deerflow-api:0.1.0` | `python:3.12-slim` + `uv sync --frozen`，非 root（uid 10001） |
| `frontend/Dockerfile` | `mini-deerflow-web:0.1.0` | `node:24-alpine` 构建 → `nginx:alpine` 运行，非 root（`nginx` 用户，8080） |

镜像里**不含**：`.env`、API Key、数据库、上传文件、`node_modules`、`dist`、
测试结果。`.dockerignore` 是第二道保险（Dockerfile 用的是显式 `COPY`）。

## 3. 启动

```bash
cp .env.example .env      # 按需填模型凭据；留空则 provider 默认 fake（离线）

docker compose config      # 语法与插值校验（不启动任何东西）
docker compose up -d --build
docker compose ps
```

探活：

```bash
curl -fsS http://127.0.0.1:8000/api/health   # {"status":"ok","service":"mini-deerflow"}
curl -fsS http://127.0.0.1:8000/api/ready     # {"status":"ready","runtime":true}
curl -fsS http://127.0.0.1:8080/              # 前端首页
```

- `/api/health`：进程存活，不碰 Runtime/数据库/模型
- `/api/ready`：lifespan 是否成功创建了 Runtime；未就绪返回 503

### 部署层与测试层分离

`docker-compose.yml` 只做**正常部署**：`volumes` 里只有两个具名卷，
**不挂载任何仓库内宿主目录**。测试用的 MCP Server 放在
`docker-compose.test.yml` 里按需叠加：

```bash
# 平时
docker compose up -d

# 跑 MCP smoke（Git Bash 必须加 MSYS_NO_PATHCONV=1，否则 /mcp/... 会被改写）
MSYS_NO_PATHCONV=1 docker compose -f docker-compose.yml -f docker-compose.test.yml up -d
MSYS_NO_PATHCONV=1 docker compose -f docker-compose.yml -f docker-compose.test.yml down
```

| 检查项 | 主 `docker-compose.yml` | 叠加覆盖层 |
|---|---|---|
| bind mount | **0** | 1（`/mcp/fixtures`，只读） |
| 具名卷 | 2 | 2（不变） |
| `MINI_DEERFLOW_MCP_CONFIG` | 未设置（MCP 关闭） | 已设置 |

### 依赖顺序

`web` 用 `depends_on: condition: service_healthy` 等 `api` 的 healthcheck
通过之后才启动，因此第一次访问不会撞到"后端还没起来"。

### 优雅停止

```bash
docker compose down
```

`api` 的 `stop_grace_period: 30s`，uvicorn 收到 SIGTERM 后：
结束在跑的 run → 关闭事件循环与线程池 → `RuntimeContext.shutdown()`
（含 `SQLiteThreadStore.close()`）→ 进程退出。

## 4. 环境变量

三层优先级：**系统环境变量 > `.env` > 代码默认值**（`load_dotenv` 默认
`override=False`）。

Compose 的注入顺序：

1. `env_file: .env`（`required: false`，没有也能起）——**运行时注入，不进镜像**
2. `environment:` —— 覆盖 `env_file`，固定数据路径

被固定的两项：

```yaml
MINI_DEERFLOW_DB_PATH: /data/mini-deerflow.db
MINI_DEERFLOW_WORKSPACE_ROOT: /workspace
```

不提供 `.env` 时 `MINI_DEERFLOW_MODEL_PROVIDER` 缺省为 `fake`，
后端照常可用（离线 `FakeModel`），不会因为缺 Key 而起不来。

完整变量表见 [README.md#环境变量](../README.md#环境变量)。

## 5. 数据卷

| 卷 | 挂载 | 内容 |
|---|---|---|
| `mini-deerflow_api-data` | `api:/data` | SQLite：Thread 与消息 |
| `mini-deerflow_api-workspace` | `api:/workspace` | `uploads/`、`artifacts/` |

`docker compose down` 不带 `-v` 时卷保留，**重启后 Thread、消息、上传文件、
Artifact 全部还在**；只有 `down -v` 才会删除。

## 6. 备份与恢复

见 [README.md#数据卷与备份恢复](../README.md#数据卷与备份恢复)。
要点：备份前 `docker compose stop api`，避免写入中途被截断。

## 7. 本机开发 vs 生产

| | 开发 | 生产 Compose |
|---|---|---|
| Thread 存储 | 内存（重启即失） | SQLite（`/data`，卷持久化） |
| Workspace | 临时目录 | `/workspace`（卷持久化） |
| 前端 | Vite dev server（跨源） | nginx 静态 + 同源反代 |
| CORS | 需要 | 不需要 |
| 模型 | `provider=fake` | `.env` 注入，默认 `fake` |
| MCP | 未配置即关闭 | 未配置即关闭 |

## 8. 常见问题

**`/api/ready` 返回 503**
lifespan 里 Runtime 创建失败。最常见原因：
显式设了 `MINI_DEERFLOW_MODEL_PROVIDER=minimax|mimo` 却没给对应 Key
（这是**故意 fail-closed**，不会降级成假模型）。看 `docker compose logs api`。

**打开页面白屏、请求全打到 `/api/api/...`**
前端 `API_BASE_URL` 被设成了 `/api`，而调用方传的路径本身已含 `/api`，
拼出 `/api/api/health` → 404。生产 base 必须归一成**空串**
（`frontend/Dockerfile` 里是 `VITE_API_BASE_URL=/`）。
`frontend/src/shared/api/config.test.ts` 有 8 项永久回归测试守这条。
开发模式与 Playwright E2E 都测不到它——它们的 base 是绝对地址。

**前端刷新 404**
nginx 的 `try_files $uri $uri/ /index.html` 负责 SPA 回落；
若 404 出现在 `/assets/*`，说明构建产物没打进镜像（检查 `frontend/.dockerignore`）。

**SSE 事件不推送**
`/api/` 的 `proxy_buffering off` 必须保留，否则事件会攒在 nginx 缓冲里。

**重启后数据没了**
确认没有用 `docker compose down -v`；确认 `MINI_DEERFLOW_DB_PATH`
仍指向 `/data/...`（容器内路径，不是宿主机路径）。

## 9. 已知限制

- 无认证、无多租户、无分布式锁
- 无远程 MCP（只支持本地 stdio）
- 无 token streaming（SSE 只发阶段事件，不逐 token 推送）
- 单实例设计：横向扩容需要先解决 SQLite 与进程内协调器的共享问题

详见 [feature-parity.md](feature-parity.md)。
