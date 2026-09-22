# mini-deerflow 实现文档

## 项目目标

从零复现 DeerFlow 的核心架构，理解 Agent 系统的每一层是如何工作的。不依赖真实大模型，先用模拟逻辑跑通完整流程。

---

## 第一关：安全工具层（tools.py）

### 要解决的问题

Agent 需要调用外部工具（比如计算器），但不能直接执行用户输入的字符串，否则会带来严重的安全风险。

### 为什么不能用 eval

`eval()` 会把传入的字符串当作完整的 Python 表达式执行：

```python
# 如果允许 eval，恶意输入可以这样执行任意代码：
eval('__import__("os").system("rm -rf /")')
```

即使通过 `{"__builtins__": {}}` 限制内建函数，攻击者仍然可以利用已有对象的 `__class__`、`__subclasses__` 链绕过限制。所以 eval 从根本上不安全。

### 实现思路

使用 Python 标准库 `ast`（抽象语法树）把字符串**解析**成树形结构，然后**只遍历允许的节点类型**：

```
输入: "(12 + 8) * 3"
        ↓
ast.parse() 解析成 AST 树
        ↓
    ast.Expression
     └─ ast.BinOp(op=Mult)
         ├─ ast.BinOp(op=Add)
         │   ├─ ast.Constant(12)
         │   └─ ast.Constant(8)
         └─ ast.Constant(3)
        ↓
eval_node() 递归遍历，只处理四种节点：
  - ast.Expression  → 剥壳，进入 body
  - ast.Constant    → 数字字面量，直接返回值
  - ast.BinOp       → 二元运算，用白名单查找运算符
  - ast.UnaryOp     → 一元运算，用白名单查找运算符
        ↓
其他节点（函数调用、变量名、属性访问等）→ 抛出 ValueError
```

### 关键代码

运算符白名单：只允许加减乘除，`**`（幂运算）等不在其中，从源头禁止。

```python
_BINOPS = {
    ast.Add: op.add,
    ast.Sub: op.sub,
    ast.Mult: op.mul,
    ast.Div: op.truediv,
}
```

递归求值中的安全防线——任何不在白名单的节点类型都会走到这里：

```python
raise ValueError(f"不支持的表达式节点: {ast.dump(node)}")
```

工具注册表：为将来 Agent 调用工具提供查找入口。

```python
TOOLS: dict[str, callable] = {
    "calculator": calculator,
}
```

---

## 第二关：模型层 + Agent 循环（models.py + agent.py）

### 要解决的问题

有了工具还不够，需要一个**调度者**来决定"什么时候调用什么工具"。这就是 Agent 的角色。

### 架构设计

```
┌─────────────────────────────┐
│  Agent（调度层）              │  agent.py
│  循环：用户→模型→工具→模型→答案  │
├─────────────────────────────┤
│  Model（决策层）              │  models.py
│  Protocol 接口 + FakeModel   │
├─────────────────────────────┤
│  Tool（执行层）               │  tools.py
│  安全计算器 + 工具注册表        │
└─────────────────────────────┘
```

### 数据类设计（三个不可变数据类）

Agent 和模型之间需要一个"对话协议"，用三个 frozen dataclass 表达：

```python
@dataclass(frozen=True)
class Message:
    role: str      # "user" 或 "tool"
    content: str

@dataclass(frozen=True)
class ToolCall:
    name: str      # 工具名称
    args: str      # 工具参数

@dataclass(frozen=True)
class FinalAnswer:
    content: str   # 最终回答
```

模型的 `decide()` 方法返回 `ToolCall` 或 `FinalAnswer` 之一——这是模型对当前状态的唯一两种决策。

### 模型接口（Protocol）

用 `typing.Protocol` 定义模型应该长什么样，不绑定具体实现：

```python
class Model(Protocol):
    def decide(self, messages: list[Message]) -> ToolCall | FinalAnswer: ...
```

只要某个类实现了 `decide` 方法，它就是一个合格的 Model。FakeModel 和将来接入的真实 LLM 都满足这个协议。

### FakeModel 的决策逻辑

确定性模拟，不调用任何外部 API：

```python
def decide(self, messages):
    latest = messages[-1]

    if latest.role == "user" and latest.content.startswith("计算 "):
        return ToolCall(name="calculator", args=表达式)

    if latest.role == "tool":
        return FinalAnswer(content=f"计算结果: {latest.content}")

    return FinalAnswer(content="当前只支持计算功能...")
```

### Agent 的核心循环

Agent 是一个循环，不断在"模型决策"和"工具执行"之间切换：

```python
def run(self, user_input):
    messages = [Message("user", user_input)]

    for step in range(max_steps):
        decision = model.decide(messages)

        if isinstance(decision, FinalAnswer):
            return decision.content      # 循环结束

        if isinstance(decision, ToolCall):
            result = tools[decision.name](decision.args)
            messages.append(Message("tool", result))
            # 回到循环顶部，让模型再次决策
```

### 依赖注入

Agent 的构造函数接收 `model` 和 `tools`，不知道也不关心具体是什么实现：

```python
agent = Agent(model=FakeModel(), tools=TOOLS)
```

将来换成真实 LLM，只需替换 `model` 参数，Agent 代码完全不改。

### 执行轨迹

`run()` 返回 `(answer, trace)` 元组，trace 记录每一步发生了什么：

```python
# "计算 (12 + 8) * 3" 的执行轨迹：
[
    {"step": 1, "action": "model_decision", "detail": "调用工具 calculator，参数: (12 + 8) * 3"},
    {"step": 1, "action": "tool_call",       "detail": {"name": "calculator", "args": "(12 + 8) * 3", "result": "60"}},
    {"step": 2, "action": "final_answer",    "detail": "计算结果: 60"},
]
```

### 调用流程图

```
用户输入: "计算 (12 + 8) * 3"
    │
    ▼
┌──────────────────────────────────────────────────┐
│ Step 1: Agent 调用 model.decide([用户消息])         │
│ FakeModel 发现 "计算 " 前缀                        │
│ → 返回 ToolCall("calculator", "(12 + 8) * 3")    │
├──────────────────────────────────────────────────┤
│ Step 1: Agent 在 TOOLS 中找到 calculator           │
│ → 调用 calculator("(12 + 8) * 3")                │
│ → ast 解析并求值 → 返回 "60"                       │
├──────────────────────────────────────────────────┤
│ Step 1: Agent 将 Message("tool", "60") 加入消息列表 │
├──────────────────────────────────────────────────┤
│ Step 2: Agent 调用 model.decide([用户消息, 工具结果]) │
│ FakeModel 发现最新消息 role="tool"                 │
│ → 返回 FinalAnswer("计算结果: 60")                  │
├──────────────────────────────────────────────────┤
│ Agent 返回 ("计算结果: 60", [3条轨迹记录])           │
└──────────────────────────────────────────────────┘
```

---

## 测试覆盖

```bash
uv run python -m unittest discover -s tests -v
```

```
Ran 12 tests in 0.001s — OK
```

| 测试文件 | 测试数量 | 覆盖内容 |
|---|---|---|
| test_tools.py | 7 | 四则运算、括号、正负号、拒绝恶意输入、TOOLS 注册表 |
| test_agent.py | 5 | 计算结果、执行轨迹、普通文本、未知工具、死循环防护 |

---

## 小结

| 关卡 | 新增文件 | 核心概念 |
|---|---|---|
| 第一关 | tools.py | AST 白名单、沙箱安全、工具注册 |
| 第二关 | models.py, agent.py | 不可变数据类、Protocol 接口、依赖注入、ReAct 循环、执行轨迹 |
