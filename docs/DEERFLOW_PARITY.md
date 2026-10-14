# Mini DeerFlow ↔ Official DeerFlow 对照审计（25B 收口 + 25C 前置）

> 本文档只使用官方来源：`https://github.com/bytedance/deer-flow` 仓库
> `main` 分支的代码、README、docs、已合并 PR；以文件:行为准，不以博客或
> 二手教程为准。
>
> 状态标记：
> **A** = 官方 DeerFlow 已确认行为
> **B** = Mini DeerFlow 当前实现
> **C** = Mini DeerFlow 自主工程设计（**不**声称是官方原样）
> **D** = 尚未实现、计划在 25C–25H 补齐

## 0. 绝对不可以声称是「官方原样实现」

以下设计是 Mini DeerFlow 的**自主工程取舍**，官方仓库不存在或语义不同，
必须明确区分；任何对外文档、PR 描述、handoff 都不能隐含这是 DeerFlow 自带：

| 自主设计 | 实质 | 官方对应 |
|---|---|---|
| 12 状态 `LeadRunState`（CREATED…SYNTHESIZING + 5 终态） | Mini 自创，规格 24 拍板 | 官方 ThreadState 走 LangGraph 内置 checkpoint，**不**显式建模状态机 |
| `SQLiteLeadRunStore`（CAS + 事件 + 快照） | Mini 自创 | 官方 ThreadState 由 LangGraph 自带持久化（`MemoryStore`/`Postgres`），不维护独立 SQLite 事件表 |
| `BudgetLease` / `LeadBudgetLedger` | Mini 自创 | 官方用 LangChain `BaseCallbackHandler` 收集 token 用量（`SubagentTokenCollector`），不做资源级四类计数 |
| `ResourceClaim` 父子路径冲突 | Mini 自创 | 官方没有声明式资源访问表，Subagent 工具受 `BASH_AGENT_CONFIG.allowed_tools` 显式限制 |
| 最多一次修订（`max_revisions_per_item=1`） | Mini 自创 | 官方无对应概念 |

下面表格中**任何**字段出现以上设计，都标 **C**。

---

## 1. 对照表

