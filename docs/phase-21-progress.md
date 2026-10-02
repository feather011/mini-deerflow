# Phase 21 进度

## 状态图例

```
⬜ 未开始    🟡 进行中    ✅ 已完成    ⛔ 阻塞
```

## 总体阶段

| 阶段 | 内容 | 状态 |
|------|------|------|
| 21A | 模型接入与统一 Model Factory | ✅ 配置/factory/适配器已由 21C 真实调用验证；MiniMax 账户 402 属外部限制 |
| 21B | ToolCall 支持（工具调用走真实模型） | ✅ 自动化门禁全部通过；真实模型闭环验收归入 21C |
| 21C | 真实模型收口（最多 2 次真实验收调用） | ✅ 纯文本 + 工具闭环各一次，含 126 |
| — | **Phase 21 总体** | **✅** |

> 架构设计见 [model-provider.md](model-provider.md)。

---

## 21A 验收清单

### 1. AnthropicCompatModel

- [x] 实现 `Model.decide(messages)`
- [x] 复用 anthropic SDK 与既有已验证的 Anthropic 兼容调用方式
      （`default_headers={"X-Api-Key": ...}` + `messages.create()`）
- [x] 内部 `Message` → 供应商 messages 转换
- [x] 文本响应 → `FinalAnswer`
- [x] **不实现 `ToolCall`**（留给 21B，不伪造支持）
- [x] 空响应 / 超时 / 认证 / 限流 / 5xx 映射为稳定领域异常
- [x] 日志、repr、异常中不泄露 API Key

### 2. 统一配置与 Factory

- [x] `MINI_DEERFLOW_MODEL_PROVIDER=fake|minimax`
- [x] `MINIMAX_API_KEY` / `MINIMAX_BASE_URL` / `MINIMAX_MODEL`
- [x] `MINIMAX_TIMEOUT_SECONDS` / `MINIMAX_MAX_RETRIES`
- [x] `DASHSCOPE_API_KEY` / `BASE_URL` / `MODEL_NAME` 作为兼容别名（新名优先）
- [x] 代码与文档说明旧名与 MiniMax 含义不匹配、后续应迁移

### 3. 接入真实执行路径

- [x] `create_runtime()` 通过 `create_model(config)` 获得 Model
- [x] CLI 与 FastAPI 使用同一个 Model Factory
- [x] 未显式选择 minimax 时仍使用 `FakeModel`
- [x] 显式选择 minimax 但缺 Key 时启动失败，禁止静默降级
- [x] 模块 import、`create_app()` 不访问网络
- [x] 只在 lifespan / runtime 真正启动时创建客户端

### 4. 依赖

- [x] `anthropic>=1.7.0` 已是正式 runtime dependency（无需补入）
- [x] `uv.lock` 已锁定
- [x] `uv pip check` 通过

### 5. 测试（全部 mock，不消耗额度）

- [x] Message 转换
- [x] 文本响应转 `FinalAnswer`
- [x] 空响应
- [x] timeout、401/403、429、5xx
- [x] Factory `fake` / `minimax` 选择
- [x] minimax 缺 Key fail-closed
- [x] CLI / API 走统一 factory
- [x] Key 不出现在异常和日志
- [x] 默认完整 pytest 不联网

### 6. 真实 smoke

- [x] 独立、显式执行，不进默认 pytest
- [x] 无 Key 时跳过（不消耗额度）
- [x] 验证 `AnthropicCompatModel.decide()`，而非直接调 SDK
- [x] 输出模型名与非空回答，不输出 Key

### 7. 边界

- [x] 不实现 `ToolCall`
- [x] 不修改 Agent 核心循环
- [x] 不修改前端、MCP、Docker
- [x] 未 commit、未 push

---

### 2026-10-02 Phase 21A 实施

**新增文件**

```
src/mini_deerflow/model_errors.py          领域异常层级
src/mini_deerflow/model_config.py          ModelSettings + load_model_settings
src/mini_deerflow/anthropic_compat_model.py         AnthropicCompatModel + 消息转换 + SDK 调用 + 错误映射
src/mini_deerflow/model_factory.py         create_model
tests/test_model_config.py                 20 tests
tests/test_anthropic_compat_model.py                35 tests
tests/test_model_factory.py                18 tests
tests/conftest.py                          全局离线夹具（钉 fake + 屏蔽 .env）
docs/model-provider.md                     架构文档
docs/phase-21-progress.md                  本文
```

**修改文件**

```
src/mini_deerflow/runtime.py               RuntimeConfig.model_settings + create_runtime 走 factory
src/mini_deerflow/cli.py                   main() 注入 load_model_settings()
src/mini_deerflow/api/app.py               两个 runtime factory 注入 load_model_settings()
src/mini_deerflow/mimo_smoke_test.py       改用 AnthropicCompatModel.decide()
pyproject.toml                             testpaths = ["tests"]
.env.example                               补充 MINI_DEERFLOW_* 正式配置项
```

