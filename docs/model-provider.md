# 模型接入架构（Phase 21）

> 21A：MiniMax 模型适配与统一 Model Factory。
> 21B：ToolCall 支持（真实模型工具闭环）。

## 背景

Phase 19–20 的完整链路（FastAPI + React + SSE + 测试闭环）此前一直跑在
`FakeModel` 上——它是纯函数、零网络、行为确定，保证了整套测试稳定，但也意味着
**真实 LLM 能力从未接进 Agent**。

`create_runtime()` 原本硬编码 `model=model or FakeModel()`，`.env` 里的 API Key
对应用完全无效：唯一的联网代码是独立的 `mimo_smoke_test.py`，它绕过 Agent 直接
调 SDK，只证明「能连通」，不证明「Agent 能用」。

21A 的目标就是把这条真实能力接进**生产执行路径**，同时保持默认离线。

## 供应商

21A 接入了**两个 Anthropic 兼容接口**，共用同一套适配器与错误映射：

| provider | 接口 | 默认模型 | 凭据变量 |
|---|---|---|---|
| `minimax` | `https://api.minimaxi.com/anthropic` | `MiniMax-M3` | `MINIMAX_*` |
| `mimo` | `https://api.xiaomimimo.com/anthropic` | `mimo-v2.6-flash` | `MIMO_*` |
| `fake`（默认） | — | — | 无凭据 |

命名说明（重要）：**凭据变量必须与提供方语义一致**。历史上
`DASHSCOPE_API_KEY` / `MIMO_MODEL` 被当作用来配 MiniMax —— 名字属于别的提供方，
实际是在两个供应商之间串用凭据，这是凭据边界问题，不是便利问题。
因此所有「别人的别名」都已移除；每个 provider 只认自己的三件套。

> 早期 `MIMO_API_KEY` 等曾因「是 MiniMax 的别名」被移除，现在它们是 `mimo`
> provider 的**正式变量**。语义从「别人的别名」变成「本提供方自己的配置」，
> 这正是移除别名要达到的目的，不构成反复。

## 模块划分

```
src/mini_deerflow/
├── model_errors.py     领域异常层级（不含任何凭据）
├── model_config.py     ModelSettings + load_model_settings(env, dotenv_path)
├── anthropic_compat_model.py    AnthropicCompatModel（Model 协议实现）+ 消息转换 + SDK 调用
├── model_factory.py    create_model(settings) → Model
└── mimo_smoke_test.py  独立显式的真实连通性 smoke
```

依赖方向：`model_factory → anthropic_compat_model → {model_config, model_errors, models}`，
均不反向依赖 `runtime` / `cli` / `api`，无循环导入。

## 配置

**提供方变量（每个 provider 只认自己的三件套）**

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `MINI_DEERFLOW_MODEL_PROVIDER` | `fake` | `fake` \| `minimax` \| `mimo` |
| `MINIMAX_API_KEY` | — | 缺失且 provider=minimax → 启动失败 |
| `MINIMAX_BASE_URL` | `https://api.minimaxi.com/anthropic` | |
| `MINIMAX_MODEL` | `MiniMax-M3` | |
| `MIMO_API_KEY` | — | 缺失且 provider=mimo → 启动失败 |
| `MIMO_BASE_URL` | `https://api.xiaomimimo.com/anthropic` | |
| `MIMO_MODEL` | `mimo-v2.6-flash` | |

**传输层变量（去提供方化，两个 provider 共用）**

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `MINI_DEERFLOW_MODEL_TIMEOUT_SECONDS` | `60` | 非数字 → `ModelConfigError` |
| `MINI_DEERFLOW_MODEL_MAX_RETRIES` | `2` | 非整数 → `ModelConfigError` |

`minimax` 与 `mimo` 走同一套 Anthropic 兼容协议，timeout / 重试是**传输属性**，
不该挂在某个供应商名字下 —— 旧名 `MINIMAX_TIMEOUT_SECONDS` 在只有 minimax 时尚可，
加第二个提供方就会误导（用 MiMo 却配 `MINIMAX_TIMEOUT_SECONDS`）。

**跨提供方串用凭据必须失败**：`provider=mimo` 时 `MINIMAX_API_KEY` 完全不被读取，
反之亦然；有专门测试覆盖双向。

### 三层优先级

```
系统环境变量  >  .env  >  代码默认值
```

