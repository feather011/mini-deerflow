# Lead Agent 架构（Phase 25A + 25B）

> Lead Agent 位于现有 Agent/Subagent **之上**的编排层。本阶段只交付
> **领域模型、状态机与滚动批次校验**——纯领域代码，无 I/O、无模型调用、无调度。

## 定位与边界

```
                    Lead Agent（本阶段：领域层）
                    ├─ GoalContract / GoalAmendment
                    ├─ LeadRunState + 状态机
                    ├─ 滚动批次校验（validate_batch）
                    └─ 资源冲突判定（conflicts / validate_parallel_safe）
                              │
                    （后续阶段：Planner 调用、Router 调度、
                      Executor / Reviewer / Synthesizer）
                              │
        ┌─────────────────────┴─────────────────────┐
        ▼                                           ▼
   现有 Agent（单线程工具循环）              现有 Subagent（并行子任务）
```

**本阶段明确不做**：Planner 模型调用、Router 实际调度、Executor/Reviewer/
Synthesizer、SQLite、FastAPI/SSE、前端、真实模型调用；**不修改任何现有
Agent 行为**。

## 冻结设计

| 决策 | 落点 |
|---|---|
| 位于 Agent/Subagent 之上 | 本包只产出 `WorkItem`，不继承也不包装 `Agent` |
| 滚动批次，不生成一次性完整 DAG | 无 `Plan`/`DAG` 对象；只有 `PlanningDecision`（一次决策）+ `WorkItemProposal`（本批提案），批次之间靠 `LeadRunState` 累积 |
| 只有 `VerifiedFact` 能进入后续规划 | `EvidenceReceipt` → `VerificationResult` → `VerifiedFact` 三级晋升，规划输入类型就是 `VerifiedFact` |
| 终态不可逆 | 状态表终态映射空集；`TerminalStateError` 单独一类 |
| `WAITING_FOR_INPUT` 可恢复非终态 | 只从 `PLANNING` / `VERIFYING` 进入，且**只回到 `PLANNING`**——澄清不打断正在执行的任务 |
| ThreadState 仅在整个 Run 成功后提交 | `LeadRunState` **没有** `commit()`/`submit()` 方法，提交时机属集成阶段 |
| WorkItem 最多首次执行加一次修订 | `revised` 标志 + `RevisionLimitExceededError` |
| 并行用资源读写声明，不明则保守串行 | `ResourceAccessMode.UNKNOWN` 强制冲突 |
| 执行路线与读写方式是两个正交概念 | `ExecutionRoute`（怎么跑）与 `ResourceAccessMode`（读还是写）**彻底拆开**，不留别名 |

## 状态机

### LeadRunStatus（12 个状态，5 个终态）

```
CREATED ─► PLANNING ─► ROUTING ─► EXECUTING ─► VERIFYING ─┬─► SYNTHESIZING ─┬─► SUCCEEDED ┤
   │          │           │           │          │        │                ├─► PARTIAL   │ 终态
   │          │           │           │          │        │                └─► FAILED    │ 不可逆
   │          │           │           │          │        │
   │          │           │           │          │        ├─► EXECUTING（验证未过，返工）
   │          │           │           │          │        └─► PLANNING（目标歧义）
   │          │           │           │          │
   │          │           │           │          └─► WAITING_FOR_INPUT（验证阶段可请求澄清）
   │          │           │           │
   │          │           │           └─► CANCELLED / TIMED_OUT / FAILED
   │          │           │
   │          │           └─► CANCELLED / TIMED_OUT / FAILED
   │          │
   │          ├─► WAITING_FOR_INPUT ──► PLANNING（澄清后回规划）
   │          └─► CANCELLED / TIMED_OUT / FAILED
   │
   └─► CANCELLED（尚未开始计时，故无 TIMED_OUT）
```

关键边：

- **`EXECUTING → VERIFYING → SYNTHESIZING → SUCCEEDED / PARTIAL`**：
  滚动批次的推进主干。执行完必须先验证；**部分完成只能在收口阶段宣告**
  ——执行或验证阶段不能直接下 `PARTIAL` 判断。
- **`VERIFYING → EXECUTING`（返工）与 `VERIFYING → PLANNING`（目标歧义）**：
  验证是分叉点，另可进 `WAITING_FOR_INPUT` 请求澄清。
