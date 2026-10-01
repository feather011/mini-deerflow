# 6B 阶段：SQLiteThreadStore

> 范围：`thread_store.py` 新增 `SQLiteThreadStore`；新增 `tests/test_thread_store_sqlite.py`。
> 约束：不修改 Agent、CLI、现有 ThreadStore 接口；不用 pickle；不提交 Git；零第三方依赖（仅标准库 `sqlite3`）。
> 前置：[serialization.md](serialization.md)（6A 序列化）、[thread-store.md](thread-store.md)（5B ThreadStore）。

---

## 1. 这一阶段做了什么

给 ThreadStore 加上**第二个可插拔实现**——`SQLiteThreadStore`。线程状态不再随进程退出消失，而是持久化到磁盘上的 SQLite 数据库：

```
之前（5B）：
  InMemoryThreadStore → 进程一关数据就没了

之后（6B）：
  SQLiteThreadStore → 关掉重启数据仍在（磁盘文件 .db）
```

因为都是同一个 `ThreadStore` Protocol，CLI 和 Agent **一行没改**——这就是当初用 Protocol 抽象的收益。

## 2. 改动的文件

| 文件 | 操作 |
|---|---|
| `src/mini_deerflow/thread_store.py` | 修改：新增 `SQLiteThreadStore` 类 |
| `tests/test_thread_store_sqlite.py` | 新增：17 个测试 |
| `agent.py` / `cli.py` / `models.py` / `thread_state.py` | **一行未动** |

## 3. 表结构

```sql
CREATE TABLE IF NOT EXISTS threads (
    thread_id TEXT PRIMARY KEY,
    state_json TEXT NOT NULL
)
```

设计极简——只有两个字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `thread_id` | TEXT | 主键，直接存 ID |
| `state_json` | TEXT | `ThreadState.to_dict()` 的 JSON 字符串 |

没有为 `messages` 单独建表或拆字段——整条 Thread 作为一个 JSON 行存取，读写都是整条原子操作，避免了关系模型的复杂 JOIN。**查询频繁到需要拆表时再拆**，当前阶段明显过度设计。

## 4. 序列化方式

### 写入

```python
ThreadState.to_dict()              # dict → {"schema_version":1, "thread_id":"...", "messages":[...]}
  ↓
json.dumps(..., ensure_ascii=False)   # 中文不转 \uXXXX
  ↓
INSERT / UPDATE state_json           # 存 TEXT 列
```

### 读取

```python
SELECT state_json ...                # 取 TEXT 列
  ↓
json.loads()                         # 字符串 → dict
  ↓
ThreadState.from_dict(data)          # dict → ThreadState（带 schema_version 校验）
```

`ThreadState.from_dict` 已经做了全部字段校验（schema_version、role 白名单、bool 拒绝），SQLite 侧**不重复实现** Message 解析——序列化逻辑只有一份。

## 5. 每个方法的 SQL

| 方法 | SQL | 异常触发 |
|---|---|---|
| `__init__` | `CREATE TABLE IF NOT EXISTS threads (...)` | — |
| `create` | `INSERT INTO threads (thread_id, state_json) VALUES (?, ?)` | 主键冲突 → `ThreadAlreadyExistsError` |
| `get` | `SELECT state_json FROM threads WHERE thread_id = ?` | 空结果 → `ThreadNotFoundError` |
| `save` | `UPDATE threads SET state_json = ? WHERE thread_id = ?` | rowcount=0 → `ThreadNotFoundError` |
| `delete` | `DELETE FROM threads WHERE thread_id = ?` | rowcount=0 → `ThreadNotFoundError` |
| `exists` | `SELECT 1 FROM threads WHERE thread_id = ? LIMIT 1` | 永不抛 `ThreadNotFoundError` |
| `list_threads` | `SELECT thread_id, state_json FROM threads ORDER BY thread_id` | 按 ID 稳定排序 |

SQL 要点：