由 `load_dotenv()` 的**默认 `override=False`** 实现：

- 已存在于进程环境的键**不会**被 `.env` 覆盖 → 系统环境变量占优
- 只在 `.env` 里出现的键被写入 → 优先于代码默认值
- 两者都没有 → 代码默认值

生产代码与 smoke **禁止使用 `load_dotenv(override=True)`**：那会让 `.env` 覆盖真实
进程环境，使容器/CI 注入的凭据失效，并掩盖「系统里已经存在另一个提供方的 key」
这一事实。有专门测试 `test_load_dotenv_不会以_override_true_调用` 把守这条规则。

> **已知踩坑（排错时按这个顺序查）**：正因为系统环境变量优先，**shell 里残留的同名
> 变量会把 `.env` 的值完全挡住**，请求会带着一个陈旧 Key 走，返回 401。
> 21A 真实 smoke 就撞过一次：`.env` 里的 Key 与 loader 实际读到的 Key 长度
> 都不一样（51 vs 67 字符），而当时先去试了四种鉴权头，全都 401，
> 差点误判成「Key 被端点拒绝」。
> 正确的第一步永远是**比对「配置文件里的值」与「loader 读到的值」是不是同一个**
> （哈希比对即可，不打印明文）：

> ```python
> # 一致 → 配置链路正常，问题在别处；不一致 → 就是环境变量在挡
> ```

> 清理残留变量：PowerShell `Remove-Item Env:MIMO_API_KEY`（仅当前会话）。


### `load_model_settings(env, dotenv_path)`

- `env` 为显式映射时**只读该映射**（测试注入用，不碰真实进程环境）
- `env=None` 时先 `load_dotenv(dotenv_path)` 再读 `os.environ`
- `dotenv_path=None` 时按 python-dotenv 默认规则查找

> 为什么需要 `dotenv_path` 这个参数：python-dotenv 的默认查找是
> **沿调用栈第一个非 dotenv 帧的文件目录向上找**，不是看 cwd。生产环境从
> `model_config.py` 出发能找到仓库 `.env`，但在 pytest 下会从测试文件出发解析到
> 完全不同的位置。依赖这种隐式发现做断言既不可靠也不可复现，因此把
> 「哪份 .env 参与解析」显式化为参数。生产调用方一律传 `None`，行为不变。

### 已移除的旧变量

| 旧变量 | 移除理由 |
|---|---|
| `DASHSCOPE_API_KEY` | 属于阿里云 DashScope。继续兼容会在 DashScope 与其他提供方之间**串用凭据** |
| `BASE_URL` / `MODEL_NAME` | 名字过于通用，易与无关进程的环境变量撞名 |
| `MINIMAX_TIMEOUT_SECONDS` / `MINIMAX_MAX_RETRIES` | 已去提供方化，改名为 `MINI_DEERFLOW_MODEL_*` |

这些变量在本配置层被**完全忽略**：既不被读取，也不会被写回。若系统环境中残留
同名变量，不会产生任何影响。

### fail-closed

```
provider ∈ {minimax, mimo} 且对应 api_key 缺失/空白
  → load_model_settings 抛 ModelConfigError（提示该 provider 的 Key 变量名）
  → create_model 再次校验（防御直接构造 ModelSettings 的场景）
  → 绝不降级返回 FakeModel
```

两道闸的原因：前者让 CLI/FastAPI **启动时**就失败并给出可读提示，
后者保证任何调用路径都无法绕过。`minimax` 与 `mimo` 共用这套规则，
各自只认自己的 Key 变量。

## 统一注入链路

CLI 与 FastAPI 共用同一条链路，不存在两套 provider 选择逻辑：

```
load_model_settings()
        ↓
RuntimeConfig.model_settings
        ↓
create_runtime(...) → create_model(config.model_settings)
        ↓                    ├─ provider=fake    → FakeModel
                             └─ provider=minimax → AnthropicCompatModel
        ↓
Agent(model=...)
```

| 调用方 | 注入点 |
|---|---|
| CLI | `cli.main()` → `RuntimeConfig(model_settings=load_model_settings())` |
| FastAPI | `_default_runtime_factory` / `_settings_runtime_factory`（均在 **lifespan 内**调用） |
| 测试 | `create_runtime(..., model=...)` 显式传入，或默认 `ModelSettings()` → fake |
| E2E | `tests/e2e_server.py` 自带 `runtime_factory`，刻意保持 FakeModel 派生的 `SlowFakeModel` |

