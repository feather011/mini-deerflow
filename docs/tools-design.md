# tools.py 模块设计文档

> 范围：`src/mini_deerflow/tools.py` 中的安全计算器与 Tool / ToolRegistry 工具管理基础设施。
> 现状：Agent、FakeModel、CLI 仍使用旧的 `TOOLS` 字典调用方式，本模块暂不改变它们。

---

## 1. 设计目标

1. **建立工具管理基础设施**：把「工具是什么」「工具怎么注册」「工具怎么被查找和执行」从散落的字典调用中抽象出来，为后续 Agent 接入统一的工具层做准备。
2. **保持兼容**：现有 `calculator()` 函数和 `TOOLS` 字典原样保留，原有 19 个测试不改一字节继续通过。
3. **错误可诊断**：注册、查找、执行三个环节的失败各有明确异常类型，执行失败保留原始异常链（`__cause__`），便于排查。
4. **零第三方依赖**：只用标准库（`dataclasses`、`collections.abc`）。

## 2. 模块结构

```
tools.py
├── 既有部分（保持不变）
│   ├── _BINOPS / _UNARYOPS      ast 节点 → 安全运算符映射
│   ├── _eval_node()             递归求值，仅允许数字与四则运算
│   ├── calculator(expression)    安全计算，返回 str
│   └── TOOLS = {"calculator": ...}   旧调用入口（Agent/CLI 仍在用）
│
└── 新增：工具管理基础设施
    ├── ToolRegistryError            异常基类
    │   ├── DuplicateToolError       重名注册
    │   ├── ToolNotFoundError        未知工具
    │   └── ToolExecutionError       handler 执行失败（__cause__ 保留原异常）
    ├── ToolHandler                  类型别名 Callable[[dict[str, object]], str]
    ├── Tool                         不可变数据类：元数据 + handler
    └── ToolRegistry                 注册 / 查找 / 执行 / 列表
```

## 3. 核心设计

### 3.1 ToolHandler

```python
ToolHandler = Callable[[dict[str, object]], str]
```

- **入参** `dict[str, object]`：与 LLM function-calling 的 JSON arguments 解析结果天然对齐（`json.loads` 得到的就是 `dict`）。
- **返回** `str`：与现有 `calculator` 的返回约定一致——所有工具结果统一为字符串，Agent/CLI 无需按类型分支处理。
- 使用 `Callable` 别名而非基类，handler 就是普通函数，注册零样板。

### 3.2 Tool（不可变数据类）

```python
@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, object]
    handler: ToolHandler

    def execute(self, arguments: dict[str, object]) -> str: ...
```

| 字段 | 用途 |
|---|---|
| `name` | 注册表主键，也是模型调用时的工具名 |
| `description` | 供 LLM 理解工具用途（后续生成 tools schema） |
| `parameters` | JSON Schema 风格的参数声明，类型放宽为 `dict[str, object]` 以容纳任意 schema 结构 |
| `handler` | 实际执行逻辑 |

设计取舍：

- **`frozen=True` 保证元数据不可变**：Tool 一旦注册，名称等身份信息不可被改写，避免注册表出现「键与内容不一致」的状态。
  - 注意：`frozen` 只锁定字段引用，`parameters` 字典内部仍可变。这是有意的权衡——深只读需要额外包装（如 `MappingProxyType`），当前场景收益不足，按「最小实现」原则不引入。
- **`execute` 是实例方法**：调用方只需 `tool.execute(args)`，不必同时持有 `tool.handler` 两个概念；也让「查找+执行」可以在 Registry 层一站式完成。

### 3.3 ToolRegistry

```python
class ToolRegistry:
    def __init__(self): self._tools: dict[str, Tool] = {}
    def register(self, tool: Tool) -> None
    def get(self, name: str) -> Tool
    def execute(self, name: str, arguments: dict[str, object]) -> str
    def list_tools(self) -> list[Tool]
```

