# Phase 25 进度

## 状态图例

```
⬜ 未开始    🟡 进行中    ✅ 已完成    ⛔ 阻塞
```

## 总体阶段

| 阶段 | 内容 | 状态 |
|------|------|------|
| 25A | Lead Agent 领域模型、状态机与滚动批次校验 | ✅ 定向 498 passed（[lead-agent-architecture.md](lead-agent-architecture.md)） |
| 25B | LeadRunStore、SQLite、CAS 与持久化预算账本 | ✅ 定向 713 passed / 全量 3003 passed + 42 subtests |
| 25C | LeadPlanner 内部服务（下一步规划） + 25C 架构收口 | ✅ 定向 760 passed / 全量 3050 passed + 42 subtests |
| 25D | LeadRouter 策略建议与执行权限分离 + 25D 契约收口 | ✅ 定向 816 passed / 全量 3106 passed + 42 subtests |
| 25E | Executor + Reviewer | ⬜ 未开始 |
| 25F | Synthesizer + Recovery | ⬜ 未开始 |
| 25G | API + SSE | ⬜ 未开始 |
| 25H | 前端 + E2E | ⬜ 未开始 |
| — | **Phase 25 总体** | 🟡 进行中 |

---

## 25A 验收清单

架构见 [lead-agent-architecture.md](lead-agent-architecture.md)。

### 1. 包结构（规格要求的五个文件）

- [x] `src/mini_deerflow/lead_agent/models.py`
- [x] `src/mini_deerflow/lead_agent/state_machine.py`
- [x] `src/mini_deerflow/lead_agent/validation.py`
- [x] `src/mini_deerflow/lead_agent/errors.py`
- [x] `src/mini_deerflow/lead_agent/__init__.py`（纯导出 37 个公共名，零副作用）

### 2. 规格点名的 13 个模型

- [x] `GoalContract`（构造期拦空 objective / acceptance_criteria，`amend()` 返回新对象）
- [x] `GoalAmendment`（不可变修订记录）
- [x] `LeadRunStatus`（12 状态 / 5 终态：CREATED…SYNTHESIZING）
- [x] `WorkItemStatus`（10 状态 / 5 终态）
- [x] `ExecutionRoute`（**执行路线**：`direct_agent` / `tool` / `subagent` / `subagent_batch`）
- [x] `ResourceAccessMode`（**读写方式**：`read` / `write` / `unknown`）
      —— 与 `ExecutionRoute` 正交，两组值零交集、互不留别名
- [x] `PlanningDecision`（一次滚动批次决策，非完整 DAG）
- [x] `WorkItemProposal`（待校验提案，带 `execution_route`）
- [x] `WorkItem`（可变，状态转换 + 修订上限 + 继承 `execution_route`）
- [x] `ExecutionAttempt`（`sequence` 1=首次、2=修订）
- [x] `EvidenceReceipt`（证据，非事实）
- [x] `VerificationResult`（`passed=True` 必须带证据）
- [x] `VerifiedFact`（唯一可进入后续规划的事实）
- [x] `LeadRunState`（Run 聚合，**无 commit/submit 方法**）

额外：`ResourceClaim`（资源访问声明对，冲突判定所需）。

### 3. 严格状态转换

- [x] 两张转换表（`status → frozenset[可到达状态]`），终态映射空集
- [x] 非法转换抛 `InvalidTransitionError`
- [x] **终态单独抛 `TerminalStateError`**（`InvalidTransitionError` 子类，调用方可区分）
- [x] 穷举全部 `current × target` 组合与独立编写的期望表比对
      （**144 个 LeadRun 组合** + 100 个 WorkItem 组合）
- [x] `WAITING_FOR_INPUT` 可恢复：**只从 PLANNING/VERIFYING 进入、只回到 PLANNING**
- [x] **`EXECUTING` 不接受 `WAITING_FOR_INPUT`**（澄清不打断执行中任务）
- [x] `WAITING → EXECUTING` 非法（澄清回规划，不直接续跑）
- [x] `PARTIAL` 为终态：可达、不可逆、自身转换也被拒
- [x] `PARTIAL` **只能经 `SYNTHESIZING` 抵达**（执行/验证阶段不能直接宣告）
- [x] `CREATED → PLANNING` 是唯一前进方向，`CREATED` 不允许 `TIMED_OUT`
- [x] 滚动批次主干 `EXECUTING → VERIFYING → SYNTHESIZING → 终态`；
      `EXECUTING → PLANNING` **已删除**（批次推进必须先经验证）
- [x] 类型不匹配 / 未知状态类型抛 `TypeError`

### 4. 序列化往返

- [x] 9 个小模型参数化 roundtrip（`to_dict → from_dict → == to_dict`）
- [x] `WorkItem` 含 attempts / revised 的完整 roundtrip
- [x] `LeadRunState` 全量 roundtrip（含 decisions、items、facts、goal amendments）
- [x] 拒绝未知 `schema_version`、缺失必填字段、未知枚举值、非 dict 输入

### 5. frozen 不可意外修改

- [x] 9 个 frozen 模型逐一断言赋值抛 `FrozenInstanceError`
- [x] 目标契约只能经 `amend()` 派生新对象（原对象逐字段不变）
- [x] 嵌套 frozen（`VerifiedFact.receipts`）同样不可变

### 6. 滚动批次校验

- [x] 合法批次产出 `WorkItem(READY)`
- [x] 依赖可指向**已验证历史**（滚动批次核心语义）
- [x] 依赖可指向**本批内后声明**的条目（按集合校验，不看顺序）
- [x] 空批次合法
- [x] objective / acceptance criteria 为空 → `EmptyGoalFieldError`
      （第一道防线在 `GoalContract` 构造期，批次层为防御性复核）
- [x] 批次内 work_id 重复 → `DuplicateWorkIdError`
- [x] 与已验证历史重名 → `DuplicateWorkIdError`
- [x] 自依赖 → `SelfDependencyError`
- [x] 缺失依赖 → `MissingDependencyError`（携带缺失 id 元组）
- [x] 直接环、三角环 → `CyclicDependencyError`（环路径首尾闭合）
- [x] 菱形依赖**不是**环，放行
- [x] 100 节点大环可检出（迭代式 DFS，不递归）

### 7. 资源规范化与冲突