- **不存在 `EXECUTING → WAITING_FOR_INPUT`**：用户澄清属于「规划信息不足」
  或「验证发现歧义」，不属于正在执行中的任务；执行中途挂起会让超时、
  取消与重试语义全部变得含混。
- **`WAITING_FOR_INPUT` 只回到 `PLANNING`**：澄清是对规划信息的补充，
  不是「继续往下跑」的信号，因此 `WAITING → EXECUTING` 非法。
- **`CREATED → PLANNING`** 是唯一前进方向；尚未开始计时，
  故 `CREATED` 不允许 `TIMED_OUT`。
- **五个终态 → 任何状态（含自身）** 一律抛 `TerminalStateError`。

> 收口修正：原先的 `EXECUTING → PLANNING`（执行中直接跳回规划）已删除——
> 批次推进必须经 `VERIFYING`，只有验证发现问题才回规划。

### WorkItemStatus（10 个状态，5 个终态）

```
PROPOSED ──► READY ──► RUNNING ──┬──► SUCCEEDED（终态）
    │           │          │     ├──► CANCELLED / TIMED_OUT（终态）
    │           │          │     ├──► BLOCKED ──► READY（唯一一次修订）
    │           │          │     ├──► WAITING_FOR_INPUT ──► RUNNING
    │           │          │     └──► FAILED（终态，已用尽修订次数）
    │           │          │
    │           │          └──► 第二次仍失败：再走 BLOCKED 抛 RevisionLimitExceededError
    │           └──► REJECTED（终态）
    └──► REJECTED / CANCELLED
```

修订上限的实现（`revised` 标志在 `BLOCKED → READY` 时置位）：

```python
# 第一次 RUNNING -> BLOCKED：允许（revised 仍为 False）
# BLOCKED -> READY：标记 revised = True，这是唯一一次修订
# 第二次 RUNNING -> BLOCKED：入口处直接抛 RevisionLimitExceededError
```

## 模型与数据流

```
GoalContract ──(amend)──► GoalContract'        目标锚点，构造期拦空
      │
      ▼
LeadRunState.start()  →  status = CREATED
      │
      ▼
PlanningDecision(batch_index, proposals...)    一次滚动批次决策
      │
      ├─► validate_batch(proposals, verified_work_ids, goal)
      │         │ 失败 → 批次异常（DAG 未形成，Run 不受影响）
      │         ▼
      └─► WorkItem[]  (status=READY)
                │
                ▼
         ExecutionAttempt (sequence=1)         首次执行
                │ 失败
                ▼
             BLOCKED ──► READY ──► ExecutionAttempt(sequence=2)  唯一一次修订
                │ 第二次失败（revised=True）
                ▼
             FAILED（终态）→ 抛 RevisionLimitExceededError 先行拦截
                │ 成功
                ▼
         EvidenceReceipt[]  ──►  VerificationResult(passed)
                                      │ 通过且带证据
                                      ▼
                                VerifiedFact  ← 唯一可进入后续规划的输入
                                      │
                                      ▼
                        LeadRunState.verified_facts / verified_work_ids
                                      │
                                      ▼
                        下一批 validate_batch 的依赖解析来源
```

两条构造期硬约束：

- `VerificationResult(passed=True, receipts=())` → 拒绝（假通过）
- `VerifiedFact(receipts=())` → 拒绝（无证据的事实）

## 滚动批次校验

`validate_batch(proposals, *, verified_work_ids, goal) -> tuple[WorkItem, ...]`，

校验顺序（失败即抛，诊断优先级从具体到一般）：

1. `goal.objective` 与 `goal.acceptance_criteria` 非空
   （**第一道防线在 `GoalContract` 构造期**，批次层是防御性复核）
2. 批次内 `work_id` 唯一，且不与**已验证历史**重名
3. 单条 `objective` 非空
4. **禁止自依赖**
5. 每条依赖 ∈ `verified_work_ids ∪ 本批 work_ids`
   （滚动批次不接受「引用还不存在的未来任务」）
6. **当前批次无循环依赖**（迭代式 DFS 三色标记，返回环路径；深批次不递归）

