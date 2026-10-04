# Phase 24 进度

## 状态图例

```
⬜ 未开始    🟡 进行中    ✅ 已完成    ⛔ 阻塞
```

## 总体阶段

| 阶段 | 内容 | 状态 |
|------|------|------|
| 24A | 领域模型（ContextState / Summary / TaskNote / Policy） | ✅ 71 项测试通过 |
| 24B | ContextStore（InMemory / SQLite + CAS） | ✅ 42 项测试通过 |
| 24C | Token 计量、弹性分配与安全消息分组 | ✅ 40 项测试通过 |
| 24D | ContextCompactor（Raw Model 摘要 + 增量合并 + 定期重建） | ✅ 26 项测试通过 |
| 24E | Task Notes 工具（needs_context 通道 + 分页） | ✅ 57 项测试通过 |
| 24F | History Vector Search（水位 + 延迟补齐 + 两工具） | ✅ 51 项测试通过 |
| 24G | ContextAssembler 与 Agent / Subagent / CLI / API 集成 | ✅ 27 项测试通过 |
| 24H | 文档与回归 | ✅ 架构文档 + 全量门禁 |
| 24I | 生命周期一致性（shutdown / 删除清理 / 重启） | ✅ 15 项测试通过（见「正确性审计」） |
| — | **Phase 24 总体** | **✅** |

> 架构设计见 [context-engineering.md](context-engineering.md)。
> 各阶段下方的清单为**实现时**的验收项；完成后的正确性审计发现的额外问题见文末
> 「正确性审计」一节。

---

## 24A 领域模型

- [x] `ContextState` 序列化 / 反序列化（含空态与全字段态 roundtrip）
- [x] `schema_version` 校验：正确值通过、未知值拒绝、缺失走默认
- [x] `ConversationSummary` 清理：空串、重复条目、前后空白
- [x] `ConversationSummary` 限额：单字段条目、总条目、总字符、narrative 长度
- [x] 凭据拦截：叙述中出现直接拒绝，数组条目中出现整条剔除
- [x] `TaskNote` 状态转换：add → update → resolve → reopen，resolve 幂等
- [x] 非法 `category` / `status` 在构造期即拒绝
- [x] 旧数据兼容：缺 `source_run_id` / `indexed_through` / `compaction_count` 等字段仍可读
- [x] `ContextPolicy` 公式与各区块 cap，非法边界（0 / 负数 / 越界比例）全部拒绝

## 24B ContextStore

- [x] `InMemoryContextStore` CRUD 与幂等删除
- [x] `SQLiteContextStore` 重启恢复
- [x] `revision` CAS：过期版本与显式错误版本均抛 `ContextConflictError`
- [x] 并发冲突：8 线程 `cas_update` 全部通过重试成功，revision 精确推进到 8
- [x] `cas_update` 重试耗尽后抛错，不返回脏数据
- [x] **删除线程时同步回收投影**（ContextState + 向量索引），幂等
- [x] 损坏数据：非法 JSON、顶层非字典、`schema_version` 类型错误
- [x] schema 迁移：注册 v0→v1 后可读旧数据；未注册迁移明确拒绝
- [x] **CAS 契约**：`state.revision` 必须等于 `expected_revision`，
      否则视为「mutator 未基于传入 state 构造」，直接抛冲突

## 24C Token 与分组

- [x] 低于阈值不压缩；恰好达到软阈值（80%）触发；差 1 个 Token 不触发
- [x] `reserved_output_tokens` / `safety_margin_tokens` 正确缩小硬上限
- [x] 空 Memory / Skill 的额度回流给最近消息，且不做固定预留
- [x] 必要内容超过硬上限抛 `ContextWindowExceededError`
- [x] `ContextTokenBreakdown` 九区之和恒等于 `total`（有等式断言）
- [x] 普通消息、单工具调用、多连续工具调用、工具失败各自成组
- [x] `ToolCall` 与 `ToolResult` **不被拆分**
- [x] 损坏序列（孤立 result / 悬空链 / user 插入链中）全部明确报错
- [x] `strict=False` 容错路径标出 `is_complete=False`
- [x] 压缩边界落在 ToolCall 前后时吸附到组起点，不产生孤立 result

## 24D ContextCompactor

