# 6A 阶段：序列化边界

> 范围：新增 `src/mini_deerflow/serialization.py` 与 `tests/test_serialization.py`。
> 约束：不修改 Agent、CLI、ThreadStore；不读写文件、不接 SQLite；不提交 Git；无第三方依赖。
> 前置：[thread-state.md](thread-state.md)（5A ThreadState）、[thread-store.md](thread-store.md)（5B ThreadStore）。

---

## 1. 这一阶段做了什么

为 ThreadState / Message 增加一套**与语言无关的数据表示**：

```
之前：内存里的对象只能存在于进程内
之后：可以转成 dict / JSON 字符串 —— 可以存文件、写数据库、过网络
```

只是格式表达——本阶段**不**读写磁盘、不接 SQLite、不改 Agent。下一阶段（6B+）才把 JSON 落到具体存储里。

## 2. 改动的文件

| 文件 | 操作 |
|---|---|
| `src/mini_deerflow/serialization.py` | 新增：6 个函数 + 1 个常量 + 1 个异常 |
| `tests/test_serialization.py` | 新增：26 个测试 |
| `agent.py` / `cli.py` / `thread_store.py` / `thread_state.py` / `models.py` | **一行未动** |

## 3. 接口与设计

### 3.1 SCHEMA_VERSION 与异常族

```python
SCHEMA_VERSION = 1

class SerializationError(Exception):
    """序列化与反序列化过程中的所有错误均包装为 SerializationError。"""
```

`SCHEMA_VERSION` 是**未来兼容性的契约**：将来字段定义变了，把版本号加 1，旧的 JSON 仍能被识别为"老格式"并明确报错。

### 3.2 四个转换函数

```python
message_to_dict(message)  -> dict
message_from_dict(data)    -> Message     # 字段级校验：role / content 都必须是字符串
thread_to_dict(thread)    -> dict        # 包含 schema_version + thread_id + messages
thread_from_dict(data)     -> ThreadState # 顶层 + 字段级校验
```

消息级校验：

| 失败 | 异常 |
|---|---|
| 顶层不是 `Mapping`（如 `str`、`list`） | `SerializationError` |
| 缺 `role` 或 `content` | `SerializationError` |
| `role` 非字符串或空白 | `SerializationError` |
| `content` 非字符串 | `SerializationError` |

线程级校验：

| 失败 | 异常 |
|---|---|
| 缺 `schema_version` | `SerializationError` |
| `schema_version` 是 `bool` | `SerializationError`（`bool` 是 `int` 子类，必须显式排除） |
| `schema_version != 1` | `SerializationError`（明确不支持的版本） |
| 缺 `thread_id` / `messages` | `SerializationError` |
| `thread_id` 非字符串 | `SerializationError` |
| `messages` 非 `list` | `SerializationError` |
| 任何一条 message 非法 | `SerializationError`（**整批失败，无部分恢复**） |

`KeyError` / `TypeError` / `ValueError` 全部不外露——调用方只需捕获 `SerializationError`。

### 3.3 `thread_from_dict` 的原子性

```python
restored = [message_from_dict(m) for m in raw_messages]  # ① 先解析全部
thread = ThreadState(thread_id=thread_id)
thread.extend(restored)                                 # ② 全部合法才写入
return thread
```

如果第 17 条 message 非法，列表推导式立即抛 `SerializationError`，**`ThreadState` 从未被构造**——不会有"前 16 条进了 Thread、第 17 条报错"的半成品状态。

### 3.4 JSON 转换

```python
def thread_to_json(thread):
    return json.dumps(thread_to_dict(thread), ensure_ascii=False)

def thread_from_json(raw):
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise SerializationError(f"非法 JSON: {exc}") from exc
    return thread_from_dict(data)
```

`ensure_ascii=False` 是关键——默认 `json.dumps` 会把中文转成 `你好`，可读性差；关闭后输出原生中文，diff、调试、日志都更友好。`from exc` 保留原始 JSON 解析错误的链路。

### 3.5 外部依赖

只用了标准库 `json` 与 `collections.abc.Mapping`。**不引入第三方序列化库**（如 `pydantic`、`attrs`）——保持 mini-deerflow "零第三方依赖" 的约束。

## 4. 不变量（测试固化）

```python
restored = thread_from_dict(thread_to_dict(original))

restored.thread_id     == original.thread_id     # ✓ 一致
restored.snapshot()    == original.snapshot()    # ✓ 消息列表一致
restored is not original                          # ✓ 不同实例
restored.messages is not original.messages        # ✓ 列表本身也不同
restored.append(...) 不影响 original              # ✓ 互相隔离
```

## 5. 故意不做（Non-goals）

- ❌ 读写文件——纯函数，下一阶段（6B+）才把 JSON 落到磁盘/SQLite
- ❌ 自定义编码器/解码器——只处理标准 dict / JSON
- ❌ 压缩、加密、签名——基础数据透传
- ❌ 性能优化——`json.dumps` 默认即可，未考虑大列表场景
- ❌ 第三方序列化库——保持零依赖

## 6. 测试覆盖（tests/test_serialization.py，26 个）

### Message 级（8）
- 往返转换 / to_dict 形状 / 缺 role / 缺 content / role 非字符串 / content 非字符串 / 空白 role / 顶层非 Mapping

### ThreadState 级（12）
- 往返转换 / 顺序不变 / 不同实例 / 互不干扰 / dict 含 schema_version
- 缺 schema_version / 未知版本 / bool 版本（重点：bool 必排除）
- 缺 thread_id / messages 非列表 / 非法 message 不部分恢复 / 顶层非 Mapping

### JSON 级（5）
- JSON 往返 / 中文不被 `\uXXXX` 转义 / 非法 JSON 包装 / 空字符串 / JSON 顶层非对象

### 常量（1）
- `SCHEMA_VERSION == 1`

```
Ran 114 tests in 0.014s — OK
```

（原 88 + 新 26；Agent / CLI / Registry / Store / ThreadState 全部未受影响）

## 7. 下一步（6B 预告）

`thread_to_json` 与 `thread_from_json` 现在只是**表达式**——调用方拿到字符串后无法自动落地。

6B 预计的接线方向：

```
JSONFileThreadStore (实现 ThreadStore Protocol)
  ├─ create / get / save：内部用 JSON 持久化到磁盘
  ├─ 路径：~/.mini_deerflow/<thread_id>.json 或单一 store.json
  └─ 读不到旧文件时优雅处理（返回 ThreadNotFoundError）
```

到那时 `ThreadStore` 协议的价值真正体现——`InMemoryThreadStore` 与 `JSONFileThreadStore` **完全可互换**，CLI 和 Agent 不需改一行。