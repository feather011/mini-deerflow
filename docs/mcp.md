# MCP 扩展（Phase 22）

> 让 Agent 能调用**本地 stdio MCP Server** 上的外部工具。
> 默认关闭；显式配置才启用。

## 架构

```
MCPServerConfig          配置（只来自文件，不来自 HTTP）
  → MCPClientManager     生命周期：启动连接 / 发现工具 / 关闭
  → MCP Session          长驻异步任务持有 async with
  → Tool Discovery        list_tools()
  → MCPToolAdapter        Schema 转换 + 命名空间 + 参数校验 + 输出限制
  → ToolRegistry          普通工具注册表（MCP 工具与内置工具同表）
  → Agent                 与 calculator 走完全相同的循环
```

### 模块

```
src/mini_deerflow/
├── mcp_config.py     MCPServerConfig + 加载与校验（fail-fast）
├── mcp_bridge.py     AsyncPortal：长期事件循环线程，同步 Agent ↔ 异步 MCP
├── mcp_adapter.py    MCPToolAdapter：Schema/命名/校验/截断（纯转换，无 I/O）
├── mcp_manager.py    MCPClientManager：连接、发现、注册、关闭
└── （接入点）runtime.py → RuntimeConfig.mcp_servers / RuntimeContext.shutdown
tests/fixtures/
└── mcp_test_server.py 确定性测试 MCP Server（echo / add / failing_tool / wait）
```

## 配置

只支持**本地 stdio**；不实现远程 HTTP MCP、OAuth、动态市场。

`MINI_DEERFLOW_MCP_CONFIG` → JSON 文件路径；**未设置 = 不启用**。

```json
{
  "servers": [
    {
      "name": "demo",
      "command": "python",
      "args": ["-m", "mypackage.server"],
      "env": {"MY_TOKEN": "仅子进程可见"},
      "enabled": true,
      "required": false,
      "startup_timeout": 15,
      "call_timeout": 30,
      "max_output_bytes": 65536
    }
  ]
}
```

| 字段 | 含义 |
|---|---|
| `name` | 服务器名，也是工具命名空间的一部分；**重复即报错** |
| `command` / `args` | **只来自配置文件**，没有任何 HTTP 入口能提供 |
| `env` | 传给子进程的**白名单**变量 |
| `enabled` | `false` 则完全跳过（仍会校验字段） |
| `required` | `true` 时启动失败 → **fail-closed**，整个 runtime 起不来 |
| `startup_timeout` | 连接 + 发现工具的超时 |
| `call_timeout` | 单次 `call_tool` 超时 |
| `max_output_bytes` | 单次工具输出上限（按**字节**截断） |

### 安全边界

| 边界 | 做法 |
|---|---|
| 命令执行 | `command`/`args` 只读配置文件；后端与前端**都没有**接收它们的接口 |
| 环境变量 | 子进程环境 = SDK 的 `get_default_environment()` + 配置 `env` 白名单；父进程的 API Key **不下发**（有测试断言） |
| 工具名 | `mcp__<server>__<tool>`，服务器名参与命名，冲突直接报错不覆盖 |
| 输出 | 按字节截断，超限附加提示；二进制块转占位文本，不把 base64 灌进模型上下文 |
| 参数 | 发起调用**之前**按 `input_schema.required` 校验 |
| 日志 | 只记服务器名与**异常类型名**，不记 env、不记完整命令行 |
| 配置展示 | `MCPServerConfig.__repr__` 只暴露 `env` 的键名，不暴露值 |

## 关键实现约束

### 1. cancel scope 必须同任务进出

anyio 的 cancel scope 绑定到"进入它的那个任务"。若任务 A `__aenter__`、
任务 B `__aexit__`，会抛：

```
RuntimeError: Attempted to exit cancel scope in a different task than it was entered in
```

因此每个服务器跑一个**长驻任务** `_session_task`，由它独占整个
`async with Client(...)` 的进入与退出；`list_tools()` / `call_tool()`
从其他任务发起，只操作传输流、不进出 cancel scope，是安全的。

### 2. 禁止 `asyncio.run()`

Agent 是同步的，MCP 是异步的。每次工具调用临时 `asyncio.run()` 会：

- 新建/销毁事件循环，而 session 的传输流绑定在别的循环上 → 跨循环调用报错
- 每次调用都要重建连接

`AsyncPortal` 在**专用线程上跑一个长期事件循环**，同步侧用
`asyncio.run_coroutine_threadsafe()` 提交协程：

```
Agent (同步线程)
   └─ portal.run(coro)
        └─ run_coroutine_threadsafe(coro, loop) ──→ [MCP 事件循环线程]
                                                  └─ session 常驻这里
```

`close()` 先**排空未完成协程**再停循环，否则被 cancel 的协程来不及收尾，
退出时会打印 `Task was destroyed but it is pending`。

### 3. 默认关闭

`RuntimeConfig.mcp_servers` 默认空列表 → `MCPClientManager.start()` 直接返回 0，
不创建事件循环线程、不拉起任何子进程（有测试断言子进程集合不变）。

## 生命周期

| 时机 | 行为 |
|---|---|
| `create_runtime()` | 逐个连接 → 发现工具 → 注册进 `ToolRegistry` |
| 任一 `required` 失败 | `MCPClientManager.stop()` 自清理 → 抛 `MCPStartupError`（fail-closed） |
| 任一 `optional` 失败 | 记安全日志（仅异常类型名）→ 跳过该服务器，其余继续 |
| `RuntimeContext.shutdown()` | 先停 MCP（它持有外部子进程与事件循环桥），再关其他资源 |
| 重复 `start()` / `stop()` | 幂等，不产生重复连接或残留 |