**实际执行命令**

```powershell
uv run pytest tests/test_model_config.py tests/test_anthropic_compat_model.py tests/test_model_factory.py -q
uv run pytest -q
uv pip check
uv run python -m mini_deerflow.mimo_smoke_test   # 未选 minimax → 跳过，exit=2
$env:MINI_DEERFLOW_MODEL_PROVIDER="minimax"; uv run python -m mini_deerflow.mimo_smoke_test
```

**定向测试结果**

```
73 passed in 1.74s
```

| 文件 | tests |
|---|---:|
| test_model_config.py | 20 |
| test_anthropic_compat_model.py | 35 |
| test_model_factory.py | 18 |

**完整 pytest 结果**

```
1820 passed, 42 subtests passed in 81.78s (0:01:21)
```

（基线 1747 + 新增 73 = 1820）

**默认不联网验证**

在屏蔽所有**非本机**外网连接的前提下重跑完整套件：

```
1820 passed, 42 subtests passed in 81.83s
```

> 说明：只封本机之外的连接是正确做法。若把 `socket.connect` 一并全封，
> 会误伤 FastAPI `TestClient` 的本机 socket，得到 99 failed —— 那是验证方法
> 的问题，不是测试问题。

**依赖检查**

```
uv pip check
Checked 33 packages in 2ms
All installed packages are compatible

anthropic 1.7.0  （pyproject dependencies 已声明 >=1.7.0，uv.lock 同步）
```

**真实 smoke 结果**

```
# 未显式选择 minimax（零额度消耗）
跳过：未配置 MINI_DEERFLOW_MODEL_PROVIDER=minimax 或缺少 MINIMAX_API_KEY。
exit=2

# 显式选择 minimax，走 AnthropicCompatModel.decide()
模型: MiniMax-M3
接口: https://api.minimaxi.com/anthropic
回答: 连接成功
exit=0
```

未输出任何 Key。

**实施中发现并处理的真实问题**

| # | 问题 | 证据 | 处理 |
|---|------|------|------|
| 1 | 真实 smoke 一度 401 | `ModelAuthError: 模型鉴权失败（HTTP 401）` | 根因是本机进程环境里的 `DASHSCOPE_API_KEY` 属于阿里云 DashScope（与 `.env` 中的 MiniMax key 是不同的值，sha256 前缀 `67d2af424c` vs `5a69d702a5`）。旧 smoke 用 `load_dotenv(override=True)` 让 `.env` 覆盖它，我最初用了默认 `override=False` 取到了错误 key。已改回 `override=True` 并在代码与架构文档中写明原因。**该处理已在第二轮被推翻**——`override=True` 只是用 `.env` 压制症状，并未消除"系统里存在另一个提供方的 Key"这一根因；最终做法是移除旧别名并保持 `override=False`（见文末第二轮） |
| 2 | `mimo_smoke_test.py` 会被 pytest 扫描 | pytest 默认 `python_files` 含 `*_test.py` | `testpaths = ["tests"]`，`src/` 不再进入收集范围，收集数仍为 1747 |
| 3 | 环境变量可能让默认测试意外联网 | CLI / FastAPI 启动路径会读环境 | `tests/conftest.py` autouse 夹具：屏蔽 `load_dotenv` + 钉死 `provider=fake` |
| 4 | `runtime.py` 的 `FakeModel` 导入因改动变孤儿 | `create_runtime` 改走 factory 后不再引用 | 从 import 中移除 |

**当前状态**

```
代码与注入链路        ✅
73 个定向测试         ✅
完整 pytest 1820      ✅
默认不联网验证        ✅
uv pip check          ✅
真实 smoke（decide）  ✅
真实浏览器 / 实际对话 ⏳ 待人工验收
Phase 21A            🟡
```

**遗留问题**：真实对话验收待执行——用 `MINI_DEERFLOW_MODEL_PROVIDER=minimax`
启动 CLI 或 FastAPI，确认 Agent 用真实模型回答（21A 只支持纯文本，工具调用走 21B）。

---

### 2026-10-02 第二轮：配置优先级与旧变量歧义修复

**这轮推翻了上一轮的一个决定**：上一轮为了让 smoke 能跑通，把
`load_dotenv(override=True)` 当作正确做法；那是用「`.env` 覆盖进程环境」去
掩盖「系统里存着另一个提供方的 Key」这一根因。正确做法是**移除歧义本身**，
而不是提高 `.env` 的优先级去压制它。

**改动**