依赖可以指向**本批内后声明**的条目——校验看集合，不看声明顺序。
菱形依赖（多父汇聚）不是环，必须放行。

## 资源冲突规则

### 资源分两类，语义不同

`is_path_resource(s)` 先分类：

| 类别 | 判定 | 规范化语义 |
|---|---|---|
| **路径型**（`file:`/`workspace:`/`path:` scheme、含分隔符、`name.ext`） | 路径 | 统一 `/`、折叠 `.` 与重复分隔符、**拒绝 `..`**、**`casefold()` 折叠大小写**、保留 scheme 与根 `/` |
| **非路径型**（`cache`、`db:Orders`、`redis:Key`） | 逻辑标识符 | 只去空白，**保持大小写**、**精确匹配** |

```python
normalize_resource("A.txt")        == normalize_resource("a.txt")   # 路径型：折叠
normalize_resource("db:Orders")    != normalize_resource("db:orders")  # 逻辑：保留
normalize_resource("./src/./x")    == normalize_resource("src/x")   # 折叠 .
normalize_resource("a/../etc")     → UnknownResourceError           # 拒绝 ..
```

> **大小写的正确结论**（收口修正）：不折叠会让 `A.txt` 与 `a.txt` 被判为
> **不同资源而允许并行**——Windows 下它们可能指向同一文件，那是**漏掉真实
> 冲突**。折叠的代价只是 Linux 上多出一些串行。保守方向只能是「多串行」，
> 不能是「漏冲突」。

### 父子路径视为重叠资源

```python
file:src            ┐
file:src/agent.py   ┘  → 同一片区域
```

路径型资源满足 `a == b` 或 `a` 是 `b` 的父目录（反之亦然）即视为重叠，
**只要双方不是读–读就判冲突**：

| 左 | 右 | 结果 |
|---|---|---|
| 父目录 WRITE | 子文件 READ | **冲突** |
| 父目录 READ | 子文件 WRITE | **冲突** |
| 父目录 READ | 子文件 READ | 允许并行 |
| `A.txt` WRITE | `a.txt` READ | **冲突**（折叠后同文件） |

非路径资源**没有父目录概念**：`db` 与 `db:orders` 是两个不同资源，
不套用路径规则。

### 读写判定

```python
conflicts(a, b):
    任一方 UNKNOWN     → True    # 副作用不明，强制串行（优先级最高）
    READ  vs READ      → False   # 读读并行
    其余（写写 / 读写）  → True
```

`UNKNOWN` 优先级最高：它是「我不知道」，不能因为对面是读就放行——
分不清时选择串行的代价只是慢，错误并行的代价是数据竞争。

`validate_parallel_safe(groups)` 对**所有组两两比较**：
资源冲突不具传递性，只比较相邻对会漏掉跨对冲突（有测试专门守这条）。

## 持久化（25B）

### 快照 + 事件，而不是完整 Event Sourcing

- **当前快照**（`LeadRunState`）用于**恢复**——重启后直接读出来继续用
- **追加事件**（`LeadDomainEvent`）用于**审计**——回答「这个 Run 经历过什么」

完整 Event Sourcing（事件作为唯一事实源、靠重放重建状态）不在本阶段：
它让每次读取都变慢，而这里快照就是权威，事件只是旁证。

事件 `sequence` **由 Store 分配**（`save()` 事务内取 `len(events)+1`），
调用方传的 `sequence` 会被覆盖，因此每个 run **单调递增且不可重复**，
并发写者之间也不会撞号。

事件 payload 拒绝密钥 / 模型 Prompt / Chain-of-Thought 键名，且**递归检查
嵌套的 dict / list**——只查顶层等于留了一扇门。比对前先把键名规范化
（小写 + 去掉连字符 / 下划线 / 空白），因此 `api_key`、`API-KEY`、`ApiKey`
是同一个键；比对**只针对整个键名**，`prompt_len` 这种只是含有敏感词的键
不受影响。

> **键过滤不是完整的秘密扫描。** 它只能拦住「按惯例命名」的敏感值，拦不住
> `{"note": "这里有个 sk-ABCDEFGHIJKLMNOPQRSTUVWX"}` 这种把密钥写进普通
> 文本的形态。因此调用方**仍然只允许提交自己构造的结构化元数据**
> （计数、ID、状态、短标签），绝不能把用户输入或模型输出整段塞进来。
> 这是防呆的第二道闸，不是安全边界本身。