- **全部参数化**（`?` 占位符），从不拼接字符串——SQL 注入在构造层就不存在
- **写操作用 `with self._conn:`**——sqlite3 的上下文管理器自动提交事务；抛异常时回滚（原子性与 ThreadState 的 extend 语义一致）
- `ORDER BY thread_id`——SQLite 结果顺序不保证，必须显式排序才能稳定

## 6. 错误处理

```python
def _deserialize(self, raw: str, thread_id: str) -> ThreadState:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"线程 {thread_id} 的 JSON 损坏: {exc}") from exc
    return ThreadState.from_dict(data)
```

两层错误链路：

| 损坏类型 | 抛出 | 保留 |
|---|---|---|
| JSON 语法坏（截断、乱写） | `ValueError("线程 X 的 JSON 损坏: ...")` | `from exc` 保留原始 json 错误 |
| JSON 合法但字段违反 schema（如未知 role、错 schema_version） | `ValueError`（来自 `ThreadState.from_dict`） | — |

`from exc` 让 traceback 同时显示包装信息与根因，测试 `test_corrupt_json_in_get_raises` 固化了"损坏 JSON 必须报清晰错误"。

## 7. 与 InMemoryThreadStore 的对比

| | InMemoryThreadStore | SQLiteThreadStore |
|---|---|---|
| 存储 | 进程内字典 | 磁盘 .db 文件 |
| 生命周期 | 随进程消失 | 关闭后仍在 |
| 序列化 | 无需（直接存对象） | 每次读写走 JSON + ThreadState.from_dict |
| 典型用途 | 测试、快速原型 | CLI 真实持久化、生产 |
| 协议 | 同一个 `ThreadStore` Protocol | 同一个 `ThreadStore` Protocol |

**接口完全相同**，两边 13 项行为断言一一对应（重名 / 未知 / 排序 / 非法类型错误都抛同样的异常）。

## 8. 关键修复：save 的事务提交

首次测试失败，原因是 `sqlite3` 的隐式事务——`execute(UPDATE)` 只在事务里改了页，**没 commit** 就 `close()`，sqlite3 默认回滚未提交事务。

修复：`save` / `delete` 用 `with self._conn:` 包住 execute，上下文退出时自动 commit。这也让**异常时自动回滚**——rowcount 检查抛 `ThreadNotFoundError` 前，UPDATE 已在 `with` 内，异常离开 `with` 就回滚，不留半更新状态。

## 9. 测试覆盖（17 个）

| 组 | 测试 |
|---|---|
| 基础行为（13） | 初始化建表 / create+get / save 恢复 / 中文消息 / 顺序不变 / 多 Thread 隔离 / 重复 create / get 未知 / delete / delete 未知 / exists / list 排序 / save 非 ThreadState / save 未知 |
| 持久化（1） | `test_data_persists_after_reopen`：store1 写入 → close → store2 用同路径重开 → 数据仍能读到 |
| 损坏 JSON（2） | `get` 报"损坏"错误；`list_threads` 同样报错 |

全部使用 `tempfile.TemporaryDirectory` 创建临时库，测试结束自动清理，不在项目目录留垃圾文件。

```
Ran 129 tests in 0.130s — OK
```

（原 112 + 新 17；InMemory / Agent / CLI / Registry / serialization 全部未受影响）

## 10. 明确不做（Non-goals）

- ❌ 不接入 CLI——按本阶段要求，6C 才把 CLI 切到 SQLite
- ❌ 不用 pickle——只用标准 `json`，安全且跨版本
- ❌ 不手动重复 Message 解析——复用 `ThreadState.from_dict`
- ❌ 不改 Agent / CLI / 现有 ThreadStore 接口
- ❌ 不提交 Git

## 11. 下一步（6C 预告）

CLI 目前仍用 `InMemoryThreadStore`。6C 只需改一行构造：

```python
# cli.py
store = SQLiteThreadStore("threads.db")   # 替代 InMemoryThreadStore()
```

Agent、`/new`、`/list`、`/switch` 等所有命令**零改动**——它们只认 `ThreadStore` Protocol，不认具体实现。届时关掉终端再打开，之前的会话和历史都还在。