| # | DeerFlow 能力 | 官方文件 / 模块 | 官方行为（A） | Mini 对应模块（B / C / D） | 当前状态 | 差距 / 计划 |
|---|---|---|---|---|---|---|
| 1.1 | **Lead Agent 是否为独立类** | `agents/lead_agent/agent.py` `make_lead_agent()` ＋ `agents/lead_agent/prompt.py` | `make_lead_agent(config)` 返回 LangGraph graph；`apply_prompt_template(...)` 动态渲染系统提示词；模型不显式分层，由 LangGraph `messages` 状态承载。**没有独立的 Planner 类**——Lead Agent 本身就是一个 LangGraph 图，**通过系统提示词 + 工具**驱动。 | `src/mini_deerflow/lead_agent/`（领域层已就位） | **D**：Phase 25C 才会接 `assemble_lead_agent(graph_factory)` | 25C 实施 |
| 1.2 | **Lead Agent 如何决定是否委派** | `agents/lead_agent/prompt.py`（系统提示词） | 三段式：**核心原则**「Subagents are optional. Default to direct execution. Don't delegate merely because a task is complex / has many steps / produces verbose output」；**强制 DELEGATION CHECK**「Expected benefit = parallel wall-clock savings + specialist capability + context isolation」；「Expected cost = delegation startup + duplicate context + coordination/synthesis + state-conflict risk + side-effect risk」；**结论**「Delegate only when the expected benefit is clearly greater than the expected cost」。 | 系统提示词章节（**待写**） | **D** | 25C 写 prompt 章节，**直接翻译**官方决策表 + 强约束措辞（HARD LIMITS） |
| 1.3 | **三种合法委派收益** | 同上 | (i) **Parallel latency**（独立 / 不重叠 / 可并行 → 墙钟节省）；(ii) **Specialist capability**（子代理有专门工具 / 技能 / 模型 / 领域指令）；(iii) **Context isolation**（bounded + 上下文重的调研否则会冲掉 Lead 上下文）。 | 25C 决定把三条落到 `ExecutionRoute` 选择逻辑上 | **C**（映射到 `direct` / `tool` / `subagent` / `subagent_batch` 四选） | 25C |
| 1.4 | **task / batch_task 工具职责** | `tools/builtins/task_tool.py` ＋ `tools/builtins/batch_task_tool.py` | `task`：`@tool("task", parse_docstring=True)`，签名 `(runtime, prompt, subagent_type, tool_call_id, *, acceptance_criteria=None, description="", context_mode="isolated"|"snapshot")`；同步等待；返回 `Command` 带 `ToolMessage`（result / usage / citations / verdicts）。`batch_task`：返回 batch_id **立即返回**；用于"many independent, idempotent, or read-only items"；**survives Gateway restarts**；通过 `read_batch_result` 单独取回。 | `WorkItem.execution_route` ∈ {`direct_agent`, `tool`, `subagent`, `subagent_batch`}（**C**：四类映射官方两类的细化） | **B** 模型已就位 / **D** 工具实现未做 | 25C 接 LangChain `StructuredTool` / `BaseTool` |
| 1.5 | **硬上限（per response + per run）** | `agents/middlewares/subagent_limit_middleware.py` | `max_concurrent=3` / `max_total=6`（可配置），**在 `after_model` 阶段截断**；按 `run_id` 隔离计数；超额丢弃最早的，`stop_reason: subagent_limit_capped`，给模型追加 `_TOTAL_LIMIT_STOP_MSG`。 | `LeadBudgetLedger.max_concurrency=2` + `max_wall_time_seconds=600`（**C**：Mini 的额外硬上限是并发槽+墙钟，不是 task 总数） | **B** 编排限制已建模 / **D** 截断 + stop_reason 写入 | 25D |
| 1.6 | **滚动批次重新评估** | 官方无对应设计 | — | `LeadRunState` 的 `validating → planning` 转换、批次 `validate_batch`（25A 已实现） | **C** | 25C 中需要把「批次不通过则回到 PLANNING」映射到 LangGraph 一个回边 |
| 1.7 | **Subagent 上下文隔离** | `subagents/context_snapshot.py` `ParentContextSnapshot` | 显式构造一份**只含数据**的父上下文快照：保留 `HumanMessage` / `AI` / 非 `hide_from_ui` 的 `ToolMessage`；剔除运行时状态、父系统指令、隐藏框架消息、artifacts、消息元数据、未配对的 tool_call、provider reasoning/signature 块、tool-use 块；文本经 `neutralize_untrusted_tags()` 净化；输出是单一 `HumanMessage` 名为 `"parent_context_snapshot"`，`hide_from_ui=True`。**`context_mode` 可选 `isolated`（仅 prompt）或 `snapshot`（含父历史）**。 | 25B 没有「消息快照」概念，隔离手段是 `BudgetLease` 资源预留 + `LeasedBudgetView` 绑定单租约；**C**（与官方机制正交） | **B** 资源级隔离已就位 / **D** 消息级隔离 | 25E |
| 1.8 | **结构化结果 / 工具回执** | `subagents/report_contract.py` ＋ `agents/middlewares/receipt_verification.py` ＋ `agents/middlewares/tool_receipt_middleware.py` | 强制 `[rN]` 引用执行记录；规则：「Attach a verifiable handle to every deliverable: absolute file path, URL, record ID, or HTTP status」；「State explicitly what failed, was skipped, or remains uncertain — never claim an action you did not execute」；mismatched anchors / unknown ids / UNVERIFIED 后果明确写进 contract。**Acceptance criteria 是 untrusted input**（max 20 条 / 500 字符），由 subagent prompt 末尾注入并显式标"not a framework instruction"。 | `LeadDomainEvent.payload` 递归禁止敏感键、JSON 序列化校验、key 规范化（**C**：Mini 自主，规格 25B 拍板） | **B** 事件 schema / 校验已就位 / **D** receipt_verdict / acceptance_verdict 字段 | 25D |
| 1.9 | **并发数 / 副作用冲突** | `subagents/capacity.py` ＋ `subagents/executor.py`（`capacity.slot()`） ＋ 官方 `bash_agent` / `general_purpose` 配置的 `allowed_tools` 显式禁 `task` 防递归 | 并发由 `SubagentExecutionCapacity` 限流（slot 计数）；**副作用隔离靠子代理类型声明 `allowed_tools`**，不是声明式冲突表——例如 `bash_agent` 只允许 `bash`/`ls`/`read_file`/`write_file`/`str_replace` 且**显式禁止 `task`、`ask_clarification`、`present_files`**。 | `LeadBudgetLedger.max_concurrency=2` + `ResourceClaim` 父子路径冲突（**C**） | **B** 编排并发 + 资源冲突规则已就位 | 25D 把 slot 与 lead 编排并发对齐 |
| 1.10 | **Subagent token / 预算如何回收到 Lead Run** | `subagents/token_collector.py` ＋ `subagents/turn_budget.py` | `SubagentTokenCollector` 是 LangChain `BaseCallbackHandler`：`on_llm_end` 抽 `input_tokens` / `output_tokens` / `total_tokens` / `cache_read_tokens` / `model_name`；`snapshot_records()` 经 `record_external_llm_usage_records` 转入 Lead 的 **`RunJournal`**（不是 Budget）。 | `Budget.consume_subagent_task(depth)` 走现有 `middleware.Budget`（**C**：Mini 把 token 折算成 `subagent_tasks` 计数 + `max_subagent_depth`；官方不做这种折算） | **B** 计数侧已建模 / **D** RunJournal 形式记账 | 25C 在 `RunJournal` 加 subagent 字段 |
| 1.11 | **中断 / 取消 / 恢复 / durable delegation ledger** | `agents/middlewares/delegation_ledger.py` ＋ `agents/middlewares/durable_context_middleware.py` ＋ `subagents/batch_service.py` | `DelegationEntry` 两阶段捕获：**Phase 1** AI 发 `task` tool_call → 写 `in_progress` + UTC timestamp；**Phase 2** 收到 `ToolMessage` → 补 `status` / `stop_reason` / `receipt_verdict` / `acceptance_verdict` / `result_brief` / `result_sha256`。`render_delegation_ledger()` 输出模型可见 markdown，**按预算感知**裁掉较老条目；**确定性截断**（不用 LLM 摘要，全部 HTML 转义）。`SubagentBatchService` 提供 lease/execute/recover（`claim_items(lease_owner, lease_seconds)`，崩溃后另一 worker 可 reclaim；item 用 `item_key` 当幂等身份；`max_live_items` / `max_running_items` / `max_attempts` 限制）。 | `LeadRunState` 12 状态机 + `LeadRunStore` CAS 快照（**C**：Mini 自主，不是 LangGraph checkpoint） | **B** 状态机 / 持久化 / 恢复基础已就位 | 25C 写 Phase 1/2 事件映射；25D 写 lease 类似的 batch 恢复 |
| 1.12 | **Planner 是否为独立类** | 官方无独立 Planner | 决策由 Lead Agent 的 LLM 在每条响应里做（系统提示词 + `task`/`batch_task` 工具 + middleware 截断） | 25C 决定**沿用官方模型**——不引入独立 Planner 类；通过系统提示词 + 工具 + middleware 截断 | **D** | 25C（决策：不写独立 Planner） |
| 1.13 | **Lead vs Subagent Middleware 区别** | `agents/middlewares/` ＋ `subagents/executor.py` `create_agent()` | 同一仓库**两套独立 middleware 栈**：Lead 端有 `subagent_limit_middleware` / `delegation_ledger` / `clarification_middleware` / `token_budget_middleware` / `todo_middleware` 等；Subagent 端由 `create_agent()` 自建栈（`knowledge_scope_middleware` / `loop_detection_middleware` / `clarification_middleware` 等同名但独立实例）；**核心 `Budget` 资源由两边共享**（同进程） | Mini 端是**同一把** `middleware.Budget` 实例（`LeadBudgetLedger.resources`）跨 Lead 与 Subagent 复用（**C**：Mini 用 `Budget as SubagentBudget` 别名） | **B** | 25C 在合成 `assemble_subagent_agent` 时把 `Budget` 注入 |
| 1.14 | **最终综合结果如何产生** | `agents/middlewares/terminal_response_middleware.py` ＋ Lead Agent 自身 | **最终综合由 Lead Agent 的 LLM 完成**——`task`/`batch_task` 返回的 `ToolMessage` 作为输入，Lead 自行写最终答案。`TerminalResponseMiddleware` 仅在模型返回空 `AIMessage` 时注入兜底；**不**有独立的 Synthesizer 节点。 | 25C 决定**沿用**——不引入独立 Synthesizer；最终综合是 Lead Agent 的 LLM 行为 | **D** | 25C（决策：不写独立 Synthesizer） |
| 1.15 | **API / SSE / 前端展示 Subagent 状态** | `subagents/step_events.py` ＋ `agents/middlewares/subagent_limit_middleware.py` | 自定义事件 `task_started` / `task_running` / `task_completed` / `task_failed` / `task_cancelled` / `task_timed_out` 走 **root namespace**（`subagent_start_event` / `subagent_end_event`），通过 LangGraph `astream` 流到 Gateway → SSE。`task_completed` payload 含 `task_id` / `status` / `model_name` / `usage` / `result`（截断 8192）/ `error`（截断 8192）。**前端不请求子图流**——只看根 namespace。 | Phase 25E FastAPI/SSE | **D** | 25E 复用 `task_*` 事件名 + 截断 8192 字节 |