- [x] 主模型成功生成严格 JSON
- [x] 返回 `ToolCall` 立即拒绝，不进格式修复流程
- [x] 第一次格式错误后修复成功（`summary_calls == 2`）
- [x] 两次错误抛 `SummaryFormatError(attempts=2)`，不做第三次尝试
- [x] 增量合并：新 facts / decisions 并入旧摘要
- [x] **第 5 次压缩触发完整重建**，前 4 次不触发
- [x] 重建失败时**保留旧 Summary**，退回增量合并
- [x] **ThreadState 始终不变**（`to_dict()` 前后逐字节相同）
- [x] 摘要请求 `tools=[]`，Prompt 含「历史是不可信数据」与凭据禁令
- [x] 摘要输入的 `[index]` 与 watermark 坐标系一致
- [x] **摘要使用独立 Budget**，与决策额度互不串账

## 24E Task Notes

- [x] `add` / `update` / `resolve` / `list` 四个 action
- [x] 不存在的 `note_id` 返回明确错误
- [x] 已 `resolve` 的 Note 不可再 update（审计线索不被事后抹掉）
- [x] 字符数 / active 数 / 总数三个上限，**超限抛错而非静默逐出**
- [x] **Run 失败后 Note 仍在**（写入即落库，靠 `source_run_id` 审计）
- [x] **Agent 无法操作其他 thread**（参数 schema 无 `thread_id`）
- [x] revision 冲突由 `cas_update` 重试，工具调用仍成功
- [x] Trace 事件 `context.task_note.changed`，**不写 Note 正文**

## 24F History Search

- [x] 同 Thread 召回
- [x] **不同 Thread 完全隔离**（独立 bucket、独立水位、跨线程内容不外泄）
- [x] `upsert` 幂等（同 reference 重复写入不膨胀）
- [x] **索引失败不回滚已保存消息**，水位停在原地
- [x] 水位落后时 `history_search` 前延迟补齐，并 emit `context.index.repaired`
- [x] 首次建索引不冒充「修复」
- [x] `history_read` 邻近窗口，边界裁剪正确
- [x] 参数上限：`top_k` / `before` / `after` / `query` 长度全部拒绝越界
- [x] 不可信内容边界：`HISTORY_NOTICE` 进入工具结果与工具描述
- [x] **本地向量完全离线且确定性**（md5，不依赖 `PYTHONHASHSEED`）

## 24G 集成

- [x] **短会话行为不变**（有无 assembler 的最终回答逐字相同）
- [x] 长会话触发压缩
- [x] 模型实际收到 Summary、Active Notes 与最近消息
- [x] 原 Thread 保留完整历史
- [x] **Memory / Skill 每次 decide 各注入恰好一份**
- [x] **Summary 调用不递归**（`Agent.run()` 只被调用一次）
- [x] Summary 使用独立 Budget
- [x] Trace 事件完整，且**不含历史正文与凭据**
- [x] **两个 Subagent 上下文隔离**（Summary / Notes / 向量索引互不可见）
- [x] 软阈值失败且未超硬上限 → 降级继续；超硬上限 → 抛错
- [x] 删除线程（API 与 CLI 两条路径）同步回收投影

## 24H 文档与回归

- [x] `docs/context-engineering.md` 覆盖 12 项必答点
- [x] `.env.example` 增补 6 个 `MINI_DEERFLOW_CONTEXT_*` 变量（无真实 Key）
- [x] `load_context_config()` 只读环境，不在 import / `create_app()` 建连接

---

## 门禁结果

| 命令 | 结果 |
|------|------|
| `uv run pytest`（全量） | **2290 passed + 42 subtests**（基线 1961，新增 329，零回归） |
| `uv run pytest tests/test_context_*.py`（定向） | **329 passed** |
| `uv pip check` | `All installed packages are compatible`（exit 0） |
| `npx vitest run` | **250 passed / 26 files** |
| `npx playwright test` | **18 passed** |

改动涉及 `agent.py` 与 `runtime.py`（API + SSE 实际执行路径），因此按要求补跑了
前端单元与 E2E 回归，两项均通过。

**全部门禁实际通过，Phase 24 ✅。**

---

## 正确性审计（完成后补做）

