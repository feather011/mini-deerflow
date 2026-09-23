# 5A 阶段：ThreadState 会话状态模型

> 范围：新增 `src/mini_deerflow/thread_state.py` 与 `tests/test_thread_state.py`。
> 约束：不修改 Agent、CLI、Model、ToolRegistry；不加时间戳 / 数据库 / UUID / 序列化；无第三方依赖；不提交 Git。

---

## 1. 这一阶段做了什么

在正式引入"多轮会话"之前，先把**一个会话的状态**定义清楚。`ThreadState` 是一个持有消息历史的容器：

```
之前：会话消息散落在 Agent.run() 的局部变量 messages 里，函数结束即丢失
之后：ThreadState(thread_id, messages) —— 可以被持有、追加、快照的会话对象
```

它只是一层**数据模型**：负责"存什么、怎么存、怎么验证"，完全不知道 Agent、工具、模型的存在。本阶段刻意不动 Agent——先证明状态容器自身正确，下一阶段（5B）再接入。

## 2. 改动的文件

| 文件 | 操作 |
|---|---|
| `src/mini_deerflow/thread_state.py` | 新增：`ThreadState` 数据类 |
| `tests/test_thread_state.py` | 新增：11 个测试 |
| `agent.py` / `cli.py` / `models.py` / `tools.py` | **一行未动** |

## 3. 结构与 API

```python
@dataclass
class ThreadState:
    thread_id: str
    messages: list[Message] = field(default_factory=list)

    def append(self, message: Message) -> None           # 单条追加，类型校验
    def extend(self, messages: Iterable[Message]) -> None # 批量追加，原子性
    message_count: int                                    # 属性
    last_message: Message | None                          # 属性，空线程为 None
    def snapshot(self) -> tuple[Message, ...]             # 不可变快照
```

四个设计点：

### 4.1 `field(default_factory=list)` 而不是 `= []`

dataclass 里写 `messages: list[Message] = []`，这个 `[]` 在**类定义时创建一次**，被所有实例共享——给线程 A append，线程 B 会凭空多出消息。`default_factory` 让每次构造都重新生成独立空列表。

### 4.2 `__post_init__` 验证 thread_id

- 非字符串 → `ValueError`
- `strip()` 后为空（`""`、`"   "`、`"\t\n"`）→ `ValueError`
- **只检查不修改**：验证用 `.strip()`，但存进 `self.thread_id` 的仍是调用方原值——不在调用方不知情时偷改数据

### 4.3 `extend` 的原子性："全部成功或全部失败"

```python
def extend(self, messages: Iterable[Message]) -> None:
    items = list(messages)              # ① 先转成副本（顺带消耗生成器）
    for item in items:
        if not isinstance(item, Message):
            raise TypeError(...)        # ② 整批验证，此时尚未写入
    self.messages.extend(items)         # ③ 验证全过才一次性写入
```

如果逐个边验边写，遇到第 2 个非法元素时第 1 个已进列表——状态"半更新"，调用方拿到异常却无法恢复原状。先验证后写入保证失败时内部状态与调用前**完全一致**。

附带好处：`list(messages)` 让 `extend` 既接受列表也接受任意可迭代对象（生成器在验证阶段被安全地物化，不会出现"消耗一半"的问题）。

### 4.4 `snapshot()` 返回 tuple

`tuple` 不可变，外部拿到快照后无论怎么操作，都**不会影响**内部 `self.messages`。这是给后续阶段准备的：5B 里 Agent/调用方需要读取会话历史，只读访问应该通过快照，而不是直接摸内部列表。

```python
snap = thread.snapshot()      # tuple，与内部列表脱离
external = list(snap)
external.append(...)          # 改外部，snap 和 thread 都不受影响
```

## 5. 刻意不做（Non-goals）

- ❌ 时间戳——每条消息何时进入线程（要加也应在 5B+ 与 Message 一起设计）
- ❌ UUID 自动生成——`thread_id` 由调用方显式传入，生成策略不属于数据模型
- ❌ 数据库持久化 / 序列化——纯内存结构
- ❌ 修改 Agent / CLI / Message——每阶段只引入一个变量

## 6. 测试覆盖（tests/test_thread_state.py，11 个）

| 要求 | 测试 |
|---|---|
| 创建空 ThreadState | `test_create_empty` |
| 两线程 messages 互不共享 | `test_two_threads_do_not_share_messages` |
| append 后数量与 last_message 正确 | `test_append_count_and_last` |
| extend 保持顺序 | `test_extend_preserves_order` |
| snapshot 是 tuple | `test_snapshot_is_tuple` |
| 外部列表改动不影响 snapshot | `test_external_list_change_does_not_affect_snapshot` |
| 空线程 last_message 为 None | `test_last_message_none_when_empty` |
| 空白 thread_id 被拒绝 | `test_blank_thread_id_rejected` |
| 非字符串 thread_id 被拒绝 | `test_non_string_thread_id_rejected` |
| append 非 Message 被拒绝 | `test_append_non_message_rejected` |
| extend 非法元素零部分写入 | `test_extend_invalid_element_no_partial_write` |

```
Ran 47 tests in 0.004s — OK
```

（原 36 + 新 11；Agent / CLI / Registry / calculator 全部未受影响）

## 7. 下一步（5B 预告）

`ThreadState` 目前没人使用——这正是 5A 的意图。5B 预计的接线方向：

```
CLI 创建 ThreadState(thread_id=...)
  → 每轮用户输入 append(Message("user", ...))
  → Agent.run 接收 ThreadState 而非裸字符串
  → 消息历史从 ThreadState 读取（而非函数局部变量）
  → 多轮对话成为可能：上一轮的上下文在下一轮仍可见
```

到那时，5A 埋下的 `snapshot()`（安全读取）和类型校验（防止脏数据进入历史）才会真正发挥作用。
