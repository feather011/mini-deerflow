# Mini DeerFlow

一个**单机私有 MVP** 级别的 Agent 运行时：同步 Agent 循环 + 工具注册表 +
FastAPI/SSE + React 前端，可插拔真实模型（Anthropic 兼容接口）与本地 MCP 服务器。

> **安全声明**：本项目**没有认证、没有多租户、没有分布式锁**。
> 默认端口只绑定 `127.0.0.1`。**在没有反向代理与认证的前提下，禁止直接暴露公网。**
> 详见 [docs/deployment.md](docs/deployment.md)。

---

## 目录

- [开发模式](#开发模式)
- [生产模式（Docker Compose）](#生产模式docker-compose)
- [环境变量](#环境变量)
- [数据卷与备份恢复](#数据卷与备份恢复)
- [测试](#测试)
- [文档](#文档)

---

## 开发模式

### 后端

```bash
# 1) 安装依赖（锁定版本）
uv sync

# 2) 启动 API（开发）
uv run uvicorn mini_deerflow.api.app:app --reload --port 8000
```

- `GET /api/health` 存活检查、`GET /api/ready` 就绪检查
- OpenAPI 文档：`http://127.0.0.1:8000/docs`
- **默认 `provider=fake`（`FakeModel`）**：不读凭据、不联网、行为确定

### 交互式 CLI

```bash
uv run mini-deerflow
uv run mini-deerflow --trace      # 显示模型决策 / 工具调用 / 最终回答的执行轨迹
```

### 前端

```bash
cd frontend
npm install
npm run dev                       # http://localhost:5173
```

开发环境的 API 地址来自 `frontend/.env.local`（缺省 `http://127.0.0.1:8000`），
因此开发时**是跨源**的，需要设置 `MINI_DEERFLOW_CORS_ORIGINS`；
生产 Compose 走同源反代，**不需要 CORS**。

### 本地 MCP（默认关闭）

```bash
# 显式提供配置文件路径后才会连接；未设置则完全不启动 MCP
export MINI_DEERFLOW_MCP_CONFIG=$PWD/mcp.servers.json
uv run uvicorn mini_deerflow.api.app:app --port 8000
```

配置格式与安全边界见 [docs/mcp.md](docs/mcp.md)。

---

## 生产模式（Docker Compose）

```bash
# 0) 可选：准备凭据（不写进镜像，仅运行时注入）
cp .env.example .env

# 1) 校验 Compose 配置
docker compose config

# 2) 构建并启动
docker compose up -d --build

# 3) 健康检查
docker compose ps
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/ready
curl http://127.0.0.1:8080/

# 4) 停止（默认**不删**数据卷）
docker compose down

# 5) 彻底清理（会删数据卷，谨慎）
docker compose down -v

# 6) （可选）MCP smoke：需要额外叠加测试覆盖层
#    Git Bash 下必须加 MSYS_NO_PATHCONV=1，否则 /mcp/... 会被改写成 C:/Program Files/Git/...
MSYS_NO_PATHCONV=1 docker compose -f docker-compose.yml -f docker-compose.test.yml up -d
MSYS_NO_PATHCONV=1 docker compose -f docker-compose.yml -f docker-compose.test.yml down
```

> `docker-compose.yml` 是**正常部署面**，只含具名卷、不挂任何仓库内宿主目录；
> `docker-compose.test.yml` 才挂测试用的 MCP Server（只读）。
> 平时一律用 `docker compose up -d`，测试资源不会进生产 Compose。

| 服务 | 容器内端口 | 宿主机绑定 | 说明 |
|---|---|---|---|
| `api` | 8000 | `127.0.0.1:8000` | FastAPI，非 root |
| `web` | 8080 | `127.0.0.1:8080` | nginx 静态站点 + `/api` 同源反代，非 root |

打开 <http://127.0.0.1:8080/> 即可使用；`web` 通过
`depends_on: condition: service_healthy` 等待 `api` 就绪后才启动。

---

## 环境变量

配置优先级（全局一致）：

```
系统环境变量   >   .env   >   代码默认值
```

由 `load_dotenv()` 的默认 `override=False` 实现。
**生产代码禁止 `load_dotenv(override=True)`**——那会让 `.env` 覆盖容器注入的凭据。

### 模型（21）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MINI_DEERFLOW_MODEL_PROVIDER` | `fake` | `fake` \| `minimax` \| `mimo` |
| `MIMO_API_KEY` / `MIMO_BASE_URL` / `MIMO_MODEL` | — / `https://api.xiaomimimo.com/anthropic` / `mimo-v2.6-flash` | `provider=mimo` 必填 Key |
| `MINIMAX_API_KEY` / `MINIMAX_BASE_URL` / `MINIMAX_MODEL` | — / `https://api.minimaxi.com/anthropic` / `MiniMax-M3` | `provider=minimax` 必填 Key |
| `MINI_DEERFLOW_MODEL_TIMEOUT_SECONDS` | `60` | 传输超时 |
| `MINI_DEERFLOW_MODEL_MAX_RETRIES` | `2` | SDK 自动重试次数 |

> 显式选择真实 provider 但缺 Key 时**启动即失败**，绝不静默降级成 `FakeModel`。
> 每个 provider 只认自己的三件套，**没有跨提供方别名**（避免串用凭据）。

### API / 存储（19、23）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MINI_DEERFLOW_DB_PATH` | 未设置 | **设置后启用 SQLite 持久化**；未设置 = 内存（重启即失） |
| `MINI_DEERFLOW_WORKSPACE_ROOT` | 临时目录 | 上传文件与产物的根目录 |
| `MINI_DEERFLOW_CORS_ORIGINS` | 未设置 | 逗号分隔的 Origin；不设则不启用 CORS |
| `MINI_DEERFLOW_CORS_ALLOW_CREDENTIALS` | `false` | |
| `MINI_DEERFLOW_MAX_UPLOAD_BYTES` | `10485760` | 单文件上传上限 |
| `MINI_DEERFLOW_MAX_LIST_ENTRIES` | `1000` | 文件列表条目上限 |

### MCP（22，默认关闭）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MINI_DEERFLOW_MCP_CONFIG` | 未设置 | JSON 配置文件路径；**未设置 = 不启动任何 MCP** |

`command` / `args` **只来自该配置文件**，HTTP 请求无法控制（否则等于开远程执行）。

完整说明见 [.env.example](.env.example)、
[model-provider.md](docs/model-provider.md)、[mcp.md](docs/mcp.md)。

---

## 数据卷与备份恢复

Compose 创建两个**具名卷**：

| 卷 | 容器内路径 | 内容 |
|---|---|---|
| `api-data` | `/data` | SQLite 数据库（Thread、消息） |
| `api-workspace` | `/workspace` | `uploads/` 上传文件、`artifacts/` 产物 |

### 备份

```bash
# 停写后备份（最稳）
docker compose stop api

docker run --rm \
  -v mini-deerflow_api-data:/data:ro \
  -v "$PWD":/backup \
  alpine tar czf /backup/mini-deerflow-data.tgz -C /data .

docker run --rm \
  -v mini-deerflow_api-workspace:/workspace:ro \
  -v "$PWD":/backup \
  alpine tar czf /backup/mini-deerflow-workspace.tgz -C /workspace .

docker compose start api
```

### 恢复

```bash
docker compose stop api

docker run --rm -v mini-deerflow_api-data:/data -v "$PWD":/backup \
  alpine tar xzf /backup/mini-deerflow-data.tgz -C /data

docker run --rm -v mini-deerflow_api-workspace:/workspace -v "$PWD":/backup \
  alpine tar xzf /backup/mini-deerflow-workspace.tgz -C /workspace

docker compose start api
```

> `docker compose down`（**不带 `-v`**）不会删除具名卷，重启后数据仍在；
> 只有 `docker compose down -v` 才会连数据一起删。

---

## 测试

```bash
# 后端：全部自动化测试默认**零网络、零额度**
uv run pytest -q
uv pip check

# 前端
cd frontend
npm run typecheck
npm run lint
npm run test -- --run
npm run build

# 浏览器 E2E（自动拉起确定性后端与前端）
npm run test:e2e
```

真实模型的验收是**显式、独立**执行的，不会被默认测试收集：

```bash
env -u MIMO_API_KEY uv run python scripts/phase21c_acceptance.py
```

---

## 文档

| 文档 | 内容 |
|---|---|
| [docs/deployment.md](docs/deployment.md) | 部署、端口、卷、备份、安全边界 |
| [docs/feature-parity.md](docs/feature-parity.md) | 已实现 / 部分实现 / 明确不做 |
| [docs/PROGRESS.md](docs/PROGRESS.md) | 总进度 |
| [docs/model-provider.md](docs/model-provider.md) | 模型接入与 ToolCall 数据流 |
| [docs/mcp.md](docs/mcp.md) | MCP 配置、生命周期与安全边界 |
| [docs/frontend/ARCHITECTURE.md](docs/frontend/ARCHITECTURE.md) | 前端分层与状态流 |
| [docs/frontend/PROGRESS.md](docs/frontend/PROGRESS.md) | 前端 Phase 19–20 进度 |
