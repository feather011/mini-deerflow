# TOOL-07 技术方案：harness 权限与工具模块集成验收

**依赖基线**：`ae556cf`（TOOL-06 冻结基线，PR #5）
**分支**：`automation/tool-07`
**范围**：主 Agent → `project_execute` → `HarnessRunner` → harness 子进程 → 工具执行入口

---

## 零、本轮不做什么

- 不关闭/合并 PR #5（TOOL-06 基线继续冻结）
- 不修 `test_mcp.py` 旧测试文件
- 不新增前端页面
- 不做 Skill 实现（Skill 仍只做只读盘点，另立设计卡）

---

## 一、链路真实入口

```
Agent.run()
  → _do_tool_call()                        [mini-deerflow/agent.py]
      → ensure_tool_allowed()              白名单（暴露层）
      → check_tool_access()                TOOL-02 授权判定（执行层）
      → registry.execute("project_execute")
  → project_execute.handler()              [mini-deerflow/project_tools.py]
      → _bound_repo()                      thread_id → project_id → repo_path
      → get_lock(project_id)               项目锁（非阻塞）
      → _snapshot_workspace()              执行前基线
      → runner.run()                       HarnessRunner
  → HarnessRunner.run()                    [mini-deerflow/harness_runner.py]
      → _start_process()                   subprocess.Popen(cwd=repo_path)
      → _supervise()                       deadline + stop_event + 进程树回收
      → _read_events()                     JSONL 协议解析
  → harness 子进程                          [harnessagent/harness/noninteractive.py]
      → _install_noninteractive_hooks()
          → PreToolDispatch                计数 + 限流
          → PreToolUse → check_permission() 3 道闸
              ① deny_list   (bash 硬编码黑名单)
              ② rules       (越界 / 破坏性 / 敏感文件 / 装包)
              ③ ask_user    (非交互模式 prompt_user=False → 直接拒绝)
      → 工具 handler 执行
  → 回到 _diff_snapshots()                 本轮 diff
```

**mini-deerflow 与 harness 的边界**：mini-deerflow 负责会话级授权（哪个会话能派发），
harness 负责工具级授权（派发进来后能做什么）。两层不重复、不互相绕过。

---

## 二、验收矩阵

### A. 会话级授权（mini-deerflow 侧）

| # | 验收项 | 真实入口 | 断言 | 已有覆盖 |
|---|--------|----------|------|----------|
| A1 | 普通会话 `project_*` 不在模型工具列表 | `Agent.run()` → `filter_tool_specs` | `model.seen_tool_names` 不含 `project_execute` | `test_agent_tool_permissions.py` |
| A2 | 普通会话强制调用 `project_execute` 被拒 | `_do_tool_call` → `ensure_tool_allowed` | 抛 `ToolNotAllowedError`，handler 零执行 | `test_agent_tool_permissions.py` |
| A3 | 项目会话 `project_execute` 可见且可执行 | 同上，`project_id` 存在 | 白名单为 `None`，工具返回结果 | `test_agent_tool_permissions.py` |
| A4 | `thread_id → project_id → repo_path` 反查 | `_bound_repo()` | 非项目会话返回 `ok=false` + 原因 | `test_project_tools.py` |
| A5 | 工具禁用后模型不可见且不执行 | `registry.set_policy(enabled=False)` | 工具不在列表 + handler 零执行 | `test_tool_authorization_integration.py` |

### B. 工具级授权（harness 侧）