Store 入口另有两道防御：`event.run_id` 必须与 `state.run_id` 一致（否则审计链会串），
`event.sequence` 一律被 Store 覆盖生成（调用方传什么都不作数）。
run_id 不符抛的是领域异常 `EventRunMismatchError`——不是 `ValueError`：
这是「拿错了审计事件」，调用方该换事件重来，而不是修输入格式。
校验发生在**写事务之前**，所以快照不更新、revision 不增、事件不写。

### Store 契约

```python
class LeadRunStore(Protocol):
    def create(self, state: LeadRunState) -> None: ...
    def get(self, run_id: str) -> LeadRunState: ...
    def save(self, state, *, expected_revision: int, event) -> int: ...
    def list_by_thread(self, thread_id, *, limit, offset) -> list[LeadRunState]: ...
    def list_recoverable(self, *, limit) -> list[LeadRunState]: ...
    def list_events(self, run_id, *, after_sequence=0, limit=100) -> list[LeadDomainEvent]: ...
    def delete(self, run_id: str) -> None: ...
```

- `InMemoryLeadRunStore` 与 `SQLiteLeadRunStore` **跑同一份契约测试**——行为必须一致
- `list_recoverable()` 只返回**非终态** Run（终态没有恢复的必要）
- `limit` / `offset` 有硬上限（`MAX_LIST_LIMIT = 200`），**禁止无界读取**
- 异常分层：`LeadRunAlreadyExistsError` / `LeadRunNotFoundError` /
  `LeadRunConflictError`（CAS）/ `LeadStoreClosedError` /
  `LeadStoreDataCorruptedError` / `LeadStoreWriteError`（写入失败但非并发）/
  `EventRunMismatchError` / `LeaseNotFoundError`

### CAS 事务过程

`save()` 的四步在**同一事务**内完成，任何一步失败整体回滚：

```
1. 读当前 revision，比对 expected_revision   ──不符→ 抛 Conflict（不覆盖）
2. 在事务内算新 sequence = MAX(sequence)+1   ──天然唯一，不会撞号
3. 写快照 + revision = 旧 + 1
4. 追加事件
```

InMemory 用同一把 `RLock` 包裹四步；SQLite 用**显式 `BEGIN IMMEDIATE`**。

为什么不用默认的 deferred：deferred 直到**第一次写**才取写锁，于是
「读 revision → 算 sequence → 写」之间存在窗口。并发写会撞上 WAL 的
`SQLITE_BUSY_SNAPSHOT`，直接抛 `OperationalError: database is locked` 给调用方。
`IMMEDIATE` 把竞争提前到事务开始，因此连接以 `isolation_level=None`
（autocommit，事务边界由本类管理），写事务显式 `BEGIN IMMEDIATE … COMMIT`，
异常时 `ROLLBACK`。

`BEGIN IMMEDIATE` 自己失败时事务**根本没开始**，因此那里不会 `ROLLBACK`
（回滚一个不存在的事务只会盖掉真正的错误信息）；只有 `transaction_started`
为真才回滚。提交失败同样先收尾再抛。

底层 sqlite 错误按「**能不能靠重读重试解决**」分类，只有前者才叫 CAS 冲突：

| 错误 | 映射 | 理由 |
|---|---|---|
| `database is locked` / `busy` / table locked | `LeadRunConflictError` | 没抢到写锁，重读重试即可 |
| `UNIQUE constraint failed: lead_events.run_id, lead_events.sequence` | `LeadRunConflictError` | 并发 sequence 竞争，同样可重试 |
| `no such table` / `disk I/O error` / 语法错 / `malformed` | `LeadStoreWriteError` | 重试也没用 |
| `CHECK constraint failed` / 其它 UNIQUE / `FOREIGN KEY` | `LeadStoreWriteError` | 重试也没用 |

把后者谎报成冲突是有害的：调用方会对着磁盘坏了的操作**无限重试**，
真正的故障被重试彻底掩盖。因此 `IntegrityError` / `OperationalError`
都不外泄，但落到的是**两类不同**的领域异常。失败事务整体回滚，
绝不残留半写的事件行。