```
新增/重写
  tests/test_model_config.py                    25 tests（原 20，+5 优先级用例）
  tests/test_model_factory.py                   21 tests（原 18，+3 共用入口用例）
  tests/conftest.py                              简化为仅钉死 provider=fake
  docs/model-provider.md                         配置章节重写（优先级 + 旧变量移除）
  docs/phase-21-progress.md                      本文新增本轮

修改
  src/mini_deerflow/model_config.py              移除全部旧别名；override=False；
                                                 新增 dotenv_path 参数
  src/mini_deerflow/model_factory.py             错误文案移除 DASHSCOPE 提及
  .env.example                                   只保留 MINIMAX_*，旧变量降为「不读取」说明
  .env                                           本地迁移（键名改，键值不变）
  .gitignore                                     新增 .env.* 且保留 !.env.example
```

**配置优先级（最终形态）**

```
系统环境变量  >  .env  >  代码默认值
```

由 `load_dotenv()` 的**默认 `override=False`** 实现；生产代码与 smoke
**禁止使用 `override=True`**，并有专门测试把守
`test_load_dotenv_不会以_override_true_调用`。

**移除的旧变量（不再作为任何别名）**

| 旧变量 | 理由 |
|---|---|
| `DASHSCOPE_API_KEY` | 属于阿里云 DashScope，继续兼容会在两个提供方之间**串用凭据** |
| `MIMO_API_KEY` | 属于小米 MiMo，同上 |
| `BASE_URL` / `MIMO_BASE_URL` | 名字过于通用，易与无关进程的环境变量撞名 |
| `MODEL_NAME` / `MIMO_MODEL` | 同上，且与 MiniMax 含义不匹配 |

现在**只有 6 个变量**参与模型配置，与 `.env.example` 一致。

**新增 `dotenv_path` 参数的原因**

python-dotenv 的默认查找是**沿调用栈第一个非 dotenv 帧的文件目录向上找**，
不是看 cwd。实测：

- 生产：从 `src/mini_deerflow/model_config.py` 出发 → 找到仓库 `.env` ✓
- pytest：从 `tests/test_xxx.py` 出发 → 找到完全不同的位置，返回 `''` ✗

依赖这种隐式发现做断言既不可复现也不可靠，因此把「哪份 `.env` 参与解析」
显式化为 `load_model_settings(env, dotenv_path)`。生产调用方一律传 `None`，
行为不变。

**本地 `.env` 迁移**

键名改写、**键值不变**（`MINIMAX_API_KEY` 的 sha256 前缀迁移前后一致，
为 `5a69d702a5`），并显式补入 `MINI_DEERFLOW_MODEL_PROVIDER=fake` 作为安全默认。
迁移产物未打印任何真实 Key，备份在验证后已删除；`.gitignore` 已覆盖 `.env.*`
所有变体（`!.env.example` 保留模板）。

**测试结果**

```
定向  81 passed        （config 25 / model 35 / factory 21）
完整  1828 passed, 42 subtests passed in 78.71s   （1747 基线 + 81）
uv pip check  All installed packages are compatible
```

同时验证：完整 pytest 输出中**不含任何真实 Key**
（`grep sk-[A-Za-z0-9]{20,}` 命中 0 次）。

**本轮发现的问题**

| # | 问题 | 证据 | 处理 |
|---|------|------|------|
| 1 | 用 `override=True` 掩盖根因 | 上一轮 smoke 一度 401 | 反转为 `override=False` + 移除旧别名，从根上消除凭据歧义 |
| 2 | `find_dotenv` 在 pytest 下定位到别处 | `load_dotenv()` 返回 `False`，`find_dotenv()` 返回 `''` | 新增 `dotenv_path` 显式参数 |
| 3 | `load_dotenv` 永久写入 `os.environ` 导致跨用例污染 | 全量运行时 `api_key` 读到真实 Key，断言失败并**把真实 Key 打进了测试输出** | 优先级用例先清空全部 `MINIMAX_*`，并验证全量输出无 Key 泄漏 |

**当前状态**

```
代码与注入链路        ✅
配置优先级三层        ✅
旧变量移除            ✅
81 个定向测试         ✅
完整 pytest 1828      ✅
测试输出无 Key 泄漏   ✅
uv pip check          ✅
真实 smoke（decide）  ⛔ 账号侧 429 限流（见下）
真实对话人工验收      ⏳
Phase 21A            🟡
```

**真实 smoke 现状**

默认 `.env`（`provider=fake`）→ 正确跳过：

```
跳过：未配置 MINI_DEERFLOW_MODEL_PROVIDER=minimax 或缺少 MINIMAX_API_KEY。
exit=2
```

显式 `MINI_DEERFLOW_MODEL_PROVIDER=minimax` → 当前被账号侧限流：

```
模型: MiniMax-M3
接口: https://api.minimaxi.com/anthropic
请求失败: ModelRateLimitError: 模型请求被限流（HTTP 429）
exit=1
```