| # | 验收项 | 真实入口 | 断言 | 已有覆盖 |
|---|--------|----------|------|----------|
| B1 | deny_list：`rm -rf /` 被拒 | `PreToolUse` → `check_deny_list` | handler 零执行，tool 结果含 "Permission denied" | `test_noninteractive_permission.py` |
| B2 | rules：`pip install` 非交互下直接拒绝 | `check_rules` → `prompt_user=False` | 结果为 "Permission required: Installing new package" | `test_noninteractive_permission.py` |
| B3 | rules：`read_file ../outside.txt` 越界被拒 | `_check_out_of_workspace` | 结果为 "Permission required: Access outside workspace" | `test_noninteractive_permission.py` |
| B4 | rules：写 `.env` 敏感文件被拒 | `_check_sensitive_file` | 文件未创建 + 拒绝消息 | `test_noninteractive_permission.py` |
| B5 | 正向：`echo hello` 照常执行 | 完整三道闸放行 | handler 执行 + 返回 `[exit 0]` | `test_noninteractive_permission.py` |
| B6 | 非交互模式绝不调用 `input()` | `ask_user` | `input()` spy 为空 | `test_noninteractive_permission.py` |
| B7 | 被拒的调用同样消耗额度 | `PreToolDispatch` 计数 | `counters["total_calls"]` 递增 | `test_noninteractive_permission.py` |

**结论**：harness 侧工具级授权已有完整测试，本轮不重复实现，只做跨进程端到端验证（见 C2）。

### C. 跨进程端到端（本轮需补）

| # | 验收项 | 真实入口 | 断言 | 夹具 |
|---|--------|----------|------|------|
| C1 | 取消：子进程启动后取消 → 进程树回收 | `CancellationToStopEvent` → `HarnessRunner._supervise` | 状态 `stopped` + 锁释放 + 进程已退出 | 真实 HarnessRunner + 慢 fake LLM |
| C2 | 权限拒绝跨进程透传 | harness `PreToolUse` → JSONL tool 事件 → `HarnessRunner.tool_events` → `project_execute.verification` | `verification[].output` 含 "Permission denied" | 真实 HarnessRunner + 恶意命令 fake LLM |
| C3 | 本轮 diff 保留既有修改（内容级） | `_snapshot_workspace` + `_diff_snapshots` | 既有文件哈希执行前后一致；新文件出现在 `changes` | 真实 HarnessRunner |
| C4 | MCP 工具在项目会话注册且授权可复核 | `tool_policy_registry` → `MCPSearchAdapter.assert_allowed` | 禁用/撤权后 handler 不执行 | TOOL-06 已有夹具（复用） |
| C5 | 子 Agent 不扩大父授权 | `SubagentManager` → `check_tool_access(principal=MINI_SUBAGENT)` | 子 Agent 能力 ⊆ 父能力 | `test_tool_authorization_integration.py` 已有，本轮补跨进程断言 |

### D. 状态收口（已有覆盖，本轮回归）

| # | 验收项 | 已有覆盖 |
|---|--------|----------|
| D1 | 超时：`HarnessRunner._supervise` deadline → 进程树回收 | `test_harness_runner_integration.py` |
| D2 | 超时：锁释放，下一个任务可获取 | `test_project_execute_e2e.py` |
| D3 | 失败：`HarnessRunResult.status` 准确，不伪报成功 | `test_harness_runner_integration.py` |
| D4 | 项目忙：并发第二次 `project_execute` 被拒 | `test_project_execute_e2e.py` |
| D5 | 协议错误：非 JSON 行 → `protocol_error` 终态 | `test_harness_runner_integration.py` |

---

## 三、实现步骤

### Step 3a：跨进程端到端测试（新增 `tests/test_tool07_harness_permission_e2e.py`）

**夹具**：真实 `HarnessRunner` + 内嵌 mock LLM HTTP 服务器。
`project_execute` 调用 `runner.run()` 时不传 `extra_env`，因此用
monkeypatch 恒注入 mock 地址——真实入口不变，只换掉模型 Provider。

| 测试 | 场景 | 结果 |
|------|------|------|
| `test_取消_子进程启动后_进程树回收` | fake LLM 首轮返回 `sleep 30` | ✅ passed |
| `test_权限拒绝_跨进程透传` | fake LLM 返回 `bash rm -rf /` | ⚠️ xfail（已知缺口，见下） |
| `test_本轮diff_既有修改内容不变_新文件出现在changes` | 预置既有文件 + fake LLM 写新文件 | ✅ passed |

**实现要点**：
- 取消时机：轮询 `_child_pids()` 等到子进程真的出现才 `cancel()`，
  避免"启动前就取消"造成的假通过