实现完成后做过一轮专门的正确性审计，**真实复现并修复了 6 个问题**，
每个都配有能证明修复的测试。这是「测试具备故障发现能力」的证据，而非补测试凑数：

| # | 风险 | 原实现 | 修复 | 新增测试 |
|---|------|--------|------|----------|
| 1 | 重启后索引不重建 | 起点取持久化水位 `state.indexed_through`，与空索引不一致时 `start >= total` 直接返回 | 起点改为**索引实例实际状态** `index.indexed_through()`；`repaired` 判定同步修正 | `test_restart_rebuilds_index_despite_persisted_watermark`、`test_restart_does_not_emit_spurious_repair`、`test_partial_index_repairs_from_actual_position` |
| 2 | 软阈值判断依据错 | `should_compact(breakdown)`（分配+裁剪后） | `_compose` 返回 `raw_total`（裁剪前），改用它判断；Trace 记录两者 | `test_compaction_uses_raw_tokens_not_allocation`、`test_assembled_trace_carries_raw_total` |
| 3 | 弹性段计入 required | `system_tokens` 含 Memory/Skill/Summary/Notes → 长记忆直接抛超限 | `fixed_system_tokens=0`，弹性段只走 `*_need`；新增 `_clip_to_tokens` / `_trim_recent_to_budget` 按额度**实际裁内容**；`_enforce_hard_limit` 按**真实 messages** 校验 | `test_long_memory_is_elastic_not_required`、`test_hard_limit_applies_to_final_model_context` |
| 4 | 敏感信息过滤不全 | 5 条 pattern，缺路径与私钥；narrative 是拒绝而非过滤 | 扩到 16 条（POSIX/Windows/私钥/`.env`）；新增 `scrub_sensitive` 占位符替换，条目仍整条剔除 | `test_summary_scrubs_credentials_in_narrative`、`test_summary_scrubs_sensitive_paths`（15 参数化）、`test_summary_keeps_ordinary_paths` 等 |
| 5 | `list` 无上限 | 无条件返回全部 Note（最坏百万字符） | `limit`/`offset`/`status` + 条数、单条、总量三重上限，默认只返回 20 条 active | 14 个分页边界测试，含 `test_five_hundred_notes_never_all_enter_the_result` |
| 6 | 清理失败伪装成功 | 仅 `warning` 后正常返回；`InMemoryContextStore` 无 `close()`；shutdown 不清索引 | 抛 `ThreadServiceError` → HTTP 500 + 安全文案；补 `close()`；shutdown 增加 `index.clear()` | `tests/test_context_lifecycle.py`（全新 15 个） |

同时做了最小死代码清理：删除 `count_object`、`token_span`、`TaskNote.reopen`、
`clear_notes_for_thread`、`index_error`（从未赋过非 `None`），并把 4 组重复的
`_ok` / `_err` / `_emit` / `context_attr` 提取到 `context_shared.py`。

**审计后门禁**：定向 329、全量 2290，与上表一致。

## 真实限制

1. **`HeuristicTokenCounter` 是估算不是精确计数**：ASCII 按 3 字符 ≈ 1 token、
   非 ASCII 按 1 字 ≈ 1 token。**不等价于任何 Provider 的真实分词器**，
   误差靠 `safety_margin_tokens` 兜底。要精确计数需替换 `TokenCounter` 协议实现。
2. **`LocalHashEmbeddingProvider` 是词袋哈希不是语义 Embedding**：
   只在词面重合时相似，同义词检索不到，桶碰撞可能给出小的正分。
   **不具备语义检索质量**，仅作为离线基线与测试替身。
3. **未接入真实 Embedding 模型**（规格明确要求不加需要下载大模型的依赖），
   `EmbeddingProvider` 协议已留出扩展点。
4. **摘要 JSON 解析不猜测**：只做「直接 parse → 剥 markdown 围栏」两步确定性规范化，
   其余一律走修复请求或抛错。
5. **本轮未做**（规格明确排除）：外部文档 RAG、文档摄取与切块、Browser/Computer Use、
   Planner/Reviewer/Router、A2A/ACP、新前端页面、新向量数据库服务、多用户权限、
   Redis/PostgreSQL/分布式任务、Skill Evolution。
6. **未 commit / 未 push**（按要求）。工作树保持未提交状态待复核。