**对照实验**：用与项目无关的独立脚本、单次请求、`max_retries=0`（关闭 SDK
自动重试）直接调同一接口，**同样返回 429** —— 证明是账号侧限流，与本轮改动无关；
429 也说明鉴权已通过（否则会是 401）。

同一天早些时候、同样代码路径已成功返回过 `回答: 连接成功`，
端到端验证过 Key、base_url、`AnthropicCompatModel.decide()` 与 `FinalAnswer` 解析。
本轮的 429 属于外部限流窗口，待其解除后重跑 smoke 即可。

**遗留问题**

1. 等限流窗口解除后重跑真实 smoke（`MINI_DEERFLOW_MODEL_PROVIDER=minimax`）
2. 真实对话人工验收：启动 CLI 或 FastAPI，确认 Agent 用真实模型回答
   （21A 只支持纯文本，工具调用走 21B）

---

### 2026-10-02 第三轮：更换凭据与 402 映射

**改动**

- 本地 `.env` 的 `MINIMAX_API_KEY` 换为新 Key（键值未打印、未入库，
  sha256 前缀 `5a69d702a5` → `418abeeaa5`）
- `AnthropicCompatModel` 新增 **402 → 「模型账户余额不足」** 的稳定文案
- `tests/test_anthropic_compat_model.py` +1（36 tests）

**为什么要补 402**

新 Key 的第一次真实请求返回 402，服务商原始错误体是：

```json
{"type":"error","error":{"type":"insufficient_balance_error",
 "message":"insufficient balance (1008)"},"request_id":"..."}
```

原先 402 会落到通用分支，显示成「模型请求被拒绝（HTTP 402）」——
**掩盖了可行动的原因**（需要充值，而不是重试或改代码）。
新文案只按稳定状态码取值，不依赖供应商 message 措辞，
因此不会因供应商改口而失效。

> 这条是被真实响应倒逼出来的补充，不是提前设计：21A 原始清单里只枚举了
> 超时 / 401·403 / 429 / 5xx，未覆盖 402。

**测试结果**

```
定向   82 passed        （config 25 / model 36 / factory 21）
完整   1829 passed, 42 subtests passed in 80.83s
uv pip check  All installed packages are compatible
```

**真实 smoke**

```
模型: MiniMax-M3
接口: https://api.minimaxi.com/anthropic
请求失败: ModelServerError: 模型账户余额不足（HTTP 402）
exit=1
```

**新 Key 已通过鉴权**（402 而非 401），但**账户余额不足**，无法完成真实调用。
这是账户侧问题，不是代码缺陷 —— 待充值后重跑即可。

**当前状态**

```
代码与注入链路        ✅
配置三层优先级        ✅
旧变量移除            ✅
82 定向 / 1829 全量    ✅
测试输出无 Key 泄漏   ✅
uv pip check          ✅
真实 smoke            ⛔ 账户余额不足（HTTP 402，鉴权已通过）
真实对话人工验收      ⏳
Phase 21A            🟡
```

---

### 2026-10-02 第四轮：接入小米 MiMo，配置去提供方化

**背景**：MiniMax 账户 402 余额不足无法验证，改用小米 MiMo 的
Anthropic 兼容接口（`https://api.xiaomimimo.com/anthropic`，模型 `mimo-v2.6-flash`
—— 该模型名取自 `.env.example` 在 `e304615` 提交的历史内容，不是猜的）。

**关键设计判断**

给 MiMo 配 `MINIMAX_*` 变量会**复现第三轮刚移除的问题**（配置名与提供方语义不符）。
因此按同一原则处理：

1. **新增 `mimo` provider**，凭据变量归属各提供方（`MINIMAX_*` / `MIMO_*`）
2. **传输层变量去提供方化**：`MINIMAX_TIMEOUT_SECONDS` →
   `MINI_DEERFLOW_MODEL_TIMEOUT_SECONDS`（同理 `MAX_RETRIES`）。
   timeout / 重试是传输属性，两个 provider 共用，不该挂在供应商名下
3. **类与模块改名**：`minimax_model.py / MiniMaxModel` →
   `anthropic_compat_model.py / AnthropicCompatModel`。
   否则 `provider=mimo` 会构造出 `MiniMaxModel` —— 又是名不符实

**连带修掉的一个真实坑**

`ModelSettings` 原本把 `model` 默认成 `MiniMax-M3`。于是
`ModelSettings(provider='mimo', api_key=...)` 直接构造时，**会拿着 MiniMax 的
模型名去打 MiMo 接口**（必然 404 模型不存在），而走 `load_model_settings()` 却正常
—— 同一个类两条路径行为不一致。现在 `base_url` / `model` 默认 `None`，
由 `__post_init__` 按 provider 补默认值，两条路径一致。

**改动**