- **内部私有字典 `self._tools`**：外部无法绕过 `register` 直接改写，重名校验不可被规避。
- **register**：先查后写，重名抛 `DuplicateToolError`。采用「拒绝覆盖」而非「覆盖」策略——工具名是模型可见的契约，静默覆盖会导致同名工具行为在运行中漂移。
- **get**：`KeyError` 转换为 `ToolNotFoundError`，并 `from None` 断链，避免内部实现细节（`KeyError`）泄漏给调用方。
- **execute**：组合 `get` + `tool.execute`，只包装 handler 抛出的异常；`ToolNotFoundError` 从 `get` 直接穿出，不会被误包成 `ToolExecutionError`。
- **list_tools**：按 `sorted(self._tools)` 名称排序返回，**结果顺序确定**——对测试断言和 UI/CLI 展示都友好（dict 本身在 Python 3.7+ 保留插入序，但排序提供了与注册顺序无关的稳定契约）。

### 3.4 异常层次

```
Exception
└── ToolRegistryError          # 模块内所有错误的统一捕获点
    ├── DuplicateToolError     # 注册期：名字冲突
    ├── ToolNotFoundError      # 查找期：名字不存在
    └── ToolExecutionError     # 执行期：handler 抛错（__cause__ = 原异常）
```

- 调用方可按粒度处理：想兜底整个模块 `except ToolRegistryError`，想区分阶段则捕获具体子类。
- **`ToolExecutionError ... from exc`** 保证 `__cause__` 持有原始异常，traceback 同时显示包装信息与根因，测试 `test_original_exception_in_cause` 固化了该行为。

## 4. 执行流程

```
调用方
  │  registry.execute("calculator", {"expression": "1+2"})
  ▼
ToolRegistry.execute
  ├─ get("calculator")
  │    ├─ 命中 → 继续
  │    └─ 未命中 → raise ToolNotFoundError
  ▼
tool.execute(arguments)          # Tool 数据类方法
  ▼
handler(arguments)               # 即 calculator 所包裹的普通函数
  ├─ 正常返回 str → 原样返回
  └─ 抛出任何 Exception
        → raise ToolExecutionError(...) from exc
```

## 5. 兼容性策略

| 既有元素 | 处理 |
|---|---|
| `calculator()` / `_eval_node` / AST 白名单 | 一行未动 |
| `TOOLS` 字典 | 保留；Agent、CLI、旧 19 个测试继续按原方式使用 |
| 新旧关系 | 新旧两套并行。`Tool("calculator", ..., handler=calculator)` 可在不改 `calculator` 的前提下注册进 Registry，未来 Agent 切换只需改「取工具」的入口，不改工具实现 |

即：**新基础设施是旧调用方式的超集，而不是替代**。本次不接线，只备好底座。

## 6. 使用示例

```python
from mini_deerflow.tools import Tool, ToolRegistry, calculator, DuplicateToolError

registry = ToolRegistry()
registry.register(Tool(
    name="calculator",
    description="安全计算数学表达式",
    parameters={"type": "object", "properties": {"expression": {"type": "string"}}},
    handler=lambda args: calculator(str(args["expression"])),
))

registry.execute("calculator", {"expression": "(12 + 8) * 3"})  # "60"
[t.name for t in registry.list_tools()]                          # ["calculator"]
```

## 7. 测试覆盖（tests/test_tool_registry.py）

| 测试 | 断言的契约 |
|---|---|
| `test_register_and_get` | 注册后可按名取回**同一实例**（`assertIs`） |
| `test_execute` | execute 透传 arguments 并返回 handler 结果 |
| `test_duplicate_register` | 重名 → `DuplicateToolError` |
| `test_unknown_tool` | get / execute 未知名 → `ToolNotFoundError` |
| `test_list_tools_sorted_by_name` | 乱序注册，输出按名称升序 |
| `test_handler_error_wrapped` | handler 抛错 → `ToolExecutionError` |
| `test_original_exception_in_cause` | `__cause__` 是原始 `RuntimeError("boom")` |
| `test_errors_inherit_tool_registry_error` | 三个子类均继承 `ToolRegistryError` |

全套 27 个测试（原 19 + 新 8）通过。

## 8. 明确不做（Non-goals）

- 不改 Agent / FakeModel / CLI，不删 `TOOLS`，不改 calculator 调用方式；
- 不引入参数校验、异步 handler、插件发现、装饰器注册等未要求的能力；
- 不提交 Git（按本次任务约束）。

后续若让 Agent 接入，建议路径：在 Agent 初始化时构建 `ToolRegistry` 并注册 `calculator`，把现有 `TOOLS[name]` 查找替换为 `registry.execute(name, args)`——工具实现与异常语义均无需变动。