- [x] 规范化：去空白、统一 `\` `/`、折叠重复分隔符、去尾部斜杠、幂等
- [x] 资源分两类：**路径型**（`file:`/`workspace:`/`path:`、含分隔符、`name.ext`）
      与**逻辑型**（`cache`、`db:Orders`），语义不同
- [x] 路径型：折叠 `.` 段、**拒绝未解析的 `..`**、**`casefold()` 折叠大小写**
      （Windows 大小写不敏感，不折叠会把同文件判为两个而允许并行 = 漏冲突）
- [x] 逻辑型：**保持大小写、精确匹配**，不套用文件路径规则
- [x] 空/非字符串资源 → `UnknownResourceError`
- [x] **读–读允许并行**（`conflicts` → False）
- [x] **写–写冲突**（True）
- [x] **读–写冲突**（两个方向都测）
- [x] **unknown 强制冲突**（对 read/write/unknown 三向全测）
- [x] **父子路径视为重叠**：`file:src` ⊃ `file:src/agent.py`
      - [x] 父目录写 + 子文件读 → 冲突
      - [x] 父目录读 + 子文件写 → 冲突
      - [x] 父目录读 + 子文件读 → 不冲突
      - [x] `A.txt` 写 + `a.txt` 读 → 冲突
      - [x] 兄弟文件 `src/a.py` vs `src/b.py` 不重叠
      - [x] 逻辑资源无父目录语义（`db` 与 `db:orders` 不同资源）
- [x] `check_resource_conflicts` 只对同资源（含父子重叠）配对
- [x] `validate_parallel_safe` **两两比较**，非相邻冲突也测出
- [x] 单组、互斥资源组放行

### 8. 导入无副作用

- [x] 子进程冷启动：import 不创建线程
- [x] 子进程冷启动：import 不创建文件（含构造运行态）
- [x] 子进程冷启动：不加载 `httpx/requests/aiohttp/urllib3`
- [x] 模块层无 `Client/Connection/Executor/Session/RuntimeContext` 等运行期对象
- [x] `__all__` 显式且全部存在
- [x] 模型操作不启动后台线程

### 9. 文档

- [x] `docs/lead-agent-architecture.md`（232 行）
- [x] `docs/phase-25-progress.md`（本文件）
- [x] `docs/PROGRESS.md` 总表新增 25A 行

---

## 25B 验收清单

架构见 [lead-agent-architecture.md](lead-agent-architecture.md) 的「持久化」与
「预算账本」两节；与官方 DeerFlow（`bytedance/deer-flow` `main`）的对照审计
见 [DEERFLOW_PARITY.md](DEERFLOW_PARITY.md)（含 25C 前的设计建议）。

### 1. Store 接口与契约

- [x] `LeadRunStore` Protocol：`create` / `get` / `save` / `list_by_thread` /
      `list_recoverable` / `list_events` / `delete`
- [x] **InMemory 与 SQLite 共用同一份契约测试**（同一套用例参数化跑两种实现）
- [x] 创建重复 run_id → `LeadRunAlreadyExistsError`，且不覆盖原有数据
- [x] 不存在 → `LeadRunNotFoundError`
- [x] `list_recoverable()` 只返回**非终态** Run（五个终态逐个验证被排除）
- [x] `limit` / `offset` 有硬上限：0、负数、超 `MAX_LIST_LIMIT`、非整数、
      布尔值全部拒绝；游标越界返回空
- [x] 关闭后的 Store 拒绝全部操作，且 `close()` 幂等

### 2. 序列化与恢复

- [x] 含 items / facts / decisions / batch_index 的复杂状态往返不丢字段
- [x] **SQLite 重启后**状态、事件、revision 全部恢复
- [x] Schema 初始化幂等（同一文件反复打开三次，数据不丢）
- [x] 损坏 `state_json` → `LeadStoreDataCorruptedError`，不当空状态
- [x] 未知 `schema_version` → 明确失败，不静默按当前版本读
- [x] 测试一律用临时**文件**库（不依赖 `:memory:` 多连接共享语义）
- [x] **`thread_id` 必填非空**：构造缺失即 `TypeError`，空串被 `__post_init__` /
      `from_dict` 拒绝；Store 入口第二道防御；数据库 `NOT NULL` + `CHECK`

### 3. CAS 与原子提交

- [x] 正确 revision 保存成功并递增（0 → 1）
- [x] 陈旧 revision → `LeadRunConflictError`，且**已提交状态不被改变**
- [x] **两线程并发 CAS 同一 revision，只有一个成功**（`results == [1]`）
- [x] 非法事件 payload（凭据键）→ 整个 `save()` 回滚，状态与事件都不推进
- [x] 事件 `run_id` 与状态不一致 → **领域异常** `EventRunMismatchError`
      （不是 `ValueError`），且在写事务之前拦下：快照不更新、revision 不增、事件不写
- [x] 事件 sequence **由 Store 分配**：调用方传的值被覆盖
- [x] 事件 sequence 单调且不重复（`[0,1,2,3,4,5]`）
- [x] 事件游标分页 `after_sequence` 正确
- [x] 多线程混合读写不抛异常、不产生损坏数据
- [x] **跨连接 CAS**：两个**独立** `SQLiteLeadRunStore` 指向同一文件、
      相同 `expected_revision` 并发保存 → 严格一个成功
- [x] 失败方**不泄漏** `IntegrityError` / `OperationalError` / `database is locked`，
      而是按「能否重读重试」分流（见下「收口轮」第 3 项）
- [x] **异常映射收窄**：只有 `BUSY/LOCKED`、CAS revision 不匹配、并发
      `sequence` 的 UNIQUE 才是 `LeadRunConflictError`；表结构错 / CHECK 错 /
      I-O 错 / 其它约束 → `LeadStoreWriteError`（**不谎报成可重试的冲突**）
- [x] `BEGIN IMMEDIATE` 失败时事务未开始，**不执行 ROLLBACK**（`transaction_started` 标志）
- [x] 跨连接并发后 sequence 不重复、失败事务**不残留事件行**

### 4. SQLite 配置

- [x] `PRAGMA foreign_keys = ON` **实测为 1**（不是文档承诺）
- [x] 写锁等待（`busy_timeout`）
- [x] WAL 启用失败时静默回退（内存库场景）
- [x] 级联删除：删 Run 后事件行、租约行、预算记录全部清理
- [x] **写事务用显式 `BEGIN IMMEDIATE`**（`isolation_level=None` + 手动事务边界），
      消除 deferred 在「读 revision → 写」之间的竞争窗口

### 5. 预算账本

- [x] 默认编排限制 4 / 12 / 4 / 2 / 1 / 600（**测试独立写死**，
      不 import 生产常量做对照）
- [x] `resources` 持有**现有的** `middleware.Budget` 实例（不新建第三套）
- [x] 批次 / 工作项（总量 + 单批双限）/ 并发槽：先扣减后校验
- [x] **多线程抢批次额度，成功次数恰好等于上限**（4）
- [x] **多线程抢工作项额度，总量不超 12**
- [x] `BudgetExceededError` 继续用现有实现，**不参与 Retry**
- [x] 并发槽未配对释放 → 报错，不静默兜底
- [x] 墙钟计时幂等（`begin_wall_time` 不重置起点）
- [x] **根额度不变量**：`available = max_lease_credits - leased_consumed
      - Σ(活动租约 remaining_reserved)`，逐项验证（含预留未消费仍占额度）
- [x] **`open_lease` 根额度不足即失败**（两个线程不能都拿到名义预留）
- [x] **「根 4、两租约各消费 3」合计成功消费 ≤ 4**
- [x] **8 线程并发抢同一根额度，成功数恰好等于上限**
- [x] `LeasedBudgetView` 的 `consume_*` **同一笔只落一次**，但在同一次事务里
      既扣租约的资源预留、又扣根 `resources` 对应计数——不允许只扣一边
- [x] **资源级租约联动**：`available[r] = limit[r] - root_usage[r]
      - Σ(活动租约 remaining_reserved[r])`，四类资源各自独立
- [x] **两个 Lease 共同消费 `model_calls` 不超根 `max_model_calls`**
- [x] Lease 消费会同步增加根 `Budget.model_calls()`（实测断言，非文档承诺）
- [x] **Direct 消费不抢占活动 Lease 已预留的额度**（`usage + outstanding + amount ≤ limit`）
- [x] **不同资源分别计数**：model 的预留不占 `tool_calls` 的可用量
- [x] `bind_persistence()`：**copy-on-write**——先算候选快照、落库成功、
      才回写内存；钩子抛错时内存与 SQLite 的用量**都不变**，外部模型调用不发生

### 6. Lease

- [x] 预留 / 消费 / 归还 / 幂等关闭
- [x] **Lease 内消费不得超过 reserved**（超了抛错且不扣减）
- [x] `close()` 只归还未使用部分，**consumed 永不归还**
- [x] 重复 `close()` 幂等（返回 0）
- [x] 已关闭 Lease 拒绝消费
- [x] 归属绑定单个 run / work item，序列化后不变
- [x] **并发消费同一租约不超 reserved**
- [x] `active_leases()` 按 run / work item 过滤（25F 恢复入口）
- [x] 损坏租约数据（consumed > reserved / returned ≠ 恒等式）明确失败
- [x] `interrupt_lease` / `interrupt_all_leases` 归还未用额度

### 7. 重启恢复

- [x] `to_state()` / `from_state()` 往返：编排计数、并发槽、墙钟、
      租约（活动与已关闭）、现有资源 Budget 用量全部恢复
- [x] **重启后预算不重置**：恢复出的账本仍会撞上限
- [x] 复用调用方给定的 Budget 实例（不新建）
- [x] SQLite 账本重启后恢复，租约的 consumed/reserved 不丢
- [x] 租约表整表重写，被移除的租约不留残行

### 8. import 无副作用

- [x] 子进程冷启动导入 `sqlite_store` / `store` / `budget_ledger` / `events`
      **不创建任何文件**（不建库）
- [x] 导入并构造默认账本**不启线程**

---

## 门禁结果

| 命令 | 结果 |
|------|------|
| `uv run pytest tests/test_lead_agent_*.py -q`（定向） | **498 passed in 0.92s** |
| `uv run pytest -q`（全量） | **2788 passed + 42 subtests in 98.22s** |
| `uv pip check` | `Checked 48 packages` / `All installed packages are compatible` |
| `uv run python -m compileall -q src/mini_deerflow/lead_agent` | exit 0 |
| `git diff --check` | **exit 0，0 处空白错误** |

**测试闭合**：2290（Phase 24 基线）+ 498（本阶段新增）= 2788 ✅，零回归。

`git diff --stat` 仅显示 `.obsidian/workspace.json`（历史遗留行尾变更，
**本阶段未触碰**）；本阶段 6 项均为新增未跟踪文件，`git status --short` 见下
（未 commit、未 push，按要求）。

**全部门禁通过，Phase 25A ✅。**

### 25B 门禁：**全部通过** ✅

（此前执行器故障期间该节记为 ⛔「尚未验证」，现已补跑并回填实测数字。）

| 命令 | 结果 |
|------|------|
| `uv run pytest tests/test_lead_agent_*.py -q`（定向） | **713 passed in 2.45s** |
| `uv run pytest -q`（全量） | **3003 passed + 42 subtests in 100.16s** |
| `uv pip check` | `Checked 48 packages` / `All installed packages are compatible` |
| `uv run python -m compileall -q src/mini_deerflow/lead_agent` | exit 0 |
| `git diff --check` | **exit 0**（仅 2 条 CRLF 提示，0 处空白错误） |
| `git diff --stat` | 3 个已跟踪文件改动：`.obsidian/workspace.json`（历史遗留，**本阶段未触碰**）、`docs/PROGRESS.md`、`src/mini_deerflow/middleware.py`（`restore_usage`） |
| `git status --short` | 3 个已修改 + 8 个未跟踪（`lead_agent/` 目录与 6 个测试/文档），**未 commit、未 push** |

**测试闭合**：2788（25A 基线）+ 215（25B 新增）= 3003 ✅，零回归。
**Phase 25B ✅**，不进入 25C（等用户排期）。

### 静态复核与评审修复

| # | 问题 | 处理 |
|---|------|------|
| 1 | 预算测试调用了 `RetryPolicy.is_retryable()`，而该方法**不存在**（中间件内部直接用 `isinstance(exc, policy.retryable_types)`），会 `AttributeError` | 改为与中间件一致的 `isinstance` 判断，并补一条 `RuntimeError` 可重试的反向对照，证明该判断有意义 |
| 2 | `BudgetLease.consume()` 只做 `self.consumed += amount`，**不联动根预算**——无法证明根额度不超卖，两个租约可各自把共享池打穿 | 引入 `max_lease_credits` 根额度与 `leased_consumed`，`consume()` 在 `lease→ledger` 锁序内先扣根账本；`available = limit - leased_consumed - Σ(活动租约 remaining_reserved)`；新增 `LeasedBudgetView`（只落租约，杜绝双扣）与 `bind_persistence()`（扣减后先落库，失败即阻断外部调用） |
| 3 | 默认 deferred 事务在「读 revision → 写」之间有竞争窗口，并发写抛 `OperationalError: database is locked` 直接泄漏 | 改用 `isolation_level=None` + 显式 `BEGIN IMMEDIATE/COMMIT/ROLLBACK`；`IntegrityError`/`OperationalError` 统一映射为 `LeadRunConflictError`；新增**两个独立连接**的跨连接 CAS 测试（同一实例的 `RLock` 测不到跨连接竞争） |
| 4 | `thread_id` 是带默认值 `""` 的可选字段，空值会让 `list_by_thread()` 静默失效 | 改为**必填非空**（含 `__post_init__` / `from_dict` 校验）、Store 入口第二道防御、数据库 `NOT NULL` + `CHECK(length(trim(thread_id)) > 0)`；同步更新 25A/25B 全部测试 |
| 5 | `restore_usage()` 可以对已消费的 Budget 反复调用并把计数改小/清零；`bool` 会被当整数写入 | 加一次性闸 `_usage_restored` + 「必须全新未消费」检查；显式拒绝 `bool`/非整数/负数；未知关键字由 Python 自身抛 `TypeError`；恢复值允许超限（历史事实），由下一次 `consume_*` 暴露 |
| 6 | 事件 payload 的敏感键只查顶层 | 改为**递归**检查嵌套 dict/list，并验证可 JSON 序列化；文档明确「键过滤不是完整秘密扫描，调用方仍只允许提交安全结构化元数据」 |

> **注意**：上表第 2、3 行描述的是 25B **首轮**的结论，已被「25B 架构收口」
> 第 1、3 项推翻并修正（`LeasedBudgetView` 现在会联动根资源；
> sqlite 错误现在分流为两类异常）。此处保留是为了记录演进过程。

另清理：`InMemoryLeadRunStore.create` 里一处多余的 `replace(..., sequence=0)`；
`list_recoverable` 的 docstring 与实现不符（改用 `status` 冗余列 + 索引做真 SQL 过滤）；
测试文件里的未使用 import 与重复 `close()`。

### 本阶段新增文件规模

| 文件 | 行数 |
|---|---:|
| `lead_agent/models.py` | 1024 |
| `lead_agent/validation.py` | 343 |
| `lead_agent/state_machine.py` | 329 |
| `lead_agent/errors.py` | 132 |
| `lead_agent/__init__.py` | 102 |
| **源码小计** | **1930** |
| `tests/test_lead_agent_validation.py` | 465 |
| `tests/test_lead_agent_models.py` | 839 |
| `tests/test_lead_agent_state_machine.py` | 436 |
| `tests/test_lead_agent_import.py` | 224 |
| **测试小计** | **1964** |
| `docs/lead-agent-architecture.md` | 290 |

### 25B 新增文件规模

| 文件 | 行数 |
|---|---:|
| `lead_agent/store.py` | 306 |
| `lead_agent/sqlite_store.py` | 722 |
| `lead_agent/budget_ledger.py` | 1037 |
| `lead_agent/events.py` | 209 |
| **源码小计** | **2274** |
| `tests/test_lead_agent_budget.py` | 1144 |
| `tests/test_lead_agent_store.py` | 1134 |
| **测试小计** | **2278** |
| 25A 文件因 25B 变更 | `errors.py` 132→239、`__init__.py` 102→163、`models.py` 1024→1037、`middleware.py` +73（`restore_usage`）、`test_lead_agent_import.py` 224→275 |

---

## 实施中发现并修正的问题

| # | 问题 | 处理 |
|---|------|------|
| 1 | `validation.py` 写出 `for claim in left_ := claims_left:`（非法语法，包无法导入） | 删除，保留真正的工作循环 |
| 2 | `models.py` 空 `if: pass` 分支 | 删除，为紧邻的有效逻辑补注释 |
| 3 | `validate_batch` 内 3 处局部 `import ... as _E` 冗余 | `EmptyGoalFieldError` 提升到顶部 import |
| 4 | `objective` 构造期抛裸 `ValueError`，而 `acceptance_criteria` 抛 `EmptyGoalFieldError`（不一致） | 统一为 `EmptyGoalFieldError`（规格点名这两个字段） |
| 5 | 测试用 `importlib.reload` 验证 import 副作用，**污染同会话其它测试**（重建类身份 → 旧引用传进新函数 → 341 个用例 `TypeError`） | 改为**子进程冷启动**验证，天然隔离且更贴近真实首次导入 |
| 6 | 子进程脚本两处 `NameError`（`threading`/`LeadRunStatus` 未 import） | 补 import |

## 架构收口（收尾轮）

收尾轮对首轮实现做了 4 项修正，均为**语义正确性**问题而非补测试凑数：

| # | 问题 | 修正 |
|---|------|------|
| 1 | `ExecutionRoute` 一个名字承载两种语义（既当执行路线、又当读写方式） | 拆为 `ExecutionRoute`（`direct_agent`/`tool`/`subagent`/`subagent_batch`）与 `ResourceAccessMode`（`read`/`write`/`unknown`），`ResourceClaim` 用后者，`WorkItemProposal`/`WorkItem` 各带 `execution_route`；**不保留任何兼容别名** |
| 2 | `LeadRunStatus` 只有 9 个状态，缺 ROUTING/VERIFYING/SYNTHESIZING 等阶段；且存在 `EXECUTING → WAITING_FOR_INPUT`（执行中挂起等澄清）与 `EXECUTING → PLANNING`（执行中跳回规划）两条不合理边 | 扩到 **12 状态**；澄清只允许 `PLANNING`/`VERIFYING` 进入、只回到 `PLANNING`；批次推进改为 `EXECUTING → VERIFYING → SYNTHESIZING → 终态`；`PARTIAL` 只能经 `SYNTHESIZING` 抵达 |
| 3 | 报告称「`A.txt`/`a.txt` 不折叠会多串行、不漏冲突」——**结论说反了**：不折叠会判为两个资源而**允许并行**，Windows 下可能是同一文件 → 漏掉真实冲突 | 路径型资源改 `casefold()`；非路径型（逻辑标识符）保持精确匹配；另补拒绝 `..`、折叠 `.` 段、保留 scheme 与根 `/` |
| 4 | 完全没有父子路径重叠判定 | 新增 `_paths_overlap`：`file:src` 与 `file:src/agent.py` 视为重叠，只要非读读即冲突；逻辑资源无父目录语义 |

配套修正：`LeadRunStatus.PENDING` 更名为 `CREATED`（与新状态机一致）；
`normalize_resource("/")` 的根路径保留条件写反导致误报「为空」，已修。

## 25B 架构收口（修复项 1–4）

首轮 25B 实现评审出 4 个架构问题，均已修并各配了能红的测试：

| # | 问题 | 修正 |
|---|------|------|
| 1 | `LeasedBudgetView` **只落租约、根 `Budget` 零扣减**——Executor/Subagent 因此绕过 `middleware.Budget` 的全局限制；租约也只按通用信用记账，看不出钱花在哪类调用上 | 按 `model_calls`/`tool_calls`/`steps`/`subagent_tasks` 四类维护租约预留；`available[r] = limit[r] - root_usage[r] - Σ(活动租约 remaining_reserved[r])`；创建 Lease 只减 available 不增 root_usage；消费按规格六步在 `lease→ledger` 锁序内完成；关闭只释放 `remaining_reserved`，`consumed` 永不归还；`max_lease_credits` 降为**额外**限制，不替代各类 `max_*` |
| 2 | 「先改内存 → 调 hook → hook 失败但内存已改」：落库失败时根用量与租约计数已被占住，只能靠下次落库对齐 | 改为 **copy-on-write**：锁内预检 → 算候选状态快照 → `persist(候选)` → 成功才回写内存。钩子签名从 `persist(ledger)` 改为 `persist(state)`（拿到的是快照而非账本对象，从类型上杜绝反向读账本）；持久化侧入口新增 `save_ledger_state(run_id, state)`，`save_ledger` 先在**取库锁之前**算好快照，消除「库锁 → 账本锁」的反向锁序 |
| 3 | `_as_conflict` 把**所有** `IntegrityError` / `OperationalError` 无差别映射成 `LeadRunConflictError`；`BEGIN IMMEDIATE` 失败也可能去 ROLLBACK 一个没开始的事务 | 按「能否重读重试」分流：`BUSY/LOCKED`、CAS revision 不匹配、并发 `sequence` 的 UNIQUE → `LeadRunConflictError`；其余 → 新增 `LeadStoreWriteError`。`create()` 的 `IntegrityError` 同样收窄（只有主键唯一才等于「已存在」）。`_write_transaction` 加 `transaction_started` 标志，未开事务绝不 ROLLBACK；`COMMIT` 失败也先收尾再抛 |
| 4 | `event.run_id` 不符抛裸 `ValueError`；敏感键只做 `.lower()` 比对，`API-KEY` 会从缝里过去 | 新增领域异常 `EventRunMismatchError`，校验仍在写事务之前（快照/revision/事件整笔回滚）；键名改为**规范化后比对**（小写 + 去连字符/下划线/空白），`api_key` / `API-KEY` / `ApiKey` 是同一个键；仍只比对整个键名，`prompt_len` 不受影响；文档保留「这不是完整秘密扫描」的声明 |

**配套测试**（每条都能红）：两个 Lease 共同消费 `model_calls` 不超根 limit；
Lease 消费后根 `Budget.model_calls()` 实测递增；Direct 不抢活动 Lease 预留；
四类资源分别计数互不占用；hook 抛异常时**内存 usage / SQLite usage /
Lease remaining 三者都不变**且外部 callable 未执行；两个独立 SQLite 连接
CAS 严格一个成功；非并发 SQLite 错误不误报 Conflict（参数化 11 条分类用例）；
`event.run_id` 不匹配整笔回滚（revision / 快照 / 事件三项断言）；
`restore_usage` 二次调用、部分已有 usage、`bool`、负数、未知字段、
**8 线程并发**全部拒绝（恰好一个成功）。

另修一个真实缺陷：`_charge_resource` / `consume_direct` 调用根 Budget 的
`consume_*()` 时**没传 amount**，而那些方法一次只扣 1——`consume("model_calls", 3)`
实际只扣 1，重启恢复出来的用量因此对不上。改为按 `amount` 循环调用
（预检已保证循环中途不会因额度抛错）。

## 真实限制

1. **纯领域层，无执行能力**：本阶段不调用模型、不调度、不落库。
   `LeadRunState` 没有 `commit()`——ThreadState 何时提交由后续集成阶段决定。
2. **`validate_batch` 的目标非空复核不可达**（防御性）：`GoalContract`
   构造期已拦下空目标，无法构造非法对象喂进批次层。保留是作为第二道防线。
3. **循环依赖检测只覆盖本批内部**：依赖已验证历史的部分不可能成环
   （历史节点不再被本批依赖指向回），因此不参与环检测。
4. **资源冲突是声明式的**：判定基于 `WorkItem.claims` 里**声称**的访问方式，
   不会验证执行时是否真的只按声明访问——真正的约束需要 Executor 阶段落实。
5. **路径型资源 `casefold()`、逻辑型保持精确**：两者的区分靠
   `is_path_resource` 的启发式（scheme 前缀 / 含分隔符 / `name.ext`），
   极少数形态可能被归错类——归成逻辑型只是**可能漏掉 Windows 下的
   大小写冲突**，因此判定刻意偏向路径型（宁可多串行）。
   父子重叠只对路径型生效：`file:src` 覆盖 `file:src/agent.py`，
   而 `db` 与 `db:orders` 视为两个独立资源。
6. **大小写折叠的代价**：Linux 等大小写敏感文件系统上，
   `A.txt` 与 `a.txt` 是两个真实文件，折叠会让它们**多一次不必要串行**。
   这是刻意接受的代价——方向只能是「多串行」，不能是「漏冲突」。
7. **25A 阶段未做**（该阶段规格排除；25B 已补齐 SQLite / CAS / 预算账本）：
   Planner 模型调用、Router 实际调度、Executor/Reviewer/Synthesizer、
   FastAPI/SSE、前端、真实模型调用；未修改任何现有 Agent 行为。
8. **25B 仍未做**（规格明确排除）：Planner 模型调用、Router、Executor、
   Reviewer、Synthesizer、FastAPI/SSE、前端、**自动恢复执行**、
   `ThreadState` 最终提交、真实模型调用；不进入 25C。
9. **`InMemoryLeadRunStore` 没有账本方法**（`save_ledger` / `get_ledger` /
   `load_leases`）：账本持久化是 SQLite 独有的实现细节，`LeadRunStore`
   Protocol 本身不含它们，因此两者不共享这一段契约测试。
   生产路径只用 SQLite。
10. **账本的持久化钩子目前没有生产绑定**：`bind_persistence()` 由集成层
    （25C 之后）负责传入 `store.save_ledger_state(run_id, state)`；
    25B 阶段只有测试绑定它，不绑定时消费即为纯内存操作。
11. **`_as_conflict` 靠错误消息文本分类**：SQLite 的错误字符串不是稳定 API，
    不同版本措辞可能变。刻意取宽的并发标记（`locked`/`busy`）以免漏判成冲突，
    代价是极少数非并发的 locked 变体可能被误判为可重试——方向仍偏向
    「多一次无害重试」而非「漏掉真冲突」。
12. **租约与账本共用同一把 `RLock`**：消除 `consume`（lease→ledger）与
    `available_for`（ledger→lease）的反向锁序，代价是所有租约在同一账本上串行。
    这个量级下串行的代价可忽略，而反向锁序是真死锁。

## 下一阶段

25D+（Orchestrator / Executor / Subagent 接入 / LeadLease / Synthesizer）
尚未排期，等待用户选择。

---

## 25C 验收清单

LeadPlanner 是 Lead Agent 内部的**下一步规划服务**——只输出决策，不执行、
不调度、不开 Lease、不写 Store。

### 1. 边界

- [x] Planner 是 Protocol + `DefaultLeadPlanner` 实现；**不**是 Agent
- [x] Planner 不循环、不调度、不开 Lease、不写 Store、不改 LeadRunState
- [x] Planner 不引入 LangGraph / ToolCall / `ask_clarification` 工具
- [x] Planner 复用 25A 的 `WorkItemProposal` / `ResourceClaim` /
      `ExecutionRoute`；**不**新增近似类型
- [x] Planner 复用 25A 的 `validate_batch` + `validate_parallel_safe`；
      **不**另写冲突规则
- [x] Planner 通过 AST 守卫：未 import 25D 才有的 `orchestrator` /
      `subagent_executor` / `subagent_tools` 模块

### 2. 输入 `PlannerContext`（frozen）

- [x] 必含 `goal: GoalContract` + `verified_facts: tuple[VerifiedFact, ...]`
- [x] 必含 `last_decision: PlanningDecision | None`
- [x] 必含 `recent_items: tuple[WorkItemSummary, ...]`（最近 5 条摘要）
- [x] 必含 `budget_snapshot: BudgetSnapshot`（**只读值**，不持 Budget 引用）
- [x] 必含 `capabilities: tuple[str, ...]` + `last_error: LeadPlannerDeterministicError | None`
- [x] **不**含 `LeadRunState` / `Store` / `Budget` 引用

### 3. 输出 `PlannerOutput`（3 选 1 标签联合）

- [x] `ProposeBatch`（`proposals` + `rationale`）—— 跑 25A `validate_batch`
- [x] `RequestClarification`（`question` + `reason` + `blocking_facts`）——
      `blocking_facts` ⊆ `verified_facts.work_id`
- [x] `RecommendSynthesis`（`target_criterion` + `supporting_fact_ids` +
      `rationale`）—— `target_criterion` ∈ `goal.acceptance_criteria`；
      `supporting_fact_ids` ⊆ `verified_facts.fact_id`
- [x] frozen + `ClassVar kind` 标签；`isinstance` 判分支

### 4. 解析与业务校验

- [x] JSON 解析失败 / 字段缺失 / 类型错 → `LeadPlannerSchemaError`（触发重规划）
- [x] 确定性校验失败 → `LeadPlannerDeterministicError`（触发重规划，并把
      `type(exc).__name__` + 消息回灌模型）
- [x] Markdown 围栏 ```json ... ``` 自动剥除
- [x] 资源并行冲突（25A `validate_parallel_safe`）正确触发
- [x] 25A 循环依赖、缺依赖、重复 work_id、自依赖等校验**全部**经 `validate_batch`

### 5. 重规划（`max_planning_attempts=2`）

- [x] 首次失败 → 把「错误类型 + 消息 + 原始输出」拼进 user 消息后第二次
- [x] 第二次仍失败 → `LeadPlannerMaxAttemptsError`，**不**无限重试
- [x] 限制**独立**于 WorkItem `max_revisions_per_item=1`

### 6. 预算与持久化

- [x] 每次模型调用前 `RootBudget.consume_model_call()`（25C 架构收口后）
- [x] `RootBudget` Protocol：测试期 `BudgetRootBudget`（纯内存） / 25D 集成期
      `LeadLedgerRootBudget`（持久化）
- [x] `LeadLedgerRootBudget.consume_model_call` 走 25B
      `LeadBudgetLedger.consume_direct("model_calls")` copy-on-write 路径
- [x] 持久化失败 → `LeadPlannerBudgetError`，**模型未被调用**
- [x] 重启恢复：跨模块 `SQLiteLeadRunStore` + `LeadBudgetLedger.resources.
      restore_usage(...)` 回填已消耗额度（实测断言）
- [x] 成功 / 失败路径都正确记账 `model_calls_used`（每尝试 1 次）
- [x] **不**包 `BudgetMiddleware`（避免 25D 接入 Orchestrator 后双扣）
- [x] AST 守卫钉住「Planner 不 import BudgetMiddleware」

### 7. 文档与现实不符的修正

- [x] 25A 架构收口与 DEERFLOW_PARITY.md 称「`WAITING_FOR_INPUT` 已从状态机移除」
      —— **错**。`state_machine.py` line 101 / 120 实际保留该状态
      （仅去掉 `EXECUTING → WAITING_FOR_INPUT` 这条边）。
      25C 沿用源码，**不**为符合文档而重写 25A。
- [x] 25B 架构收口说 Planner「沿用官方：Lead Agent = LangGraph graph」
      —— 25C 阶段**不**引入 LangGraph，只产出 25D Orchestrator 需要的纯决策。
- [x] `task` / `batch_task` 是**参考语义**——25C 不复制官方签名，用 `FinalAnswer`
      JSON 即可。
- [x] **25C 架构收口自纠**：`RequestClarification.blocking_facts` 字段名暗示
      `fact_id`，但 25C 初版**校验的是 `VerifiedFacts.work_id`**——命名/语义错位。
      25C 改名 `blocking_fact_ids`、校验 `VerifiedFact.fact_id`（事实唯一键），
      并加 `test_blocking_fact_ids_uses_fact_id_not_work_id` 显式区分两者。

### 7.5 25C 架构收口：Planner 与预算的最小适配边界

第一轮实现 (`DefaultLeadPlanner(model, budget: Budget)`) 把 Planner 直接挂在
仓库既有的 `Budget` 上。`Budget.consume_model_call` 是**纯内存**计数，
**没有持久化、没有"钩子失败即不调模型"的真实契约**——之前测试里的"持久化
失败"是子类化 Budget 抛 `RuntimeError` 模拟的，**没有**走真实
`LeadRunStore`。

**最小适配**：在 Planner 与预算之间塞一个 `RootBudget` Protocol，
两条路径并存：

- `BudgetRootBudget`（`planner_budget.py`）——把 `Budget` 包成 `RootBudget`，
  保留 25C 初版测试的内存语义
- `LeadLedgerRootBudget`（`planner_budget.py`）——把 `LeadBudgetLedger`
  包成 `RootBudget`，`consume_model_call` 走 25B
  `LeadBudgetLedger.consume_direct("model_calls")` copy-on-write 路径
  （预检 → 算候选快照 → 调 `persist(candidate)` → 成功才回写）

**测试证据**（`tests/test_lead_agent_planner_budget.py`）：

| 测试 | 证明 |
|---|---|
| `test_planner_consume_persists_via_lead_ledger` | Planner 调模型前扣费**已**通过 `LeadLedgerRootBudget` → `consume_direct` → 钩子 → `SQLiteLeadRunStore.save_ledger_state` 落库；关闭 + 重开后库内账本可读 |
| `test_persistence_failure_blocks_model_call` | 持久化钩子抛错 → `LeadPlannerBudgetError`，**模型未被调用**，内存与数据库**都**未推进 |
| `test_restart_recovers_consumed_quota` | 跨进程模拟：进程 A 消耗 → 关数据库 → 进程 B 从持久化账本恢复 → 新 Planner 看到的是恢复后的额度（撞限时模型 0 次调用）|
| `test_middleware_does_not_double_charge_planner` | Planner **不**挂 `BudgetMiddleware`；防 25D 接入时双扣 |
| `test_budget_root_budget_does_not_persist` / `test_lead_ledger_root_budget_charges_via_consume_direct` | 两条适配器契约：内存路径 vs 持久化路径 |
| `test_planner_does_not_import_budget_middleware` | AST 守卫：未来无意 import Middleware 即失败 |

### 8. 门禁

| 命令 | 结果 |
|------|------|
| `uv run pytest tests/test_lead_agent_*.py -q`（定向） | **760 passed in 2.63s** |
| `uv run pytest -q`（全量） | **3050 passed + 42 subtests in 97.49s** |
| `uv pip check` | `All installed packages are compatible` |
| `uv run python -m compileall -q src/mini_deerflow/lead_agent` | exit 0 |
| `git diff --check` | exit 0，0 处空白错误 |

### 9. 本阶段新增文件

| 文件 | 行数 |
|---|---:|
| `src/mini_deerflow/lead_agent/planner.py` | 约 750 |
| `src/mini_deerflow/lead_agent/planner_budget.py` | 约 110（25C 架构收口新增）|
| `tests/test_lead_agent_planner.py` | 约 1200（40 个测试）|
| `tests/test_lead_agent_planner_budget.py` | 约 320（7 个测试，25C 架构收口新增）|

修改文件：`errors.py`（追加 5 个 Planner 异常）、`__init__.py`（导出 21 个新名）。

---

## 25D 验收清单

LeadRouter 是 Planner 与 Executor（25E）之间的**纯函数**：
- 接 `PlannerOutput`（ProposeBatch 走完整路由；RequestClarification /
  RecommendSynthesis 透传），产 `RoutedExecutionPlan` / `RoutingRejection` / `None`
- **不**调模型、**不**调 `ToolRegistry.execute`、**不**调 `SubagentManager.submit`、
  **不**开 `BudgetLease`、**不**改 `LeadRunState`
- **复用** 25A `validate_batch` + `validate_parallel_safe`，**不**重写
- `RoutingRejection` 由 Orchestrator（25D 后续 / 25E 入口）接收；
  Router **不**擅自改成 `RequestClarification`、**不**重规划

### 1. 边界

- [x] Router 是 Protocol + 纯函数实现；**不**是 Agent / Orchestrator
- [x] Router 不循环、不调模型、不执行工具、不派发 Subagent
- [x] Router **不**重写 25A 批次结构校验；复用 `validate_batch` 抛 6 类
      `BatchValidationError` 子类作为路由失败信号
- [x] Router **不**改 `LeadRunState` / 不开 `BudgetLease`
- [x] AST 守卫：router.py 不 import `Model` / `FakeModel` /
      `ToolRegistry` / `SubagentManager` / `open_lease` 等

### 2. 输入 `RoutingContext`（frozen）

- [x] `output: ProposeBatch | RequestClarification | RecommendSynthesis`（任意 PlannerOutput）
- [x] `goal: GoalContract` + `verified_facts: tuple[VerifiedFact, ...]`
- [x] `available_tools: frozenset[str]`（来自 ToolRegistry 集合，Router **不**持 Registry 引用）
- [x] `available_subagent_types: frozenset[str]` + `forbidden_subagent_types: frozenset[str]`
      默认 forbidden = `{"lead_agent"}`（防递归委派）
- [x] `max_concurrent_subagents` / `max_total_subagents` / `already_routed_in_response` /
      `already_routed_in_run`——Orchestrator 传；仿官方 subagent_limit_middleware

### 3. 输出（3 选 1 互斥，frozen + ClassVar kind）

- [x] `RoutedExecutionPlan(batches, rationale)` + `total_items` + `subagent_count`
- [x] `RoutingRejection(kind, message, offending_proposal_work_id)`
      `kind` 取 `RejectionKind.*` 之一（字符串相等）
- [x] **透传**：`RequestClarification` / `RecommendSynthesis` → `None`

### 4. 确定性规则

- [x] 25A 批次结构（6 类 `BatchValidationError`）→ `RoutingRejection`（kind = 错误类名）
- [x] **执行目标显式**：`ExecutionRoute.TOOL` 必须有 `tool_name ∈ available_tools`；
      `SUBAGENT` / `SUBAGENT_BATCH` 必须有 `subagent_type ∈ available_subagent_types ∧ ∉ forbidden`。
      **不**再从 `work_id:subagent_type` colon 形式解析——`work_id` 只表示任务身份
- [x] **域一致性**：DIRECT_AGENT 不带 tool_name/subagent_type；TOOL 不带 subagent_type；
      SUBAGENT/SUBAGENT_BATCH 不带 tool_name（WorkItemProposal 构造期校验）
- [x] **禁止递归委派**：`"lead_agent"` 默认在 `forbidden_subagent_types`；可扩展
- [x] per-response 并发截断：仅 SUBAGENT/SUBAGENT_BATCH 计入；超额丢弃**最早**（仿官方）
- [x] **截断后依赖闭合**：保留项的 `depends_on` 仍必须指向 `verified_facts ∪ 保留批次`；
      重检 25A `validate_batch`，发现 `MissingDependency` → 整批拒绝
      （**不**返回已不闭合的 `RoutedExecutionPlan`）
- [x] per-run 总量截断：仅 SUBAGENT/SUBAGENT_BATCH 计入；超额 → `TotalCapExceeded`
- [x] 资源并行冲突：25A `validate_parallel_safe(groups)` 二次确认（含父子路径重叠）
- [x] **不**重新实现 `validate_batch`——失败信号直接来自 25A

### 5. 拒绝处理

- [x] `RoutingRejection` 由 Orchestrator 接收；Router **不**自己决定重规划
- [x] Orchestrator 按 `kind` 分流：
      结构/语义错 → 写回 `PlannerContext.last_error` 重规划（**总次数由 Orchestrator 统一限制**）
      域/配置错（`UnknownTool/UnknownSubagentType/ForbiddenSubagentType`）→
      改 Orchestrator 配置（**不**走 Planner；**不**通过临时放宽
      `available_subagent_types` / `forbidden_subagent_types` 解决）
      截断 / 资源冲突（`ResourceConflict/TotalCapExceeded/PerResponseCapped`）→
      降级 / 拆分 / 降并发（**不**走 Planner）
- [x] **Planner 单次 `max_planning_attempts=2` ≠ 整个 Lead Run 的重规划上限**：
      `2` 是 Planner 单次 `plan()` 内部的两次模型尝试上限；Run 内的
      Planner 调用次数由 **Orchestrator（25E+）** 按 LeadRun 整体状态统一限制

### 6. 集成契约

- [x] **`RequestClarification` 空 `blocking_fact_ids` 表达"无对应事实的缺信息"**
      —— 字段类型已允许 `tuple[str, ...]`（含空元组）；Planner 校验通过；
      Router 透传；测试 `test_request_clarification_with_empty_blocking_fact_ids_is_passthrough` +
      `test_request_clarification_with_mixed_blocking_fact_ids_is_passthrough` 钉住
- [x] **`BudgetRootBudget` 标记为仅测试**：`_FOR_TESTING_ONLY: ClassVar[bool] = True`；
      25E Orchestrator 装配 Planner 时**强制** `isinstance(root_budget, LeadLedgerRootBudget)`；
      测试 `test_budget_root_budget_marked_for_testing_only` +
      `test_lead_ledger_root_budget_is_not_marked_for_testing_only` 钉住

### 7. 阶段归属校正

按用户要求同步校正：

| 阶段 | 内容 |
|---|---|
| 25D | LeadRouter 策略建议与执行权限分离（本次） |
| 25E | Executor + Reviewer |
| 25F | Synthesizer + Recovery |
| 25G | API + SSE |
| 25H | 前端 + E2E |

### 8. 门禁（25D 契约收口后）

| 命令 | 结果 |
|------|------|
| `uv run pytest tests/test_lead_agent_*.py -q`（定向） | **816 passed in 3.48s** |
| `uv run pytest -q`（全量） | **3106 passed + 42 subtests in 110.66s** |
| `uv pip check` | `All installed packages are compatible` |
| `uv run python -m compileall -q src/mini_deerflow/lead_agent` | exit 0 |
| `git diff --check` | exit 0，0 处空白错误 |

### 8.5 25D 契约收口（修复项 1–4）

#### 修复 1：显式带类型的执行目标

`WorkItemProposal` / `WorkItem` 25A 域新增两个**显式**路由目标字段：

- `tool_name: str | None = None` — `ExecutionRoute.TOOL` 时必填
- `subagent_type: str | None = None` — `ExecutionRoute.SUBAGENT` / `SUBAGENT_BATCH` 时必填

`work_id` **只**是任务身份标识，不再做 `work_id:subagent_type` 这种 colon
解析的妥协。Router 改读 `proposal.subagent_type` 与 `proposal.tool_name`
直接判定域，**不**回退到 work_id 推断。

序列化**向后兼容**：
- `to_dict()` 只在非 None 时写出键——老 payload（无新键）形状不变
- `from_dict()` 缺键默认为 None（DIRECT_AGENT 路线的不变式自然成立）
- `schema_version` 仍为 1——`LeadRunState.items` / `decisions.proposals` 的
  老数据可直接 `from_dict` 出来不报错

#### 修复 2：截断后依赖闭合

`_truncate_by_concurrency` 丢弃超额 subagent 后，保留项里可能仍有
`depends_on=<被截掉的 work_id>`。Router 在**重检**通过 `validate_batch`
（用**原始** `verified_work_ids`，**不**把被截的子任务当已验证——它们没真
跑过）：

- 闭合通过 → 继续走 per-run 总量 / 资源冲突检查
- 出现 `MissingDependency` → 拒绝整批 `RoutingRejection(kind="MissingDependency")`
  + 消息点明"被截的 task X 被依赖"

Router 承诺：**不**返回已不满足 `validate_batch` 的 `RoutedExecutionPlan`。
测试 `test_truncation_keeps_dependent_when_depended_subagent_dropped_rejects_batch`
钉住这条。

#### 修复 3：拒绝处理说明（文档层面）

`UNKNOWN_TOOL` / `UNKNOWN_SUBAGENT_TYPE` / `FORBIDDEN_SUBAGENT_TYPE` 是
**域/配置错**——**不**走 Planner 重规划（**不**通过临时放宽
`available_subagent_types` / `forbidden_subagent_types` 解决）。Orchestrator
按 `kind` 决策的边界：

- `Missing/Cyclic/Duplicate/Self/EmptyGoal` → 写回 `PlannerContext.last_error` → Planner **第二次**（Planner 单次 `max_planning_attempts=2` 仍生效）
- `UnknownTool/UnknownSubagentType/ForbiddenSubagentType` → 改 Orchestrator 配置（**不**走 Planner）
- `ResourceConflict/TotalCapExceeded/PerResponseCapped` → 降级 / 拆分 / 降并发（**不**走 Planner）

**Planner 单次调用的 `max_planning_attempts=2` ≠ 整个 Lead Run 的重规划上限**：
- `max_planning_attempts=2` = Planner 单次 `plan()` 内部的 2 次模型尝试上限
- Lead Run 内的"Planner 调用 N 次"由 **Orchestrator**（25E+）按 LeadRun 整体状态统一限制

#### 修复 4：RequestClarification 空 blocking_fact_ids 是正常场景

`RequestClarification.blocking_fact_ids` 类型是 `tuple[str, ...]`，**允许
空元组**。语义："我没有任何 VerifiedFact 可参考，纯缺信息"——由 Planner
业务校验通过，Router 透传。这是正常路径，**不**是反常。

测试 `test_request_clarification_empty_blocking_fact_ids_explicit_normal_path`
显式钉住：空元组过 Planner 业务校验、过 Router 透传。

### 9. 本阶段新增文件

| 文件 | 行数 |
|---|---:|
| `src/mini_deerflow/lead_agent/router.py` | 约 360 |
| `tests/test_lead_agent_router.py` | 约 600（38 个测试）|

修改文件：`planner_budget.py`（`BudgetRootBudget._FOR_TESTING_ONLY` 标记）、`__init__.py`（导出 9 个新公共名）。