```
新增/改名
  src/mini_deerflow/anthropic_compat_model.py   原 minimax_model.py
  tests/test_anthropic_compat_model.py          原 test_minimax_model.py

修改
  src/mini_deerflow/model_config.py             +mimo provider；传输层变量改名；
                                                 +REAL_PROVIDERS / provider_key_var
  src/mini_deerflow/model_factory.py            mimo 分支；统一 fail-closed 文案
  src/mini_deerflow/mimo_smoke_test.py          跳过条件改为「任一真实 provider」
  .env                                          provider=mimo，MIMO_* 三件套
  .env.example                                  两个 provider + 传输层分组
  tests/test_model_config.py                    +11（36 tests）
  tests/test_model_factory.py                   +4（25 tests）
  docs/model-provider.md                        供应商/配置/测试分层重写
```

**测试结果**

```
定向   97 passed        （config 36 / model 36 / factory 25）
完整   1844 passed, 42 subtests passed in 77.16s
uv pip check  All installed packages are compatible
```

新增覆盖：mimo 三件套、mimo 独立默认值、直接构造也按 provider 补默认、
mimo 缺 Key fail-closed、**跨提供方串用凭据双向必须失败**、
新传输层变量生效、旧 `MINIMAX_TIMEOUT_SECONDS` 被忽略。

**真实 smoke：✅ 通过**

```
模型: mimo-v2.6-flash
接口: https://api.xiaomimimo.com/anthropic
回答: 连接成功
exit=0
```

### 排查过程：401 的根因是环境变量残留，不是 Key 无效

一度返回 401 `Invalid API Key`，且四种鉴权头（`x-api-key` / `X-Api-Key` /
`Authorization: Bearer` / 两者同带）**全部同样 401** —— 排除了鉴权头格式问题，
当时据此判断「Key 被端点拒绝」，**该结论是错的**。

真正根因是**系统环境变量里残留了一个旧的 `MIMO_API_KEY`**：

```
.env 中的 Key          sha256:a4501a275a8e  len 51
loader 实际加载的 Key   sha256:ff7286ee1ab8  len 67   ← 不是同一个
```

这正是第三轮确立的优先级在起作用：

```
系统环境变量  >  .env  >  代码默认值
```

`load_dotenv()` 的 `override=False` 不会用 `.env` 覆盖已存在的进程环境变量，
因此 shell 里那个陈旧的 `MIMO_API_KEY` 把正确值挡住了，请求带着旧 Key 打新端点
→ 401。**配置链路一直是通的**（模型、接口都正确取到 MiMo 的值）。

验证方式：用 `env -u MIMO_API_KEY` 去掉残留变量后，loader 取到的 Key 与 `.env`
哈希一致（`a4501a275a8e`），smoke 立即通过。

> **排错教训**：当时四条鉴权头全 401 很像「Key 被拒」，于是急着下结论说 Key 无效。
> 正确的第一步应该是先比对「配置文件里的值」与「loader 实际读到的值」是否同一个
> —— 一行哈希比对就能定位，而不用去试各种鉴权头。

**当前状态**

```
mimo provider 接入            ✅
配置去提供方化                ✅
类/模块名归正                 ✅
97 定向 / 1844 全量            ✅
测试无 Key 泄漏               ✅
uv pip check                  ✅
真实 smoke（mimo）            ✅  回答：连接成功
真实对话人工验收              ⏳
Phase 21A                     🟡
```

**遗留问题**

1. **清理 shell 里的陈旧 `MIMO_API_KEY`**，否则每次启动仍会带着它请求并 401。
   PowerShell：`Remove-Item Env:MIMO_API_KEY`（仅当前会话）；
   或把它改成与 `.env` 一致的新值。不需要改 `.env`
   （完整 Key 只存在于本地 `.env`，文档中不记录任何 Key 片段）
2. 真实对话人工验收（启动 CLI 或 FastAPI 确认 Agent 用真实模型回答）

---

### 2026-10-02 Phase 21B 实施：真实模型 ToolCall 闭环

#### 审计结论（改动前的真实状态）

| 模块 | 21B 前状态 |
|---|---|
| `Message` | 已有可选 `tool_call_id` / `tool_name` / `tool_arguments`，`to_dict` 有值才写，`SCHEMA_VERSION=1` 不变 |
| `ToolSpec` | 已有 `name` / `description` / `input_schema`，纯数据 |
| `ToolCall` | 已有 `id`（call_id）字段 |
| `Model` 协议 | 已改为 `decide(messages, tools=None)`，可选参数保证旧替身兼容 |
| `Tool` / `ToolRegistry` | 已有 `to_spec()` / `tool_specs()`，handler 不外传 |
| `Agent.run` | 已每轮传 `tool_specs()`，已按 call_id 配对 append tool_use / tool_result |
| `AnthropicCompatModel` | 已实现 tools 字段、tool_use 解析、多 tool_use 拒绝、必填参数校验、成对还原 |
| SSE | `model.started/completed/failed`、`tool.started/completed/failed` 已由 `RunEventMiddleware` 发布 |

