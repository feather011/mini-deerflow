# 4C 阶段：Agent 正式迁移到 ToolRegistry

> 范围：`models.py`、`agent.py`、`cli.py`、`tools.py`、`tests/` 的迁移改造。
> 约束：不安装依赖、不接入 API、不提交 Git。
> 前置：[tools-design.md](tools-design.md)（4A 基础设施）、[calculator-tool-registration.md](calculator-tool-registration.md)（4B 工具注册）。

---

## 1. 这一阶段做了什么

删除 Agent 对旧 `TOOLS` 字典的依赖，让完整调用链变成一条：

```
迁移前（两套并行）：
  Agent → TOOLS["calculator"] → calculator(expression)      旧通路
  调用方 → create_default_registry() → Tool → handler        新通路（Agent 没用）

迁移后（唯一通路）：
  Agent → ToolRegistry.execute(name, arguments)
        → CALCULATOR_TOOL.execute(arguments)
        → calculator_handler(arguments)      ← 参数校验
        → calculator(expression)             ← AST 安全计算
```

从此工具的查找、校验、执行、错误语义**全部集中在 ToolRegistry**，Agent 只负责调度循环。

## 2. 改动的文件

| 文件 | 操作 | 说明 |
|---|---|---|
| `src/mini_deerflow/models.py` | 修改 | `ToolCall.args: str` → `arguments: dict[str, object]`；FakeModel 生成字典参数 |
| `src/mini_deerflow/agent.py` | 修改 | 构造函数改收 `ToolRegistry`；删除字典查找和参数校验 |
| `src/mini_deerflow/cli.py` | 修改 | 改用 `create_default_registry()` 注入 Agent |
| `src/mini_deerflow/tools.py` | 修改 | **删除旧 `TOOLS` 字典** |
| `tests/test_agent.py` | 修改 | 注入 Registry、字典参数断言、`ToolNotFoundError` |
| `tests/test_tools.py` | 修改 | 移除 `test_tools_dict`（对象已不存在） |

不保留两个并行工具系统——`grep` 确认全项目 `.py` 文件中 `TOOLS` 零残留。

## 3. 四处核心改动

### 3.1 ToolCall：参数从字符串变成字典

```python
@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, object]      # 之前是 args: str
```

**为什么改？** 旧的 `args: str` 把参数当成一段原始字符串，等于让每个工具自己去解析参数。而 LLM function-calling 的原生格式就是 **JSON 对象**（`json.loads` 解析出来就是 `dict`）。改成 `dict[str, object]` 后：

- 与真实模型 API 的 arguments 格式天然对齐，将来接入不需要再转换；
- 与 `ToolHandler` 的入参类型 `dict[str, object]` 完全一致，`registry.execute(name, arguments)` 直接透传；
- 参数有名字（`{"expression": "..."}`），多参数工具不会出现位置歧义。

FakeModel 相应地生成字典参数：

```python
        if latest.role == "user" and latest.content.startswith("计算 "):
            expression = latest.content[len("计算 "):]
            return ToolCall(name="calculator", arguments={"expression": expression})
```

### 3.2 Agent：只调度，不执行

```python
    def __init__(
        self,
        model: Model,
        registry: ToolRegistry,
        max_steps: int = 10,
    ):
        self._model = model
        self._registry = registry
        self._max_steps = max_steps
```

工具执行部分从 8 行缩到 1 行：

```python
# 之前：Agent 自己查字典 + 自己判空 + 自己抛 ValueError
tool_fn = self._tools.get(decision.name)
if tool_fn is None:
    raise ValueError(f"未知工具: {decision.name}")
result = tool_fn(decision.args)

# 之后：一行，交给 Registry
result = self._registry.execute(decision.name, decision.arguments)
```

被删掉的能力去了哪里：

| Agent 中删除的逻辑 | 由谁接管 |
|---|---|
| `self._tools.get()` 字典查找 | `ToolRegistry.get()`，未知工具抛 `ToolNotFoundError` |
| `ValueError("未知工具")` | 不存在了——错误语义统一进 `ToolRegistryError` 异常族 |
| 直接调用裸函数 | `ToolRegistry.execute` → `Tool.execute` → `handler`（参数校验也在 handler 里） |

`max_steps` 防循环和执行轨迹记录**原样保留**——调度仍是 Agent 的职责，它只是不再兼职做工具管理。

这正是**单一职责**：Agent 决定"接下来做什么"，Registry 负责"东西在哪、怎么安全地执行"。未来新增工具（比如搜索、文件读写），Agent 代码零改动。

### 3.3 CLI：注入新 Registry