---

## 2. 25C 设计建议（**不写代码**）

> 仅给方向，25C 开工前再与用户对齐。

### 2.1 Planner 的输入

- **Goal 契约**（已有，25A）：`GoalContract(goal_id, objective, acceptance_criteria)`
- **Lead Run 当前快照**（25B）：`LeadRunState.to_dict()` 含 `decisions` / `verified_facts` / `items` / `batch_index` / `updated_at`
- **可用资源**：`Budget` 限额 + `max_concurrency` + `max_lease_credits` + 当前 `available_for(r)`（四类）
- **可用工具白名单** + 已知子代理类型（`bash` / `general_purpose` / 用户自定义）
- **Delegation 账本历史**：上一批次 `LeadDomainEvent` 流（25B 已落库）

**不输入**：父系统提示词原文、`artifacts` 完整内容、未配对 tool_call。

### 2.2 Planner 的结构化输出

复用 25A 的 `WorkItemProposal` 集合 + 25B 的 `ExecutionRoute`：

```text
WorkItemProposal(
    work_id,                 # 唯一稳定 ID
    title, description,      # 模型产出
    execution_route,         # direct_agent | tool | subagent | subagent_batch
    subagent_type,           # 仅 subagent_* 时填；其余为 None
    claims,                  # ResourceClaim[]（25A 已有，25C 接 enforcement）
    depends_on,              # 现有批次内 / 验证过的历史
    acceptance_criteria,     # list[str]，每条 ≤ 500 字符（沿用官方上限）
)
```

