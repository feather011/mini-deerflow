# 4B 阶段：calculator 注册为正式 Tool

> 范围：`src/mini_deerflow/tools.py` 中新增 `calculator_handler`、`CALCULATOR_TOOL`、`create_default_registry`。
> 约束：不改 Agent、FakeModel、ToolCall、CLI；不删旧版 `TOOLS` 字典；不提交 Git。
> 前置：[tools-design.md](tools-design.md)（Tool / ToolRegistry 基础设施设计）。

---

## 1. 这一阶段做了什么

上一阶段备好了 `Tool` / `ToolRegistry` 底座，但 `calculator` 还只是旧 `TOOLS` 字典里的裸函数。本阶段把它**升级为正式的 Tool 对象**并注册进默认注册表：

```
之前：  Agent → TOOLS["calculator"] → calculator(expression)
之后：  任意调用方 → create_default_registry().execute("calculator", {"expression": "..."})
                  → CALCULATOR_TOOL.execute(args)
                  → calculator_handler(args)   ← 参数校验
                  → calculator(expression)      ← AST 安全计算（原封不动）
```

新增三样东西，一条执行链，旧通路完全不动。

## 2. 改动的文件

| 文件 | 操作 | 说明 |
|---|---|---|
| `src/mini_deerflow/tools.py` | 修改 | 新增 `calculator_handler`、`CALCULATOR_TOOL`、`create_default_registry`，其余一行未动 |
| `tests/test_calculator_tool.py` | 新增 | 10 个测试，覆盖全部7项要求 |
| `agent.py` / `models.py` / `cli.py` | **未修改** | 旧 `TOOLS` 通路继续工作 |
| `TOOLS` 字典 | **保留** | Agent 和 CLI 仍使用它 |

## 3. 三个新成员

### 3.1 calculator_handler —— 参数适配层

```python
def calculator_handler(arguments: dict[str, object]) -> str:
    if "expression" not in arguments:
        raise ValueError("缺少参数: expression")
    expression = arguments["expression"]
    if not isinstance(expression, str):
        raise ValueError("expression 必须是字符串")
    if not expression.strip():
        raise ValueError("expression 必须是非空字符串")
    return calculator(expression)
```

**为什么不直接 `handler=calculator`？** 因为 `ToolHandler` 的入参是 `dict[str, object]`（LLM function-calling 解析出来的原始 arguments），而 `calculator` 接收的是裸字符串。handler 承担两个职责：

1. **参数校验**——三道检查给出人类可读的错误信息，而不是让人看不懂的 `TypeError: 'int' object is not callable`；
2. **委托**——校验通过后直接调用 `calculator(expression)`，**AST 计算代码零复制**，安全白名单只有一份。

### 3.2 CALCULATOR_TOOL —— 工具的完整描述

```python
CALCULATOR_TOOL = Tool(
    name="calculator",
    description="计算四则运算表达式（加减乘除、括号、正负号），适用于只包含数字和 +-*/ 的数学计算。",
    parameters={
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "需要计算的四则运算表达式",
            }
        },
        "required": ["expression"],
        "additionalProperties": False,
    },
    handler=calculator_handler,
)
```

这是标准的 **function-calling schema 格式**——`description` 给 LLM 看（什么时候该用这个工具），`parameters` 是 JSON Schema（LLM 生成参数时按它填）。将来接入真实模型时，这个 dict 可以直接序列化成 API 的 `tools` 字段，无需再写一遍。

- `required: ["expression"]`——没有参数的调用是无效的；
- `additionalProperties: False`——拒绝多余参数，与 handler 的严格校验呼应。

### 3.3 create_default_registry —— 工厂函数，不是单例

```python
def create_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(CALCULATOR_TOOL)
    return registry
```

**为什么每次新建？** 全局可变单例是测试隔离的天敌：一个测试注册了临时工具，下一个测试就莫名收到 `DuplicateToolError`，失败原因与被测代码毫无关系。工厂函数保证每个调用方（每个测试、每个 Agent 实例）拿到独立的 Registry。

**`CALCULATOR_TOOL` 本身是模块级常量**——`frozen=True` 的数据类不可变，多个 Registry 共享同一个 Tool 实例没有一致性风险，只有内存上的好处。

## 4. 错误如何穿过 Registry

参数错误经 Registry 执行时的完整异常链：

```
calculator_handler 抛出 ValueError("缺少参数: expression")
        ↓
Tool.execute 直接透传
        ↓
ToolRegistry.execute 的 except 捕获
        ↓
raise ToolExecutionError("工具执行失败: calculator: 缺少参数: expression") from ValueError
        ↓
结果：ToolExecutionError.__cause__ 是原始 ValueError
```

这**不需要额外代码**——上一阶段 `ToolRegistry.execute` 里的 `raise ... from exc` 已经处理好。测试 `test_error_via_registry_wrapped` 固化了这个契约：

```python
with self.assertRaises(ToolExecutionError) as ctx:
    registry.execute("calculator", {})
self.assertIsInstance(ctx.exception.__cause__, ValueError)
```

两层语义：
- 对 Registry 的调用方——统一的 `ToolRegistryError` 异常族，好兜底；
- 对排查问题的人——`__cause__` 保留根因，traceback 一目了然。

## 5. 兼容性验证

| 既有元素 | 状态 |
|---|---|
| `calculator()` 纯函数 | 一行未动，仍是唯一计算实现 |
| `TOOLS` 字典 | 保留，Agent / CLI 继续按原方式使用 |
| `agent.py` / `models.py` / `cli.py` | 未修改 |
| 原有 27 个测试 | 全部继续通过 |

新旧两套**并行**：旧通路（`TOOLS`）继续服务现有 Agent，新通路（Registry）为后续升级准备。两条通路最终调用的是同一个 `calculator`。

## 6. 测试覆盖（tests/test_calculator_tool.py）

| 要求 | 测试 |
|---|---|
| 默认 Registry 包含 calculator | `test_default_registry_contains_calculator` |
| Registry 执行 `{"expression": "(12 + 8) * 3"}` | `test_default_registry_execute` → `"60"` |
| 缺少 expression | `test_missing_expression` → `ValueError` |
| expression 不是字符串 | `test_expression_not_string` → `ValueError` |
| expression 是空字符串 | `test_expression_empty_string` → `ValueError` |
| 参数 Schema 正确 | `test_schema_correct` |
| 两次创建返回不同实例 | `test_two_registries_are_different_instances` |
| （附加）经 Registry 的错误包装 | `test_error_via_registry_wrapped` → `ToolExecutionError.__cause__` 是 `ValueError` |
| （附加）委托给 calculator | `test_delegates_to_calculator` |
| （附加）Tool 身份信息 | `test_tool_identity` |

```
Ran 37 tests in 0.004s — OK
```

（原 27 + 新 10）

## 7. 明确不做（Non-goals）

- 不改 Agent / FakeModel / ToolCall / CLI，不删 `TOOLS`；
- 不做通用参数校验框架（只在 handler 里做 calculator 需要的三道检查）；
- 不做工具自动发现 / 装饰器注册 / 异步 handler；
- 不提交 Git（按本次任务约束）。

## 8. 下一步（建议）

Agent 从 `TOOLS` 切到 Registry 只需两处改动：

```python
# Agent.__init__
self._registry = create_default_registry()   # 替代 self._tools

# Agent.run 内
result = self._registry.execute(decision.name, {"expression": decision.args})
# ToolNotFoundError 替代现有的"未知工具" ValueError 分支
```

工具实现、异常语义、现有测试都不需要变。