```python
from mini_deerflow.tools import create_default_registry

agent = Agent(model=FakeModel(), registry=create_default_registry())
```

命令行参数、`--trace` 行为完全没变。因为 `format_trace` 用 `{detail['args']!r}` 渲染参数，`dict` 的 `repr` 天然是可读的 `{'expression': '...'}`，**字典参数直接显示正常**，格式化代码一行没改。

### 3.4 删除旧 TOOLS

```python
# 已删除
TOOLS: dict[str, callable] = {
    "calculator": calculator,
}
```

同步移除 `test_tools_dict` 测试。`calculator()` 纯函数和它的 6 个安全测试原样保留——**变的是工具如何被管理和调用，不是工具本身**。

## 4. 迁移后的完整调用链

以输入 `计算 18 / (2 + 1)` 为例：

```
用户输入 "计算 18 / (2 + 1)"
    │
    ▼
Agent.run → FakeModel.decide
    → ToolCall(name="calculator", arguments={"expression": "18 / (2 + 1)"})
    │
    ▼
Agent → registry.execute("calculator", {"expression": "18 / (2 + 1)"})
    │
    ▼
ToolRegistry.execute
    ├─ get("calculator")           # 未知 → ToolNotFoundError
    └─ tool.execute(arguments)
         │
         ▼
       calculator_handler          # 校验 expression 存在、是字符串、非空
         │
         ▼
       calculator("18 / (2 + 1)")  # AST 白名单求值
         │
         ▼
       "6.0"
    │
    ▼
Agent → Message("tool", "6.0") → FakeModel.decide → FinalAnswer("计算结果: 6.0")
```

## 5. 错误语义的统一

迁移前后，"模型请求了不存在的工具"的错误对比：

| | 之前 | 之后 |
|---|---|---|
| 抛出点 | Agent 内部手写的 `ValueError` | `ToolRegistry.get` 的 `ToolNotFoundError` |
| 异常类型 | 裸 `ValueError`，与参数错误混在一起 | `ToolRegistryError` 异常族的一员 |
| 参数错误 | 不存在校验（裸函数直调） | `ToolExecutionError`，`__cause__` 保留根因 `ValueError` |

Agent 不再重复实现 Registry 已有的错误处理——这是"不要在两处维护同一逻辑"的直接收益。

## 6. 测试结果

```
Ran 36 tests in 0.005s — OK
```

相比迁移前 37 个，减少 1 个：`test_tools_dict`（被测对象 `TOOLS` 已删除）。其余测试的迁移：

| 测试 | 改动 |
|---|---|
| `test_calculation` | 注入方式 `tools=TOOLS` → `registry=create_default_registry()` |
| `test_trace_has_tool_call` | 断言 `args` 为字典 `{"expression": "10 + 5"}` |
| `test_unknown_tool_raises` | `ValueError` → `ToolNotFoundError` |
| `test_max_steps_limit` | 仅注入方式变化，逻辑不变 |
| CLI 7 个测试 | **零改动**，全部继续通过 |

## 7. 实际 CLI 运行

```
$ uv run mini-deerflow --trace

> 计算 18 / (2 + 1)
--- 执行轨迹 ---
  [步骤 1] 模型决策: 调用工具 calculator，参数: {'expression': '18 / (2 + 1)'}
  [步骤 1] 工具调用: calculator({'expression': '18 / (2 + 1)'}) → 6.0
  [步骤 2] 最终回答: 计算结果: 6.0
--- 结束 ---

计算结果: 6.0
```

（Windows 下经管道测试时需加 `PYTHONUTF8=1` 防止中文编码损坏；真实终端无此问题。）

## 8. 关卡小结：第 4 关三步走完

| 步骤 | 内容 | 结果 |
|---|---|---|
| 4A | `Tool` / `ToolRegistry` / 异常族 | 独立正确，原 19 测试不受影响 |
| 4B | `calculator_handler` + `CALCULATOR_TOOL` + 工厂函数 | 新旧并行，37 测试通过 |
| 4C | Agent 迁移 + 删除 `TOOLS` | 单一工具系统，36 测试通过 |

分三步而不是一次重构的原因：**每一步都保持测试全绿**。4A 证明新组件本身正确，4B 证明新组件能承载现有工具，4C 才动 Agent——任何一步出问题都能立刻定位是哪一层的错，而不是在一次大改中同时调试三个层面。

## 9. 明确不做（Non-goals）

- 不接入真实 API、不安装新依赖；
- 不改 `calculator()` 及其安全测试；
- 不做工具自动发现、权限控制等未要求能力；
- 不提交 Git（按本次任务约束；4A+4B 已随 `feat: add cli and execution trace` 保存，4C 留待下次提交）。