### 时序约束

- **模块 import 与 `create_app()` 不读配置、不建客户端、不联网**
  （`_build_factory` 只返回函数引用，真正执行在 lifespan）
- 客户端在 `AnthropicCompatModel.__init__` 创建，而 `__init__` 由 `create_runtime`
  在 runtime 真正启动时触发；`anthropic.Anthropic(...)` 构造本身不发网络请求
- 真正的网络只发生在 `decide()`

### 默认离线保证

`RuntimeConfig.model_settings` 默认是 `ModelSettings()`（`provider=fake`），
因此既有测试与本地开发完全不读环境、不联网。

额外两道保险：

1. `tests/conftest.py` 的 autouse 夹具把 `dotenv.load_dotenv` 打成空操作，
   并把 `MINI_DEERFLOW_MODEL_PROVIDER` 钉死为 `fake`
2. `pyproject.toml` 的 `testpaths = ["tests"]`，让 `src/` 下的
   `mimo_smoke_test.py` 不被 pytest 收集

## AnthropicCompatModel（21A → 21B）

```python
def decide(
    messages: list[Message],
    tools: list[ToolSpec] | None = None,
) -> ToolCall | FinalAnswer
```

- 文本响应 → `FinalAnswer`
- `tool_use` 块 → `ToolCall`，**保留 provider 的 `id`（call_id）**
- 若响应只有 `thinking` 之类的非文本块，按「无文本内容」抛
  `ModelEmptyResponseError`，不猜测语义、不截断当回答

### 消息转换

| 内部 role | 供应商 role | 还原形态 |
|---|---|---|
| `user` | `user` | 文本 |
| `assistant`（带 `tool_call_id`+`tool_name`） | `assistant` | `tool_use` 块 |
| `assistant`（其余） | `assistant` | 文本 |
| `tool`（与紧邻上一条 assistant 同 `call_id`） | `user` | `tool_result` 块 |
| `tool`（孤立，无配对 `tool_use`） | `user` | 普通文本（防御性退回） |

供应商要求 `tool_result` **必须紧跟**它的 `tool_use`，因此只有
「本条是 tool + 带 call_id + 上一条 assistant 携带相同 call_id」时才还原成
`tool_result`；否则退回普通用户文本，绝不发出找不到 `tool_use` 的
`tool_result`（会被供应商拒收）。

空消息列表与未知角色都抛 `ModelError`。

多块文本按序拼接后**只对整体 strip**——逐块 strip 会吃掉块边界上的空格。

### 工具字段

`anthropic_tools_from(specs)` 把 provider-neutral `ToolSpec` 转成 Anthropic 的
`tools` 字段；**只有本轮提供了工具才写入 `tools` 参数**，不传时请求体与 21A
逐字段一致，纯文本路径不受影响。

schema 必须是对象且 `type == "object"`，否则抛 `ModelProtocolError`。

### 严格校验（21B）

| 情况 | 行为 |
|---|---|
| 一次返回多个 `tool_use` | 抛 `ModelProtocolError`，**绝不静默取第一个** |
| `tool_use` 缺 `id` / 缺 `name` | 抛 `ModelProtocolError` |
| `tool_use.input` 不是 JSON object | 抛 `ModelProtocolError` |
| 缺少 schema 声明的必填字段 | 抛 `ModelProtocolError` |
| 工具名不在本轮 `tools` 里 | 不在此处报错，交给 `ToolRegistry` 抛 `ToolNotFoundError` |

**为什么不静默取第一个**：多出的 `tool_use` 对模型来说是「已经发出的调用」，
丢掉它会让模型以为工具执行了却永远拿不到结果，产生无法排查的错误答案。

### 错误映射

```
anthropic.APITimeoutError                    → ModelTimeoutError
AuthenticationError(401) / PermissionDenied(403) → ModelAuthError
RateLimitError(429)                          → ModelRateLimitError
APIStatusError(402)                          → ModelServerError「模型账户余额不足」
APIStatusError(status >= 500)                → ModelServerError
APIStatusError(其他)                          → ModelServerError
APIConnectionError                           → ModelServerError
APIError(兜底)                                → ModelServerError
响应无文本块 / 只有非文本块 / 全空白           → ModelEmptyResponseError
响应违反工具协议（多 tool_use、缺字段等）      → ModelProtocolError
```