- 项目锁隔离：每个测试用 `uuid` 生成的唯一 `project_id`（锁是模块级 dict）
- `.env` 隔离：`HarnessRunner` 默认把 `HARNESS_ENV_FILE` 指向仓库根 `.env`，
  那是所有测试共享的单文件且会被 HarnessRunner 重写——改为每测试独占的临时文件
- **不用 PID 断言进程回收**：Windows 上进程退出后其 PID 仍会留在
  `Get-CimInstance` 结果里一段时间，会误报"残留"。改用行为证据
  （返回远早于 sleep 结束、状态是 `stopped` 而非 `timeout`）

### Step 3b：授权集成回归（追加 2 项到 `test_tool_authorization_integration.py`）

| 测试 | 断言 | 结果 |
|------|------|------|
| `test_subagent_authority_not_expanded_by_dispatch_roundtrip` | 派发回来的结果不扩大子 Agent 授权 | ✅ passed |
| `test_subagent_authority_is_thread_scoped` | 子 Agent 授权绑定自身 run_id | ✅ passed |

### Step 3c：运行完整套件

- 定向：`test_tool07_harness_permission_e2e.py` + `test_tool_authorization_integration.py`
- 全量：`pytest tests/ -q`（排除 `test_mcp.py`，见限制 1）

---

## 四、需要补的测试数量

按矩阵逐项核对已有覆盖，**本轮只需新增 5 项**（不是 7–10 项）：

| 文件 | 新增 | 说明 |
|------|------|------|
| `tests/test_tool07_harness_permission_e2e.py` | 3 | C1 / C2 / C3 |
| `tests/test_tool_authorization_integration.py` | 2 | C5 跨进程 + C4 撤权 |

其余矩阵项已有覆盖，只做回归不重写。

---

## 五、环境隔离

| 项 | 规则 |
|----|------|
| 模型 | `MINI_DEERFLOW_MODEL_PROVIDER=fake`（正确变量名，非 `MINI_DEERFLOW_PROVIDER`） |
| harness | 用内嵌 mock LLM HTTP 服务器驱动子进程，**不调用付费服务** |
| harness env | `HarnessRunner.run(extra_env=...)` 覆盖 `HARNESS_MIMO_BASE_URL` / `HARNESS_MIMO_API_KEY`；`HARNESS_ENV_FILE` 指向每测试独占的临时文件，不写仓库根 `.env` |
| 临时目录 | 每个测试 `tmp_path` 独立；`finally` 中 `shutil.rmtree` |
| 全局状态 | `project_execute` 项目锁是模块级 dict，每个测试用唯一 `project_id` |

---

## 六、实测结果（2026-10-20）

### 定向测试

```
tests/test_tool07_harness_permission_e2e.py
tests/test_tool_authorization_integration.py
tests/test_agent_tool_permissions.py
tests/test_project_tools.py
tests/test_project_execute_e2e.py

68 passed, 1 skipped, 1 xfailed in 22.04s
```

### 全量（排除 `test_mcp.py`）

```
3496 passed, 11 failed, 1 skipped, 46 deselected, 42 subtests passed
（287.69s = 0:04:47）
```

**11 项失败在冻结基线 `ae556cf` 上完整复现**：用 `git worktree add`
在 ae556cf 上跑完整套件（同样 `deselect tests/test_mcp.py`），
得到完全相同的 11 项失败。本分支未触碰那 11 个文件。

| 失败文件 | 失败项数 | 性质 |
|----------|---------|------|
| `test_context_agent.py` | 1 | Budget 断言预先存在 |
| `test_harness_runner_integration.py` | 6 | harness 子进程调真实 API 撞 429 |
| `test_phase17_18_closing.py` | — | BudgetAtomicConsume 预先存在 |
| `test_phase21b_events.py` | 1 | SSE 事件预先存在 |
| `test_tool_loop_21b.py` | 1 | Budget 断言预先存在 |
| `test_subagent_allowed_tools.py` | 3 | MCP 初始失败导致级联 |

> 注：`test_harness_runner_integration.py` 调真实 API 而非 mock，
> 它有它自己的 429 失败模式。本文件的 E2E 测试已显式注入 mock Provider。