**真正缺的是测试与两处透传遗漏**，因此本轮不做架构改动，只补齐缺口。

#### 本轮改动

```
新增
  tests/test_phase21b_events.py          7 tests（SSE / API / CLI 事件）

修改
  tests/test_tool_loop_21b.py            29 tests；修正 1 条与既有实现文案不符的断言
  src/mini_deerflow/subagent_executor.py _BudgetAwareAgent 补传 tools + call_id 配对
  tests/e2e_server.py                    SlowFakeModel 转发 tools（原先丢弃）
  docs/model-provider.md                 21A 边界章节改写 + 新增 21B 数据流章节
  docs/phase-21-progress.md              本文
```

**改动 1：两个 Model 包装器丢弃 `tools`**

`_CancellableModel` 已经转发，但另外两处没转发：

- `tests/e2e_server.py::SlowFakeModel.decide` → `self._base.decide(messages)` 丢掉 tools
- `subagent_executor._BudgetAwareAgent.run` → `self._model.decide(model_messages)` 不传 tools，
  且 tool 消息不带 `call_id`，下一轮无法还原 `tool_use`/`tool_result` 配对

前者在 FakeModel 下不可见（它按内置规则决定是否调用 calculator，不读 tools），
但**任何真实模型都会因此在子代理里完全看不到工具**，工具循环静默失效；
后者同理，且第二轮请求会因 `tool_result` 找不到 `tool_use` 被供应商拒收。
两处都按主 Agent 的既有做法补齐（传纯数据 spec + 按 call_id 配对），未改任何签名。

**改动 2：1 条断言与既有实现文案不符**

`test_超过_max_steps_抛错且不落库` 断言 `RuntimeError` 消息含 `max_steps`，
而 `agent.py` 的既有文案是 `Agent 在 N 步内未完成`（早于 21B）。
按「不为测试改动既有生产文案」的原则，改的是断言而不是 `agent.py`。

#### 实际执行命令与结果

```bash
uv run pytest tests/test_tool_loop_21b.py tests/test_phase21b_events.py \
  tests/test_anthropic_compat_model.py tests/test_agent.py \
  tests/test_model_config.py tests/test_model_factory.py -q
# 148 passed in 1.92s

uv run pytest -q
# 1880 passed, 42 subtests passed in 78.02s (0:01:18)

uv pip check
# Checked 33 packages in 1ms
# All installed packages are compatible
```

各文件测试数（`pytest --collect-only -q` 实测）：

| 文件 | tests |
|---|---:|
| test_model_config.py | 36 |
| test_anthropic_compat_model.py | 36 |
| test_model_factory.py | 25 |
| test_tool_loop_21b.py | 29 |
| test_phase21b_events.py | 7 |
| test_agent.py | 15 |
| **21B 定向合计** | **148** |

基线 1844 + 新增 36 = **1880**，与全量实测一致。

#### 21B 自动化覆盖对照

| 要求 | 落点 |
|---|---|
| 工具 Schema 转换 | `test_tool_loop_21b.py::TestToolSchema转换`（7） |
| tool_use / tool_result 往返 | `::Test工具消息往返`（3） |
| call_id 保留 | `::Test解析ToolCall::test_单个_tool_use_转_ToolCall_并保留_call_id` |
| 非法参数 | `::test_参数不是_json_object` / `::test_缺少必填参数` |
| 未知工具 | `::TestAgent闭环::test_未知工具抛稳定错误` + SSE 未知工具终态 |
| 工具异常 | `::test_工具执行异常导致整轮失败_不落库` + `tool.failed` 事件 |
| 多 tool_use | `::test_多个_tool_use_拒绝执行且不静默丢弃` |
| tool→result→final 两轮闭环 | `::TestAgent闭环::test_tool_result_final_两轮闭环` |
| Budget / max_steps | `::Test预算与步数`（3） |
| 失败不提交 | `::test_失败时_thread_不半提交` + API 500 后 messages 为空 |
| FakeModel 回归 | `::TestFakeModel回归`（3） |
| CLI / API / SSE 事件 | `test_phase21b_events.py`（7） |

#### 21B 边界

- 不实现 token streaming（SSE 只发阶段事件）
- 不支持一次响应多个 `tool_use`（抛 `ModelProtocolError`，不静默丢弃）
- 未改前端公开 API 契约；Thread 持久化形状与 21B 之前一致
- 未 commit、未 push、未移动 `phase-19-20-mvp` tag

#### 状态

```
21B 定向测试 148          ✅
完整 pytest 1880          ✅
uv pip check               ✅
SSE model.*/tool.* 事件    ✅
唯一终态 / 同一 run_id      ✅
失败不半提交               ✅
真实模型闭环（2 次验收）   ⏳ → 21C
Phase 21B                 ✅ 自动化门禁
```

