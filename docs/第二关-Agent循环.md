# 第二关：模拟模型与 Agent 循环

## 这一关做了什么

搭建了 Agent 的核心调度逻辑：**模型做决策 → 调用工具 → 把结果喂回模型 → 模型再次决策 → 直到给出最终答案**。用一个确定性的 FakeModel 代替真实 LLM，使整个循环可以脱离 API 运行和测试。

## 解决了什么问题

有了工具还不够，还需要一个"调度员"来决定什么时候用工具、什么时候给答案。这个调度员就是 Agent，它的决策能力来自模型（LLM）。本关用 FakeModel 模拟了这个决策过程。

## 如何实现的

### 1. 对话协议：三个不可变数据类

```python
@dataclass(frozen=True)
class Message:
    role: str      # "user" 或 "tool"
    content: str

@dataclass(frozen=True)
class ToolCall:
    name: str      # 工具名称，如 "calculator"
    args: str      # 工具参数，如 "(12 + 8) * 3"

@dataclass(frozen=True)
class FinalAnswer:
    content: str   # 最终回答文本
```

`frozen=True` 意味着创建后不能修改，保证数据安全。

模型的 `decide` 方法只能返回 `ToolCall` 或 `FinalAnswer` 二选一——要么调工具，要么给答案，没有第三种可能。

### 2. 模型接口：Protocol

```python
class Model(Protocol):
    def decide(self, messages: list[Message]) -> ToolCall | FinalAnswer: ...
```

Protocol 是 Python 的结构化子类型（鸭子类型的形式化），任何类只要实现了 `decide` 方法就自动满足 `Model` 接口，不需要显式继承。这意味着将来替换成真实 LLM 只需实现同一个方法签名。

### 3. FakeModel：确定性模拟

```python
class FakeModel:
    def decide(self, messages):
        latest = messages[-1]
        if latest.role == "user" and latest.content.startswith("计算 "):
            return ToolCall("calculator", latest.content[3:])
        if latest.role == "tool":
            return FinalAnswer(f"计算结果: {latest.content}")
        return FinalAnswer("当前只支持计算功能...")
```

三条规则，完全确定性：
- 看到"计算"开头 → 调工具
- 收到工具结果 → 给答案
- 其他输入 → 直接回答

### 4. Agent：核心循环

```python
class Agent:
    def __init__(self, model: Model, tools: dict, max_steps: int = 10):
        self._model = model      # 依赖注入，不硬编码
        self._tools = tools      # 工具注册表
        self._max_steps = max_steps

    def run(self, user_input):
        messages = [Message("user", user_input)]
        for step in range(max_steps):
            decision = self._model.decide(messages)
            if isinstance(decision, FinalAnswer):
                return decision.content, trace
            if isinstance(decision, ToolCall):
                result = self._tools[decision.name](decision.args)
                messages.append(Message("tool", result))
        raise RuntimeError("步数耗尽")
```

核心逻辑只有三步：

```
① 把消息列表给模型，等它决策
② 如果是 ToolCall → 执行工具 → 结果追加到消息列表 → 回到 ①
③ 如果是 FinalAnswer → 返回答案，结束
```

### 5. 完整调用流程示例

以用户输入 `"计算 (12 + 8) * 3"` 为例：

```
步骤 1: Agent 把 Message("user", "计算 (12 + 8) * 3") 给 FakeModel
        FakeModel 看到"计算"前缀 → 返回 ToolCall("calculator", "(12 + 8) * 3")

步骤 2: Agent 在 TOOLS 中找到 calculator，调用 calculator("(12 + 8) * 3")
        calculator 通过 ast 解析求值 → 返回字符串 "60"

步骤 3: Agent 把 Message("tool", "60") 追加到消息列表，再次给 FakeModel
        FakeModel 看到 role="tool" → 返回 FinalAnswer("计算结果: 60")

步骤 4: Agent 返回 ("计算结果: 60", [3条轨迹记录])
```

## 关键设计决策

| 决策 | 原因 |
|---|---|
| **依赖注入**（Agent 不知道 FakeModel 的存在） | 将来替换真实 LLM 只需传入不同的 Model 实现，Agent 代码零修改 |
| **max_steps 限制** | 防止模型反复调工具不给答案的死循环，真实 Agent 中这是必备的安全措施 |
| **执行轨迹 trace** | 每一步决策、调用、结果都记录下来，方便调试和审计 |
| **Protocol 而非抽象基类** | Python 风格，鸭子类型，任何实现了 decide 方法的类自动兼容 |

## 与真实 Agent 的关系

这一关模拟的循环和真实 LLM Agent（如 LangGraph、OpenAI Assistants）本质完全一样：

```
真实版本：  用户 → GPT-4 API 调用 → ToolCall → 执行 → 结果喂回 → GPT-4 → FinalAnswer
本关版本：  用户 → FakeModel.decide() → ToolCall → 执行 → 结果喂回 → FakeModel → FinalAnswer
```

唯一的区别是 FakeModel 用 if-else 规则做决策，真实模型用神经网络。**架构完全相同。**

## 相关文件

- [src/mini_deerflow/models.py](../src/mini_deerflow/models.py) — 数据类 + Protocol + FakeModel
- [src/mini_deerflow/agent.py](../src/mini_deerflow/agent.py) — Agent 调度循环
- [tests/test_agent.py](../tests/test_agent.py) — 测试