异常层级（`model_errors.py`）：

```
ModelError
├── ModelConfigError        配置缺失/非法（启动期 fail-closed）
├── ModelTimeoutError
├── ModelAuthError
├── ModelRateLimitError
├── ModelServerError
├── ModelEmptyResponseError
└── ModelProtocolError      响应结构违反约定（21B，多 tool_use / 缺字段等）
```

**凭据不泄漏**：

- 异常消息只含状态码 / 异常类型名 / 模型名，绝不包含原始 SDK message
- `AnthropicCompatModel.__repr__` / `ModelSettings.__repr__` 脱敏（`api_key='***'`）
- 鉴权失败用 `raise ... from None` **断开 cause 链**——凭据问题最贴近原始凭据，
  不让它进 traceback 与日志
- `LoggingMiddleware` 记录的 `error_message` 取自 `str(exc)`，因此上面这条同样
  保护了运行期日志

## 真实 smoke

`mimo_smoke_test.py` 是**独立、显式执行**的，不进默认 pytest：

```bash
uv run python -m mini_deerflow.mimo_smoke_test
```

| 情况 | 行为 | 退出码 |
|---|---|---|
| 未选 minimax 或无 Key | 跳过，不消耗额度 | 2 |
| 配置本身非法 | 报配置错误 | 1 |
| 请求失败 | 打印异常类型与消息（不含 Key） | 1 |
| 成功 | 打印模型名、接口地址、非空回答 | 0 |

21A 相对旧版的关键区别：

- 旧版直接调 `client.messages.create()`，只证明 SDK 能连通；
  新版走 `AnthropicCompatModel.decide()`，端到端验证 Message 转换 → SDK 调用 → 响应解析 →
  `FinalAnswer` → 错误映射
- **旧版自带 `load_dotenv(override=True)`，新版完全不碰环境变量**，
  只调用与 CLI / FastAPI 相同的 `load_model_settings()`。有测试断言它源码里
  不含 `load_dotenv` / `override=True` / `os.environ`

要跑真实模型需显式覆盖 provider（系统环境变量优先于 `.env`）：

```bash
MINI_DEERFLOW_MODEL_PROVIDER=minimax uv run python -m mini_deerflow.mimo_smoke_test
```

## 测试分层

| 层级 | 文件 | 数量 | 联网 |
|---|---|---|---|
| 配置（三层优先级 + mimo + 传输层变量） | `tests/test_model_config.py` | 36 | 否 |
| 适配器（21A 文本 + 21B 工具往返） | `tests/test_anthropic_compat_model.py` | 36 | 否（注入假客户端） |
| Factory 与注入链路 | `tests/test_model_factory.py` | 25 | 否 |
| 21B 工具闭环（协议 / 两轮 / 预算 / 不半提交） | `tests/test_tool_loop_21b.py` | 29 | 否 |
| 21B CLI / API / SSE 事件 | `tests/test_phase21b_events.py` | 7 | 否 |
| Agent 循环（含工具失败不落库） | `tests/test_agent.py` | 15 | 否 |
| **21B 定向合计** | | **148** | 否 |

- 全部 mock，**不消耗额度**
- 覆盖：消息转换、文本转 `FinalAnswer`、空响应、timeout、401/403、429、5xx、
  402 余额不足、factory 选择、minimax 缺 Key fail-closed、smoke/CLI/API 共用同一配置入口、
  系统环境变量覆盖 `.env`、`.env` 在系统变量缺失时生效、
  `DASHSCOPE_API_KEY` 不能充当 MiniMax Key、新旧变量共存时只用新变量、
  Key 不出现在异常与日志
- 21B 额外覆盖：ToolSpec → Anthropic `tools` 转换、`tool_use`/`tool_result` 往返、
  `call_id` 保留、孤立 `tool` 退回、必填参数校验、多 `tool_use` 拒绝、
  tool → result → final 两轮闭环、Budget / max_steps、失败不提交 Thread、
  FakeModel 回归、SSE `model.*` / `tool.*` 事件与唯一终态、
  API 同步 run 持久化、CLI `--trace` 闭环
- CLI/API 链路用 monkeypatch 观察 `load_model_settings` 与 `create_runtime`
  实际收到的 `model_settings`，而不是靠字符串匹配