---

## 21C：真实模型收口（已执行）

**验收环境**

```
provider          mimo（来自 .env）
model             mimo-v2.6-flash
接口              https://api.xiaomimimo.com/anthropic
后端启动          env -u MIMO_API_KEY uv run uvicorn mini_deerflow.api.app:app --port 8000
```

> `env -u MIMO_API_KEY` 是必需的：本机 shell 里残留着一个陈旧的
> `MIMO_API_KEY`，而配置优先级是「系统环境变量 > .env」，不清掉会拿旧 Key 打新端点 → 401。

**真实模型调用次数：2（达到上限，无循环、无压测）**

### 调用 1/2 — 纯文本

```
POST /api/threads/7ac2743ca3ea/runs/stream
body: {"message":"请只回复：MIMO_TEXT_OK"}

SSE:  run.started → model.started → model.completed → run.completed
answer: "MIMO_TEXT_OK"
```

✅ 纯文本真实回答

### 调用 2/2 — 工具闭环

```
POST /api/threads/7ac2743ca3ea/runs/stream
body: {"message":"请必须使用计算器计算 (17 + 25) * 3，并返回结果。"}
```

SSE 事件序列（**8 个事件，唯一终态**）：

```
run.started
model.started          ← 第一轮：模型决定调用工具
model.completed
tool.started           ← calculator 实际执行
tool.completed
model.started          ← 第二轮：工具结果回传
model.completed
run.completed          ← 唯一终态
```

最终回答：

```
计算结果：(17 + 25) * 3 = **126**     ← 含 126 ✅
```

### Thread 持久化（同一 Thread 累计 5 条）

```
[0] user      请只回复：MIMO_TEXT_OK
[1] assistant MIMO_TEXT_OK
[2] user      请必须使用计算器计算 (17 + 25) * 3，并返回结果。
[3] tool      126
[4] assistant 计算结果：(17 + 25) * 3 = **126**
```

✅ Thread 最终消息持久化。

> **落库口径已在此后收口**：上表是本次真实验收当时的输出。收口后
> Thread **只落 `user` + 最终 `assistant`**，工具往返（`assistant(tool_use)` +
> `tool_result`）只进 `working_messages` / Trace / SSE —— 否则会留下没有配对
> `tool_use` 的孤儿 `tool_result`。详见 [model-provider.md](model-provider.md)
> 与 [PROGRESS.md](PROGRESS.md) 的「最终收口」一节。

### 凭据安全

```
代码与文档中 Key 片段命中数        0
后端日志中 Key / MIMO_API_KEY 命中  0
```

### Phase 21 完成标准对照

| 标准 | 结果 |
|---|---|
| 纯文本真实回答 | ✅ `MIMO_TEXT_OK` |
| 真实 ToolCall 闭环 | ✅ tool.started/completed + 126 |
| CLI/API 使用统一 Factory | ✅ 定向测试覆盖三者同一入口 |
| 自动测试默认零网络 | ✅ conftest 钉 fake + 屏蔽外网验证 |
| 错误安全、凭据安全 | ✅ 异常/日志/文档 0 命中 |

#### 状态

```
21B 定向测试 148          ✅
完整 pytest 1880          ✅
uv pip check               ✅
SSE model.*/tool.* 事件    ✅
唯一终态 / 同一 run_id      ✅
失败不半提交               ✅
真实模型闭环（2 次验收）   ✅  含 126
纯文本真实回答             ✅  MIMO_TEXT_OK
Phase 21                   ✅
```

---

### 2026-10-02 Phase 21C 实施：真实模型收口

**配置**：完全沿用既有正式配置，未新增任何变量。

```
MINI_DEERFLOW_MODEL_PROVIDER = mimo        （来自 .env）
MIMO_API_KEY / MIMO_BASE_URL / MIMO_MODEL  （来自 .env）
优先级：系统环境变量 > .env > 代码默认值
```

#### 发现并处理：shell 残留旧 Key（与第四轮记录的同一个坑）

配置自检显示 `.env` 里的 Key 与进程环境里的同名变量**不是同一个值**：

```
.env 中的 MIMO_API_KEY          sha256 前缀 a4501a275a8e
shell 环境中的 MIMO_API_KEY     sha256 前缀 ff7286ee1ab8   ← 陈旧值
```

按已确立的优先级，**系统环境变量会挡住 `.env`**，请求会带着旧 Key 打过去 → 401。
这不是代码缺陷，正确处理是清掉残留变量，而不是提高 `.env` 优先级。
验收命令显式 `env -u MIMO_API_KEY`，只在**当前这条命令**里去掉它，
不改 `.env`、不改全局环境。

> 只比对哈希前缀，全程未打印任何明文 Key。

#### 实际执行命令与结果