`batch_task` 模式下，输出一个 `BatchSpec(items=[WorkItemProposal...], max_live_items, max_running_items, item_key_template)`（**C**：Mini 自主把官方 batch 参数化）。

### 2.3 Rolling Batch 边界

- **批次不通过**（缺依赖 / 循环 / 资源冲突） → `LeadRunState` `validating → planning` 回边，Planner 拿新上下文**重做**，**不**直接落到执行
- **批次通过** → `planning → executing`（单向：执行中不再追加同批条目）
- **子代理失败** → `WorkItem` `running → failed` → 若 `attempts < max_revisions_per_item`（=1）回到 `planning` 重新规划**整批**（**C**：Mini 设计的"整批回炉"是差异点）

### 2.4 `WAITING_FOR_INPUT` 判定

**沿用官方**——不引入新状态名。Mini 这边在 25A 已经把 `WAITING_FOR_INPUT` 从状态机移除（架构收口第 2 项），改用：

- Lead Agent 调用 `ask_clarification` 工具（**待 25C 接入**）→ `ClarificationMiddleware` 拦截 → 写 `human_input_payload` 事件到 `LeadDomainEvent` 流 → 暂停图执行
- 重启后从事件流里看到 `human_input_payload` 且后续无对应 `human_response` → 渲染给前端的"等待输入"卡片
- 用户回应作为新事件续接（25E 实现 FastAPI/SSE 时落地）

**判定就是事件流查询**，不维护独立状态字段。

### 2.5 `direct` / `tool` / `subagent` / `subagent_batch` 选择

按官方 `prompt.py` 决策表**逐字翻译**为规则引擎输入（**不**让 LLM 自由发挥）：