绝不出现「状态推进了但审计没记」或反之的半截结果。

数据损坏 / 未知 `schema_version` **明确抛 `LeadStoreDataCorruptedError`**，
不静默当作空状态覆盖——那会让审计链与预算账本一起消失。

### SQLite 配置

比 `SQLiteThreadStore` 多三项（因为需要级联与并发写）：

```python
PRAGMA foreign_keys = ON      # 删 Run 时级联清事件/租约/预算记录
PRAGMA busy_timeout = 5000    # 写锁竞争时等待而非立即报 locked
PRAGMA journal_mode = WAL    # 只对文件库有效，失败静默回退（性能优化）
```

`lead_runs` 额外冗余一列 `thread_id` 供 `list_by_thread` 过滤与索引；
`LeadRunState` 始终是权威来源，该列**不反向回填**。

测试一律用 pytest 临时**文件**库，不依赖 `:memory:` 的多连接共享语义。

### 预算账本（25B）

**不新建第三套 Budget**：`LeadBudgetLedger.resources` 持有**现有的**
`middleware.Budget` 实例，模型 / 工具 / Subagent 的消耗全部记在它上面
（与 `SubagentManager(shared_budget=...)` 同一套复用）。本模块只负责
Lead 编排层新增的三类额度：

```
LeadBudgetLedger（根预算，可持久化）
  ├── resources: Budget                 现有资源：model_calls / tool_calls / steps …
  ├── 编排计数     max_batches=4 / max_work_items=12 / max_items_per_batch=4
  ├── 并发槽       max_concurrency=2
  ├── 信用额度     max_lease_credits     ← 所有租约的总池
  └── _leases      BudgetLease（每个 Run + WorkItem 一个）
        ├── reserved ── consume ──▶ consumed
        └── ──────── close ─────▶ returned（只还未用部分）
```

**根额度与租约的原子联动**——信用池与**每类资源**各有一条不变量，
两层缺一不可（只做信用池会让 Executor 绕过 `middleware.Budget` 的全局限制）：

```
信用:  available = max_lease_credits - leased_consumed
                             - Σ(active_lease.remaining_reserved)

资源:  available[r] = limit[r]              # r ∈ model_calls/tool_calls/steps/subagent_tasks
                    - root_usage[r]         # 根 Budget 上的实扣
                    - Σ(active_lease.remaining_reserved_for(r))
```

「已预留未消费」也算占用——它随时可能被花掉，不预留就等于透支。
**创建 Lease 只减 `available`，不增 `root_usage`**；真正调模型/工具时才增加后者。

`BudgetLease.consume()` **不是**只改自己那一行。它按规格六步走，
且是 **copy-on-write**——先算出「成功之后会是什么样」的候选快照、落库成功、
才回写内存：

```
lease._lock  持有（与 ledger 共用同一把 RLock）
  └─ ledger._lock  持有
       ├─ 1. 校验该资源剩余预留 consumed[r] + amount ≤ reserved[r]
       ├─ 2. 校验根额度 limit[r] 与通用信用池
       ├─ 3. 算候选状态快照（内存仍是一动未动）
       ├─ 4. _persist(候选快照)   ← 失败则整笔抛出，内存与数据库都停在原地
       └─ 5. 回写：根 Budget 逐次 consume + 信用 + 本租约计数
  （6. 返回后调用方才允许真正调模型 / 工具 / Subagent）
```

`amount > 1` 必须对 `Budget.consume_*()` 循环调用——那些方法一次只扣 1。
第 2 步已按 `usage + amount ≤ limit` 预检，循环中途不会抛错，因此
「库里已写、内存没改」的撕裂也不会出现。

因此「两个 Lease 各尝试消费 3、根额度 4」时，合计成功消费**必然 ≤ 4**：第二个要么
开不出预留，要么消费时撞 `available`。

锁序固定为 `lease → ledger`；`interrupt_all_leases()` 先在 ledger 锁内取快照、
释放后再逐个 close，否则会与 `consume()` 形成相反锁序而死锁。

**两套视图，刻意不混**：

