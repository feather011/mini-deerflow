# 5B 阶段：ThreadStore 接口与 InMemoryThreadStore

> 范围：新增 `src/mini_deerflow/thread_store.py` 与 `tests/test_thread_store.py`。
> 约束：不修改 Agent、CLI、FakeModel、ToolRegistry；无 UUID / SQLite / 文件持久化 / 并发；不提交 Git。
> 前置：[thread-state.md](thread-state.md)（5A ThreadState 数据模型）。

---

## 1. 这一阶段做了什么

5A 定义了**单个会话的样子**（ThreadState），5B 定义**多个会话如何管理**（ThreadStore）：

```
5A 之后：每段对话只能住在 Agent.run() 的局部变量里
5B 之后：可以建线程、按 ID 找、删、列举 —— 像一个迷你数据库
```

未来无论是多用户多会话、还是把 Agent 切到多轮对话模式，调用方都只需面向 `ThreadStore` 接口编程——具体背后是内存字典还是 SQLite 都无所谓。

## 2. 改动的文件

| 文件 | 操作 |
|---|---|
| `src/mini_deerflow/thread_store.py` | 新增：Protocol + 异常族 + InMemoryThreadStore |
| `tests/test_thread_store.py` | 新增：14 个测试 |
| `thread_state.py` / `agent.py` / `cli.py` / `models.py` / `tools.py` | **一行未动** |

## 3. 接口与实现

### 3.1 ThreadStore Protocol

```python
@runtime_checkable
class ThreadStore(Protocol):
    def create(self, thread_id: str) -> ThreadState: ...
    def get(self, thread_id: str) -> ThreadState: ...
    def save(self, thread: ThreadState) -> None: ...
    def delete(self, thread_id: str) -> None: ...
    def exists(self, thread_id: str) -> bool: ...
    def list_threads(self) -> tuple[ThreadState, ...]: ...
```

`@runtime_checkable` 让任何实现这六个方法的类**自动满足**该协议，依赖注入和 `isinstance` 检查都能用。未来的 `SqliteThreadStore`、`RedisThreadStore` 不用继承任何基类，只需正确实现接口。

### 3.2 异常族

```
ThreadStoreError                       # 基类
├── ThreadAlreadyExistsError              # create 时 ID 冲突
└── ThreadNotFoundError                # get / save / delete 时找不到
```

具体异常归在一族里，调用方可按粒度处理——想兜底整层 `except ThreadStoreError`，想细分则捕获子类。`exists` **不抛**异常，因为它本身就是查询接口。

### 3.3 InMemoryThreadStore：协议的第一种实现

```python
class InMemoryThreadStore:
    def __init__(self) -> None:
        self._threads: dict[str, ThreadState] = {}   # 私有字典，外部无法绕过

    def create(self, thread_id: str) -> ThreadState:
        if thread_id in self._threads:
            raise ThreadAlreadyExistsError(...)
        thread = ThreadState(thread_id=thread_id)     # 复用 5A 的 thread_id 校验
        self._threads[thread_id] = thread
        return thread
    # get / save / delete / exists / list_threads 同理
```

设计要点：

| 决策 | 原因 |
|---|---|
| `_threads` 是实例属性（非类属性） | 两个 Store 实例互不共享数据（`test_two_stores_do_not_share_data` 验证） |
| create 内部 `ThreadState(thread_id=...)` | 自动复用 5A 的 thread_id 校验（空白、非字符串等） |
| save 校验 `isinstance(thread, ThreadState)` + 存在性 | 类型安全和存在性分开报；非 ThreadState 直接 `TypeError`，未知 ID 才 `ThreadNotFoundError` |
| save 即使对象是同一个也走完整流程 | 为未来 SQLite / Redis 实现统一接口（也许持久化层需要"写入"语义） |
| `list_threads` 返回 `tuple(sorted(...))` | tuple 不可变防外部修改；sorted 保证结果确定，对测试断言和 UI 列表都友好 |

## 4. 与 5A 的关系

5A 的 `ThreadState` 是**单条记录**，5B 的 `ThreadStore` 是**记录集合**：

```
ThreadState（5A）         ThreadStore（5B）
┌───────────────┐        ┌──────────────────────────┐
│ thread_id     │        │ _threads: dict[str, State]│
│ messages:[]   │        ├──────────────────────────┤
│ append()      │  ←──── │ create / get / save /     │
│ extend()      │        │ delete / exists / list     │
│ snapshot()    │        │   → 操作 ThreadState       │
└───────────────┘        └──────────────────────────┘
```

5B 没有任何 5A 没有的能力——它只是把 5A 的单条记录放进了可索引、可枚举的集合中。

## 5. 刻意不做（Non-goals）

- ❌ 自动 UUID——`thread_id` 由调用方显式传入，生成策略属于应用层
- ❌ SQLite / 文件持久化——本阶段只做接口和内存实现，下一个 SQLite 版本只需满足同一 Protocol 即可替换
- ❌ 锁与并发——单线程内存字典够用，并发安全留给将来的持久化实现
- ❌ "当前 Thread" 概念——属于 Agent 层（5C+），不是仓库层
- ❌ Agent / CLI 接入——按本阶段要求保持骨架不动，下一阶段才让 Agent 用上 Store

## 6. 测试覆盖（tests/test_thread_store.py，14 个）

| 要求 | 测试 |
|---|---|
| 创建和获取 Thread | `test_create_and_get` |
| 重复创建失败 | `test_duplicate_create_fails` |
| 获取未知 Thread 失败 | `test_get_unknown_fails` |
| exists | `test_exists` |
| save 已有 Thread | `test_save_existing_thread` |
| save 未知 Thread 失败 | `test_save_unknown_thread_fails` |
| save 非 ThreadState 失败 | `test_save_non_thread_state_fails` |
| 删除 Thread | `test_delete_thread` |
| 删除未知 Thread 失败 | `test_delete_unknown_fails` |
| list_threads 按 ID 排序 | `test_list_threads_sorted_by_id` |
| list_threads 返回 tuple | `test_list_threads_returns_tuple` |
| 两 Store 实例互不共享 | `test_two_stores_do_not_share_data` |
| 消息可再次读取 | `test_messages_persist_via_get` |
| （附加）异常继承基类 | `test_specific_errors_inherit_thread_store_error` |

```
Ran 61 tests in 0.005s — OK
```

（原 47 + 新 14；Agent / CLI / Registry / calculator 全部未受影响）

## 7. 下一步（5C 预告）

`ThreadStore` 现在也只是个独立的盒子，没被任何代码使用。5C 预计的接线方向：

```
Agent 持有 ThreadStore（依赖注入）
  → run(input, thread_id) 从 Store 取 ThreadState
  → 用户消息 append 到 ThreadState
  → 循环内历史从 ThreadState.snapshot() 读取（而非函数局部变量）
  → 最终答案 append，Agent.save(thread) 把更新后的状态写回 Store
  → 同一 thread_id 再次调用时，上下文完整保留
```

到那时，5A 的 `snapshot()` + 5B 的 `get/save` 才会真正连成一条多轮对话的链路。