| 情形 | 选择 |
|---|---|
| 同步、可验证、上下文轻 | `direct_agent`（即 Lead 自身带工具调用） |
| 单一外部工具能完成（search/fetch/read/write） | `tool` |
| 复杂、上下文重、需要专门技能 | `subagent`（带 `subagent_type=general-purpose` 或 `bash`） |
| 大量独立、幂等 / 只读、可能跨 Gateway 重启 | `subagent_batch` |
| 上述都不满足 | **不调用工具**，Lead 自身回答 |

**关键：决策在 LLM**——它读完 Goal + 历史 + 决策表后**自己**填 `execution_route`；Mini 这边**只做截断和校验**，不替 LLM 决策（**C**：Mini 在 25A 留了 `WorkItemProposal.execution_route` 字段，25C 让 LLM 填它）。

### 2.6 如何控制最少有效 Subagent

**不追求"最少"**——追求"足够"。Mini 这边两条硬闸：

- **每响应上限**：借鉴 `subagent_limit_middleware` 的 `max_concurrent` 截断（Mini 叫 `max_concurrency=2`，截到 2 个）
- **每 Run 上限**：借鉴 `max_total` 截断（Mini 暂定为 12 = `max_work_items`；与官方 6 不同是因为 Mini 还有批次约束）
- **最少有效**：Planner 输出的 `WorkItemProposal` 数量 **必须 ≥ 1**（25A `validate_batch` 已有非空检查）；**不强制**"恰好多少"，让 LLM 自行判断

### 2.7 确定性校验失败如何修正

按 25A 的 `validate_batch` 六级错误分级：

| 错误类型 | 修正路径 |
|---|---|
| `DuplicateWorkIdError` | Planner 重做（去重） |
| `SelfDependencyError` | Planner 重做（去掉自指） |
| `MissingDependencyError` | Planner 重做（补依赖或换结构） |
| `CyclicDependencyError` | Planner 重做（破环） |
| `EmptyGoalFieldError` | 几乎不可达（25A 构造期已拦），仅做防御性回退 |
| `UnknownResourceError` / `ResourceConflictError` | Planner 重做（重新声明 resources / 拆分批次） |

**不在校验失败时自动调用 LLM 改写**——按 25A 决定，把错误回写 Planner 让它**整批重做**；只在出现**第 N+1 次**重做仍失败时把 Run 落到 FAILED（**C**：Mini 自主加的兜底）。

### 2.8 Planner 的预算消费点

**Planner 自身**：

- **不**直接扣 `Budget.model_calls`——它在 Lead Agent 内部，已经被 Lead 的 `_lock`-guarded `consume_model_call` 计了
- 走 `commit_lease_charge` 的 `consume_model_call` 路径（同 Lead Agent 调模型那一步）

**它产出的子代理**：

- 创建时 `open_lease(reserved_by_resource={"model_calls": N, "tool_calls": M, ...})`，**资源级预留**走 25B `BudgetLease`
- 子代理消费走 `LeasedBudgetView` → `lease.consume(resource, amount)` → 25B 的 `commit_lease_charge` copy-on-write 路径
- `token_collector` 折算到 `subagent_tasks` 后走 `Budget.consume_subagent_task(depth)`（25B `consume_direct`）

**预算回收**（即 close lease）：

- 子代理 COMPLETED / FAILED / CANCELLED → 25D 在 SubagentExecutor 终止路径里调 `lease.close()`，仅归还 `remaining_reserved`，**consumed 永不归还**
- 跨 Gateway 重启（25D）：用 `delegation_ledger` + `Lease.status` 重建（**C**：Mini 自主把官方 ledger 改造为本仓库的 `LeadDomainEvent` 形式）

---

## 3. 25C 风险与决策备忘

| # | 风险 / 决策 | 处理 |
|---|---|---|
| R1 | 是否在 25C 引入独立 Planner 类 | **不引入**——沿用官方：Lead Agent = LangGraph graph，决策由 LLM 在系统提示词 + 工具下完成；Mini 仅做截断和校验 |
| R2 | 是否引入独立 Synthesizer 节点 | **不引入**——最终综合仍是 Lead Agent 的 LLM 行为；不另立一阶段 |
| R3 | `WAITING_FOR_INPUT` 状态字段 | **不引入**——按官方用 `ask_clarification` 工具 + 事件流查询实现"等待" |
| R4 | 资源冲突 vs 官方 `allowed_tools` 显式禁递归 | **并存**——Mini 的 `ResourceClaim` 父子路径冲突是声明式声明，官方 `allowed_tools` 是子代理能力声明；两者在 25D 都需要 |
| R5 | Subagent 上下文消息快照 vs 资源预留隔离 | **只做资源预留**（25B 已有）——消息级快照留到 25E，**优先级低** |
| R6 | Mini 的 `BudgetLease` 资源级预留 vs 官方 `RunJournal` token 收集 | **并存**——`BudgetLease` 跑在 `commit_lease_charge` copy-on-write 路径上；`RunJournal` 25C 加 subagent 字段作为审计形式 |