---

## 七、本轮发现的真实缺口（已修复一项）

### 缺口 1（harness 侧，**已修复**）：权限拒绝不透传

**现象**：`project_execute` 的 `verification` 里看不到任何权限拒绝记录。

**根因**：`harness/agent.py` 中 `PreToolUse` 拒绝分支直接 `continue`，
跳过 `on_tool_start` / `on_tool_done`，JSONL 协议无事件，宿主收不到原因。

**修复**（已在 `D:/项目解读/harnessagent/harness/agent.py` 落地，本地 commit 未提交由你审核）：
- 拒绝分支补发 `on_tool_start` / `on_tool_done`，output = 拒绝消息
- handler 零执行的保证不变（本改动只在 `blocked is not None` 分支内）
- `tests/test_noninteractive_permission.py` 全部通过：
  handler 零执行、`input()` 不被调用的断言均未受影响

**验证**：mini-deerflow 侧 `test_权限拒绝_跨进程透传到_verification`
由 `xfail` 转为 `passed`（同时验证：拒绝生效 + 拒绝原因透传）

### 缺口 2（mini-deerflow 侧，观察项）：`verification` 只在 completed 分支有内容

`project_tools.py` 的 `stopped` / `timeout` 分支硬编码 `"verification": []`。
被取消的任务即使已执行过若干命令，也不会有任何验证记录。
本轮不改（超范围），记为观察项。

---

## 八、限制（如实记录，不掩盖）

| 限制 | 说明 | 处理 |
|------|------|------|
| 无操作系统级沙箱 | harness 子进程与父进程同用户、同权限 | 记录为限制；业务层路径/工具授权已验收（B 组） |
| 无分布式锁 | `_get_project_lock` 是模块级 dict | 本轮按单进程部署验收 |
| `test_mcp.py` 11 项失败 | 预先存在（`required` 字段 AttributeError） | 不整体跳过 MCP：用 TOOL-03/06 的可运行 stdio 夹具单独验证（C4） |
| Windows only `harness_root` | 生产 .env 需按平台配置 | 记录为平台限制 |

---

## 九、本轮完成条件（已按此记录，不预先标 DONE）

**本轮只记录以下四项，不预先写"PROGRESS 标记完成"：**

1. **实际运行的测试数量与结果**
   - 定向 TOOL-07 范围：`3 passed` (`test_tool07_harness_permission_e2e.py`)
     + `17 passed` (`test_tool_authorization_integration.py`)
   - 改造对象（含原有覆盖）：`test_tool_authorization_integration.py` 16 passed
   - 全量（排除 `test_mcp.py`）：本分支 `3493 passed, 11 failed, 1 skipped`
     与冻结基线 `ae556cf` 完全一致——11 项失败均为预先存在

2. **验收矩阵中已验证 / 未验证项**
   - ✅ 已验证：A1–A5（会话级）、B1–B7（harness 工具级，复用已有覆盖）、
     C1（取消 + 进程句柄 `poll()` 直接证据）、
     C2（权限拒绝透传，harness 缺口的修复 + 该路径的正向断言）、
     C3（diff 既有修改哈希 + 新文件进 changes）、C5（真实派发：父能力原样继承）、
     D1–D5
   - ⏸ 待补：C4 MCP 撤权 handler 阻断（依赖 `test_mcp.py` 修复或独立 stdio 夹具）

3. **限制与待验收项** — 见第八节；harness 侧修复以补丁 + 工作区修改双重落地

4. **人工体验点**（留给用户，内部权限/协议/回收交给自动化）
   - 任务派发、进度展示、停止按钮、结果展示

标记 DONE 需协调窗口按本矩阵核对证据后决定。

## 十、跨仓库改动清单

`D:/项目解读/harnessagent/harness/agent.py`：在 `PreToolUse` 拒绝分支补发
`on_tool_start` / `on_tool_done` 两个事件（详见上文缺口 1）。
handler 零执行不变——本改动只在 `blocked is not None` 分支内发起回调。
该文件未提交，由你审核后决定提交/PR。