| 使用者 | 拿到什么 | 扣在哪 |
|---|---|---|
| Planner / Reviewer / Synthesizer | `ledger.resources`（现有 `Budget`） | 根资源计数，不占信用额度 |
| Executor / Subagent | `lease.view()`（`LeasedBudgetView`） | 该租约 → 联动根信用 |

`LeasedBudgetView` 实现了现有 `Budget` 消费方用到的 `consume_*` 接口。
**一笔消耗只落一次**，但两个维度都要记：它走 `BudgetLease.consume()`，
由账本在同一次事务里既扣租约的该资源预留、又扣根 `Budget` 对应的计数。
绝不允许「只扣租约不扣根」——那正是 Executor 绕过 `middleware.Budget`
全局限制的路径，也正是「lease.model_calls() 涨了、Budget.model_calls() 没涨」的病灶。

**先持久化、再允许外部调用**：`bind_persistence()` 注册的钩子收到的是**候选快照**
（还不是内存里的对象），在回写之前同步调用；钩子抛错 → `consume` 抛错 →
调用方拿不到额度，模型/工具调用不会发生。此时**内存用量与数据库用量都还停在
扣减前**——不存在「内存改了但库没写」，也不存在「库写了但内存没改」。
钩子的签名决定了它**不能反过来读账本**（那会与账本锁形成反向锁序），
持久化侧的入口因此是 `save_ledger_state(run_id, state)`。

**原子消耗**：编排计数、并发槽、租约消费一律「锁内检查 + 扣减 + 抛
`BudgetExceededError`」，**禁止 `check()` 后 `record()` 的 TOCTOU**。
多线程抢同一额度时，成功次数**恰好等于上限**（有并发测试锁住）。

`BudgetExceededError` 继续用现有实现，**不参与 Retry**
（`RetryPolicy.retryable_types` 只含 `RuntimeError` / `ConnectionError`）——
重试一个已超限的操作只会再超一次。

租约三态满足恒等式 `reserved == consumed + returned`：

- 只能属于一个 run / work item
- 消费不得超过 `reserved`（超了抛 `BudgetExceededError`）
- `close()` **只归还未使用部分**，**consumed 永不归还**
- **重复关闭幂等**（第二次返回 0）
- 关闭后拒绝任何消费

本阶段只提供「关闭中断租约并归还未使用额度」的**基础操作**，
**不判断外部副作用是否已经发生**——那要结合审计事件（25F）再决定回滚还是重放。

`active_leases()` 是 25F 安全恢复的查询入口。`to_state()` / `from_state()`
让 usage / reserved / lease 状态在重启后**完全恢复**（预算不重置）。

## 序列化

沿用仓库既有风格（`subagent_models.py` / `context_state.py`）：

- 每个模型带 `schema_version`（当前 1），`from_dict` 校验未知版本
- `str` 枚举按 `.value` 落盘，`from_dict` 直接构造（未知值报错）
- `to_dict` 只写非默认字段，`from_dict` 缺必填字段报「缺少字段: x」
- 嵌套模型递归序列化（`GoalContract → GoalAmendment[]`、
  `LeadRunState → PlanningDecision[] + WorkItem[] + VerifiedFact[]`）

frozen 数据（`GoalContract`/`WorkItemProposal`/`EvidenceReceipt`/
`VerificationResult`/`VerifiedFact`/`ExecutionAttempt`/`ResourceClaim`/
`PlanningDecision`/`GoalAmendment`）任何字段赋值抛 `FrozenInstanceError`；
修订目标契约必须走 `amend()` 返回**新**对象。

`WorkItem` 与 `LeadRunState` 是可变 dataclass（要状态转换），但字段
在 `__post_init__` 严格校验，状态变更只走 `transition_to()`。

## 模块

```
src/mini_deerflow/lead_agent/
├── errors.py          25A + 25B 领域异常（状态机 / 批次 / 存储 / 租约）
├── state_machine.py   两个状态枚举 + ExecutionRoute/ResourceAccessMode 两个正交枚举
│                      + 两张转换表 + validate_transition / is_terminal
├── models.py          14 个模型 + 序列化（唯一依赖 state_machine）
├── validation.py      validate_batch + normalize_resource / conflicts / 父子重叠
├── events.py          LeadDomainEvent（25B：持久化审计事件，payload 安全校验）
├── store.py           LeadRunStore 协议 + InMemoryLeadRunStore + 分页上限（25B）
├── sqlite_store.py    SQLiteLeadRunStore（25B：事务 / PRAGMA / 级联 / 预算落库）
├── budget_ledger.py   LeadBudgetLedger + BudgetLease（25B：编排额度与租约）
└── __init__.py        纯导出，import 零副作用
```