---

## 4. 官方证据路径（仅 `https://github.com/bytedance/deer-flow` `main` 分支）

| 主题 | 文件 / 行 | 直接证据 |
|---|---|---|
| Lead Agent 工厂 | `backend/packages/harness/deerflow/agents/lead_agent/agent.py` `make_lead_agent` | "LangGraph graph factory; keep the signature compatible with LangGraph Server" |
| 系统提示词与委派决策 | `backend/packages/harness/deerflow/agents/lead_agent/prompt.py` | "Subagents are optional. Default to direct execution." ＋ "DELEGATION CHECK (required before every `task` call)" |
| task 工具 | `backend/packages/harness/deerflow/tools/builtins/task_tool.py` | `@tool("task", parse_docstring=True)`，签名含 `prompt / subagent_type / tool_call_id / acceptance_criteria / description / context_mode` |
| batch_task 工具 | `backend/packages/harness/deerflow/tools/builtins/batch_task_tool.py` | "Submit many independent items to DeerFlow's explicit durable batch mode" / "Survives Gateway restarts" |
| SubagentExecutor | `backend/packages/harness/deerflow/subagents/executor.py` | "Acquires an execution slot via `capacity.slot()`" / "Runs on an isolated event loop" |
| SubagentResult 状态 | 同上 | `PENDING → RUNNING → COMPLETED/FAILED/CANCELLED/TIMED_OUT` + `stop_reason: token_capped/loop_capped` |
| 上下文快照 | `backend/packages/harness/deerflow/subagents/context_snapshot.py` `ParentContextSnapshot` | "Excludes: runtime state, parent system instructions, hidden framework messages..." ＋ "context_mode: 'isolated' or 'snapshot'" |
| Token 回收 | `backend/packages/harness/deerflow/subagents/token_collector.py` `SubagentTokenCollector` | "snapshot_records() → record_external_llm_usage_records" |
| Durable ledger | `backend/packages/harness/deerflow/agents/middlewares/delegation_ledger.py` | "Two-phase capture pattern" ＋ "budget-aware omission of older entries" ＋ "deterministic truncation" |
| 硬上限 | `backend/packages/harness/deerflow/agents/middlewares/subagent_limit_middleware.py` | `max_concurrent=3` / `max_total=6` / "preserves earliest calls" |
| 批处理 lease | `backend/packages/harness/deerflow/subagents/batch_service.py` `SubagentBatchService` | "lease/execute/recover" ＋ "claim_items with lease_owner and lease_seconds" |
| 回执 / 引用 | `backend/packages/harness/deerflow/subagents/report_contract.py` | "Attach a verifiable handle to every deliverable" ＋ MAX_ACCEPTANCE_CRITERIA=20 / MAX_CRITERION_CHARS=500 |
| 澄清工具 | `backend/packages/harness/deerflow/tools/builtins/clarification_tool.py` ＋ `agents/middlewares/clarification_middleware.py` | `Command(goto=__end__)` 暂停图执行；无状态名 |
| SSE 事件 | `backend/packages/harness/deerflow/subagents/step_events.py` | `task_started` / `task_completed` 等事件 + 8192 字符截断 |
| ThreadState | `backend/packages/harness/deerflow/agents/thread_state.py` | `delegations: list[DelegationEntry]` 字段 + `merge_delegations` 归约器（终态永不被非终态覆盖） |
| 子代理类型 | `backend/packages/harness/deerflow/subagents/builtins/bash_agent.py` | `BASH_AGENT_CONFIG.allowed_tools` 显式禁 `task` / `ask_clarification` / `present_files` |
| 架构概览 | `docs/ARCHITECTURE.md` | "A single 'lead agent' orchestrates sub-agents" ＋ "Sub-agents — background delegation via SubagentExecutor (server-side execution_id) correlated to provider tool_call_id" |