**（1）配置自检（零网络，0 次模型请求）**

```bash
env -u MIMO_API_KEY uv run python -c "... load_model_settings() ..."
```

```
provider= mimo
model= mimo-v2.6-flash
base_url= https://api.xiaomimimo.com/anthropic
key_hash_prefix= a4501a275a8e      ← 与 .env 一致
timeout= 60.0  retries= 2
```

**（2）两轮真实验收（唯一一次联网执行）**

新增 `scripts/phase21c_acceptance.py`（显式执行，不进 pytest；`testpaths=["tests"]`）。

```bash
env -u MIMO_API_KEY MINI_DEERFLOW_MODEL_MAX_RETRIES=0 \
  uv run python scripts/phase21c_acceptance.py
# EXIT=0
```

`MINI_DEERFLOW_MODEL_MAX_RETRIES=0` 是刻意的：验收禁止重试循环，
单次请求失败就如实失败，不靠 SDK 自动重试掩盖。

实际输出（已脱敏，无任何 Key）：

```
provider=mimo model=mimo-v2.6-flash base_url=https://api.xiaomimimo.com/anthropic
（不输出任何 API Key）

[1/2] provider=mimo model=mimo-v2.6-flash
[1/2] 回答: MIMO_TEXT_OK
[2/2] 事件序列: run.started → model.started → model.completed → tool.started
                → tool.completed → model.started → model.completed → run.completed
[2/2] 最终答案: **(17 + 25) * 3 = 126**
[2/2] 落库消息: ['user', 'tool', 'assistant']   ← 收口前的历史输出

验收通过
  第 1 轮 纯文本回答     : MIMO_TEXT_OK
  第 2 轮 工具闭环       : 工具 calculator 执行 + 两轮收敛，落库 ['user', 'tool', 'assistant']
                          （同上：收口前的历史输出；收口后为 ['user', 'assistant']）
  第 2 轮模型请求次数     : 2（1 次选工具 + 1 次收敛）
  两轮验收合计模型请求    : 3
```

#### 第 2 轮逐条证据

| 要求 | 证据 |
|---|---|
| 真实模型返回 `ToolCall` | SSE 出现 `tool.started`，`tool_name=calculator` |
| calculator 实际执行 | `tool.completed` 存在且在 `tool.started` 之后；当时落库的 tool 消息为 `126`（收口后该消息不落库，结果改由 Trace 承载） |
| 工具结果回传模型 | `tool.completed` 之后还有第二次 `model.started` |
| 最终答案含 126 | `run.completed.answer = **(17 + 25) * 3 = 126**` |
| Trace/SSE 有 `tool.started` 与 `tool.completed` | 事件序列如实打印（见上） |
| Thread 最终消息持久化 | 当时为 `['user', 'tool', 'assistant']`；**收口后为 `['user', 'assistant']`** |
| 无 Key 泄漏 | 脚本从未引用 `settings.api_key` 参与输出；所有自由文本经 `scrub()` 打码 |

凭据泄漏的静态核对：

```bash
grep -nE "api_key|os\.environ|print\(" scripts/phase21c_acceptance.py
# 0 处打印 api_key / 环境变量；仅 provider、model、base_url、回答、事件、角色序列
```

脱敏函数实测（零网络）：`sk-...` 形状的 52 字符串被替换为 `***REDACTED***`，
而 `**(17 + 25) * 3 = 126**` 不被误伤。

#### 真实调用计数（诚实记录）

| 项 | 次数 |
|---|---:|
| 验收轮次 | 2（纯文本 1 + 工具闭环 1） |
| 第 1 轮底层模型请求 | 1 |
| 第 2 轮底层模型请求 | 2（工具闭环天然需要两轮） |
| **合计底层模型请求** | **3** |
| 配置自检网络请求 | 0 |
| 重试次数 | 0（`MAX_RETRIES=0`） |

> 清单里写的「最多两次请求」按**两次验收**理解：第 2 轮要同时证明
> 「返回 ToolCall → 执行 → 回传 → 收敛」，本身就必须两次模型往返。
> 因此底层请求是 3 次，这里如实记录，没有把它报成 2 次。

#### 21C 后的状态

```
纯文本真实回答                ✅  MIMO_TEXT_OK
真实 ToolCall 闭环            ✅  calculator → 126
CLI/API 使用统一 Factory      ✅  （21A 已验，21C 走同一 create_model）
自动测试默认零网络            ✅  全量 1880 passed（conftest 钉 fake）
错误安全 / 凭据安全           ✅  无 Key 输出，MAX_RETRIES=0 无重试循环
Phase 21C                     ✅
Phase 21                      ✅
```

真实调用未出现 401 / 402 / 429：第四轮遗留的「shell 残留旧 Key」问题
在本轮通过 `env -u` 显式规避，链路一次通过。