依赖方向单向：`models → state_machine → errors`，
`validation → models`，`store → models + events + state_machine`，
`sqlite_store → store + budget_ledger`，`budget_ledger → middleware.Budget + errors`，
无循环。

## 导入无副作用

`import mini_deerflow.lead_agent` 只执行「定义类与函数」：

- 不创建线程、数据库连接、Runtime 或网络客户端
- 不 import `httpx`/`requests`/`aiohttp`/`asyncio`
- 不写任何文件、不改环境变量

25B 起 `sqlite_store` 会 `import sqlite3`（标准库），但**导入期不建立任何连接、
不创建任何文件**——`SQLiteLeadRunStore.__init__` 才连库，`CREATE TABLE IF NOT EXISTS`
保证初始化幂等。

由 `tests/test_lead_agent_import.py` 用**子进程冷启动**验证
（不能用 `importlib.reload`——它会重建类对象、污染同会话其它测试）。

## 与既有代码的复用关系

| 复用 | 位置 |
|---|---|
| 状态机形状：`status → frozenset[可到达状态]` + 专用异常 | 沿用 `subagent_models._VALID_TRANSITIONS` |
| 序列化：`schema_version` + `req()` + `str` 枚举 | 沿用 `subagent_models` / `context_state` |
| 领域异常分层 | 沿用 `context_errors.py` 风格 |
| `str, Enum` 枚举 | 与 `TaskNoteCategory` 等一致 |
| **预算**：`LeadBudgetLedger.resources` 持有**现有的** `middleware.Budget` 实例 | 与 `SubagentManager(shared_budget=...)` 同一套复用；**不新建第三套 Budget** |
| **SQLite 习惯**：`connect(check_same_thread=False)` + `RLock` + `CREATE TABLE IF NOT EXISTS` + `with conn:` 事务 | 沿用 `SQLiteThreadStore`，额外补 PRAGMA（见下） |

### 对现有代码的唯一改动

`middleware.Budget` 新增一个方法：

```python
def restore_usage(self, *, steps=0, model_calls=0, tool_calls=0,
                  subagent_tasks=0, summary_calls=0, max_depth_seen=0) -> None
```

理由：25B 要求「重启后 usage 完全恢复」，而现有 `Budget` 只有 `consume_*`
（只能递增）与 `check_*/record_*`（**已弃用的 TOCTOU 模式**），没有写入已知用量的入口。
`restore_usage` 是**一次性写入**、不判上限、不可与 `consume_*` 混淆：
运行期消耗走 `consume_*`（原子、超限抛错），启动恢复走 `restore_usage`。

`Agent`、`Subagent`、`ThreadStore`、`SQLiteThreadStore` 的行为**完全未改**。

## 后续阶段接入点

- **Planner**：吃 `GoalContract` + `LeadRunState.verified_facts`
  （唯一合法事实来源）→ 产出 `PlanningDecision`
- **Router**：拿 `WorkItem.claims` 调 `validate_parallel_safe` 决定并行组
- **Executor**：把 `WorkItem` 变成一次 `ExecutionAttempt`；
  开跑前用 `ledger.open_lease()` 预留额度，调用前 `lease.consume()` 与
  `resources.consume_model_call()` / `consume_tool_call()`
- **Reviewer**：产出 `EvidenceReceipt` + `VerificationResult`
- **Synthesizer**：只消费 `VerifiedFact`
- **25F（恢复）**：`list_recoverable()` 找出待恢复的 Run，
  `active_leases()` 找出仍占额度的租约，
  `interrupt_all_leases()` 归还未用额度——**副作用是否已发生由 25F 判定**
- **25G（SSE）**：`LeadDomainEvent` 是内部审计事件，
  对外 Schema 不与其耦合
- **API/SSE** 仍为后续阶段；`LeadRunState.to_dict/from_dict` 已为序列化预留