**关闭顺序**：MCP 必须在事件循环桥之前收尾，否则收尾协程没有可用的桥去执行。

## Agent 如何使用

MCP 工具与内置工具**完全同表**，因此：

- 走同一条 `Agent` 循环、同一个 `ToolRegistry.execute()`
- 受同一个 `max_steps`、`Budget`、`Middleware`、`Trace` 约束
- SSE 事件序列一致：`model.* → tool.started → tool.completed → …`
- 失败语义一致：`ToolNotFoundError` / `ToolExecutionError`
- 落库形状一致：Thread 只落 `user` 与最终 `assistant`；工具往返（`assistant(tool_use)` + `tool_result`）只存在于本轮 `working_messages`、Trace 与 SSE 事件——否则会留下没有配对 `tool_use` 的孤儿 `tool_result`

模型侧只看到 `ToolSpec`（`name` / `description` / `input_schema`），
**拿不到 `handler`**，因此无法越过注册表直接执行任何东西。

## 工具失败的语义

MCP 协议里工具失败是 `is_error=True` 的**成功响应**，不是异常。
适配层把它渲染成带失败标记的文本返回给模型，让模型能据此改写方案。

这与内置工具不同：内置工具的 handler 抛异常会被
`ToolRegistry.execute` 包成 `ToolExecutionError`，整轮 Agent 失败。
两者都属于**既有的稳定语义**，22 阶段没有改写其中任何一方。

## 已知真实限制

| 限制 | 说明 |
|---|---|
| ~~`allowed_tools` 只序列化~~ → **已运行时强制** | 双层：① 暴露限制——模型只拿到白名单内的 `ToolSpec`；② 执行限制——执行前再校验工具名，伪造也绕不过。`None`=不设限制，`[]`=全部禁止，越权抛 `ToolNotAllowedError`。见 `tests/test_subagent_allowed_tools.py` |
| 只支持本地 stdio | 远程 HTTP MCP、OAuth、动态工具市场均未实现 |
| 无角色/用户维度的访问控制 | `allowed_tools` 是**按子任务**的白名单，不是按用户或角色的 ACL；整个 MVP 本身无认证 |
| 工具调用同步阻塞 Agent | 单次调用最多等 `call_timeout`；没有异步工具或并行调用 |
| 名称冲突即启动失败 | 不自动重命名、不覆盖——静默覆盖会让模型调错服务器 |

## 测试

| 层 | 文件 | 数量 | 是否起子进程 |
|---|---|---|---|
| 配置 / 桥 / 适配层 / 环境 | `tests/test_mcp.py` 前半 | — | 否 |
| 集成（本地 MCP Server） | `tests/test_mcp.py` 后半 | — | 是（用完即停） |
| **合计** | `tests/test_mcp.py` | **46** | 仅集成用例 |

覆盖：启动与发现、Schema 转换、echo/add 调用、参数错误、工具失败、
超时、名称冲突、required/optional 启动失败、**shutdown 无残留子进程**、
Runtime/Agent 完整调用链、默认未启用不启动 MCP。

Subagent `allowed_tools` 的运行时强制另有专测：
`tests/test_subagent_allowed_tools.py`（16 项，含本地/MCP/空白名单/伪造/父子隔离）。

**运行**：

```bash
uv run pytest tests/test_mcp.py -q        # 定向
uv run pytest -q                          # 完整
uv pip check

# 可重复执行的本地 MCP smoke（显式运行，不进 pytest，零外网）
uv run python scripts/mcp_smoke.py
```

`scripts/mcp_smoke.py` 连的是仓库自带的
`tests/fixtures/mcp_test_server.py`（stdio，本地子进程），依次验证：
启动与发现 → `add`/`echo` 真实执行 → 缺参被拒绝 → **关闭后无残留子进程**。
它**不读取** `MINI_DEERFLOW_MCP_CONFIG`，因此不会碰部署者配置的真实服务器。

**本地 MCP smoke**（不访问外部网络，HTTP 客户端被结构性封禁）：

```
[1/4] 发现 4 个 MCP 工具，ToolRegistry 总计 5 个工具（含 calculator）。
      工具清单: ['calculator', 'mcp__smoke__add', 'mcp__smoke__echo',
                 'mcp__smoke__failing_tool', 'mcp__smoke__wait']
[2/4] add(17, 25) = 42
      echo       = MCP_SMOKE_OK
[3/4] 缺参被拒绝: ToolExecutionError
[4/4] 启动期间子进程 3 个，关闭后残留 0 个
MCP smoke 通过（本地 stdio，零外网，未输出任何环境变量值）   exit=0
```

> 口径说明：**"4 个 MCP 工具"是本次发现并挂载的远端工具数；
> "ToolRegistry 总计 5 个"是整个注册表（含既有本地 `calculator`）的总数。**
> 两者不是同一个数，不要混用。

另一次人工 smoke（经 Agent 闭环）：

```
已注册工具: calculator + mcp__local__{add,echo,failing_tool,wait}
模型可见 ToolSpec: 同上（不含 handler）
MCP echo → local-ok | MCP add → 42
Agent 闭环 → 经 MCP 计算得到 42
Trace → model_decision → tool_call → final_answer
Thread 持久化 → (user, 算一下), (assistant, 经 MCP 计算得到 42)
（该次 smoke 执行于「工具往返不落库」收口之前，当时落库含 (tool, 42)；收口后的形状见上文"落库形状一致"）
shutdown → started=False, servers=()
```