- 三者是否同一入口用「函数对象同一性」断言：`len({smoke.fn, cli.fn, api.fn}) == 1`

## 依赖

`anthropic>=1.7.0` **已是正式 runtime dependency**（`pyproject.toml`
`[project].dependencies`，`uv.lock` 同步锁定 1.7.0），并非环境中偶然安装，
无需补入。`uv pip check`：33 个包全部兼容。

## 21B：ToolCall 闭环

### 数据流

```
ToolRegistry.tool_specs()          本地：只读纯数据，handler 不离开注册表
        ↓  ToolSpec (name/description/input_schema)
Agent 每轮 decide(messages, tools)
        ↓  anthropic_tools_from()
AnthropicCompatModel → messages.create(tools=[...])
        ↓  tool_use 块
ToolCall(name, arguments, id=call_id)      领域对象，无 SDK 类型
        ↓  Agent → ToolRegistry.execute()
str 结果 → Message(role="tool", content, tool_call_id=call_id)
        ↓  provider_messages_from() 按 call_id 配对还原
tool_result 块 → 下一轮 decide()
        ↓
FinalAnswer
```

### 供应商无关的工具协议

| 概念 | 承载 | 位置 |
|---|---|---|
| name / description / input_schema | `ToolSpec` | `models.py` |
| call_id | `ToolCall.id`、`Message.tool_call_id` | `models.py` |
| arguments | `ToolCall.arguments`、`Message.tool_arguments` | `models.py` |
| tool result | `Message(role="tool", content, tool_call_id)` | `models.py` |
| 可执行 handler | `Tool.handler` / `ToolRegistry` | `tools.py` |

**Anthropic SDK 类型只出现在 `anthropic_compat_model.py` 与其测试中**
（`grep -rn "import anthropic" src/` 只命中这一个模块），不会进入
`Agent` / `ToolRegistry` / `ThreadStore`。

### 边界

- **Model 不持有 ToolRegistry**：`decide()` 只收 `list[ToolSpec]`。
  注册表带 handler（可执行对象），交给模型适配层等于把可执行能力泄漏到传输层
- **每轮都传**：`Agent` 循环每步重新取 `registry.tool_specs()`，模型看到的
  始终是「当前允许调用的工具」
- **只串行单工具**：一次响应多个 `tool_use` 直接抛 `ModelProtocolError`
- **持久化形状（收口后）**：Thread 只落 `user` 与最终 `assistant`。
  工具往返——带 `call_id` 的 `assistant(tool_use)` 与 `tool_result`——
  **只**存在于本轮 `working_messages`、Trace 与 SSE 事件。
  原因：单独落一条不带 `call_id` 的 tool 消息，下一轮 working_messages 里
  没有与之配对的 `tool_use`，会形成孤儿 `tool_result`，供应商会拒收该轮请求。
  `Message.to_dict()` 对新增字段「有值才写」，`SCHEMA_VERSION` 保持 1，
  旧数据无需迁移（旧数据里的 tool 消息在读取时按孤立消息退回普通文本）
- **`allowed_tools` 运行时强制（子代理）**：暴露限制 + 执行限制双层，
  越权抛 `ToolNotAllowedError`；主 Agent 不受影响（`allowed_tools=None`）
- **失败不半提交**：未知工具 / 非法参数 / 工具异常 / Budget 超限 / 超步数
  都在 `thread.extend()` 之前抛出，Thread 保持原样
- **已实现 token streaming**：模型逐 token 推送 SSE 事件（`model.token`），前端按 token 增量渲染

### SSE / Trace 事件

同一 `run_id` 贯通，序列号连续自增，终态唯一且为最后一个事件：

```
run.started → model.started → model.completed
            → tool.started  → tool.completed        （成功）
            → model.started → model.completed
            → run.completed
```

工具失败时 `tool.started → tool.failed`，终态为 `run.failed`；
此时既没有 `tool.completed`，也不会再发 `model.*`。
Agent 层的 `Trace`（`--trace` / `trace` 配置）同步记录
`model_decision` / `tool_call` / `final_answer`。

## 21C：真实模型收口

见 [phase-21-progress.md](phase-21-progress.md) 的 21C 记录。
真实验收**最多两次**最小调用（纯文本一次、工具闭环一次），不进默认 pytest。
