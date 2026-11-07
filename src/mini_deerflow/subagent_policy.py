"""Subagent 权限上限与交集计算（子 Agent 首版）。

## 为什么单独成模块

子 Agent 的有效权限是**三个来源的交集**，任何一处都不能单独决定：

    有效权限 = 父任务实际权限 ∩ 服务端只读上限 ∩ 子执行主体策略

- **父任务实际权限**：父 Agent 本次运行真正持有的能力（`granted_capabilities`）
  与真正可见的工具（父的 allow list）。
- **服务端只读上限**：本模块定义的常量。**模型无法影响它**——
  `subagent_submit` 的参数 schema 里根本没有 `allowed_tools` 字段。
- **子执行主体策略**：`ToolPolicyRegistry` 里 `MINI_SUBAGENT` 主体的策略。

## 三条硬边界

1. **上限只做减法**：`capabilities` 与 `tools` 都是「父 ∩ 上限」，
   绝不可能因为存在上限常量就给子 Agent 授���父任务没有的权限。
2. **不可递归**：子 Agent 的能力集里去掉 `SUBAGENT_DELEGATE`，
   工具集里去掉全部 `subagent_*`。
3. **项目绑定不可由模型更换**：`project_id` 只能由父线程反查继承
   （见 `subagent_tools._SubmitHandler`），模型传入的任何值一律忽略。

## 为什么上限不含 project_execute

编码执行由 harnessagent 独占（`project_execute`）。若子 Agent 能拿到它，
就会出现第二条编码链路，绕过 harness 的交接契约与 Guard 判定。
因此 `project_execute` 不在上限内 —— 子 Agent 只能做**只读**工作。
"""

from __future__ import annotations

from typing import Iterable

from mini_deerflow.tool_contracts import ToolCapability


# ---------------------------------------------------------------------------
# 服务端固定的只读上限
# ---------------------------------------------------------------------------

#: 子 Agent 允许的**工具名**上限（服务端固定，模型不可改）。
#:
#: 只包含只读 / 纯计算工具。刻意**不含**：
#: - `project_execute`：编码执行由 harnessagent 独占
#: - `sandbox_*`：本地命令执行
#: - `subagent_*`：不可递归派发
#: - `mcp__*`：外部工具的能力与副作用不可预知
SUBAGENT_TOOL_CEILING: frozenset[str] = frozenset({
    "calculator",
    "history_search",
    "history_read",
    "task_note",
    "project_read_file",
    "project_glob",
    "project_grep",
})


#: 子 Agent 允许的**能力**上限（服务端固定，模型不可改）。
#:
#: 与工具上限一一对应：这些工具各自需要的能力。
#: 不含 `SUBAGENT_DELEGATE`（不可递归），不含 `PROJECT_EXEC_DELEGATE`
#: （编码执行由 harnessagent 独占），不含 `COMMAND_EXECUTE*`
#: （本地命令执行），不含 `PROJECT_MODIFY`（直接改文件）。
SUBAGENT_CAPABILITY_CEILING: frozenset[ToolCapability] = frozenset({
    ToolCapability.COMPUTE,
    ToolCapability.SESSION_HISTORY_READ,
    ToolCapability.SESSION_STATE_WRITE,
    ToolCapability.PROJECT_READ,
})


#: 绝不允许出现在子 Agent 工具集里的工具名前缀 / 精确名。
#:
#: 作为**兜底**的第二道过滤：即便上层传入的工具集被污染，
#: 这里仍会把这些工具剔除。用于防「上层漏了一道过滤」类缺陷。
FORBIDDEN_SUBAGENT_TOOLS: frozenset[str] = frozenset({
    "project_execute",
    "sandbox_read_file",
    "sandbox_write_file",
    "sandbox_run_command",
    "subagent_submit",
    "subagent_status",
    "subagent_collect",
    "subagent_cancel",
})


def _is_forbidden_tool(name: str) -> bool:
    """工具是否在禁列里（精确名或 `mcp__` 前缀）。"""
    return name in FORBIDDEN_SUBAGENT_TOOLS or name.startswith("mcp__")


def compute_child_capabilities(
    parent_capabilities: Iterable[ToolCapability],
) -> frozenset[ToolCapability]:
    """计算子 Agent 的有效能力集。

    ``parent ∩ 服务端上限`` —— 上限只做减法。

    Args:
        parent_capabilities: 父任务本次运行实际持有的能力。

    Returns:
        交集后的能力集。必然不含 ``SUBAGENT_DELEGATE``。
    """
    parent = frozenset(parent_capabilities)
    return frozenset(parent & SUBAGENT_CAPABILITY_CEILING)


def compute_child_tools(
    parent_tools: Iterable[str],
) -> tuple[str, ...]:
    """计算子 Agent 的有效工具集。

    ``parent ∩ 服务端上限``，再额外剔除禁列工具。

    Args:
        parent_tools: 父任务本次运行真正可见的工具名。

    Returns:
        排序后的工具名元组。空元组表示子 Agent 看不到任何工具
        （此时子 Agent 只能做纯推理，不能调工具）。
    """
    parent = frozenset(parent_tools)
    allowed = parent & SUBAGENT_TOOL_CEILING
    allowed = frozenset(n for n in allowed if not _is_forbidden_tool(n))
    return tuple(sorted(allowed))


def describe_ceiling() -> dict[str, object]:
    """返回上限的可读描述（供 API / 前端展示与排查）。

    只暴露工具名与能力名 —— 都是代码内的固定字面量，不含任何用户数据。
    """
    return {
        "tools": sorted(SUBAGENT_TOOL_CEILING),
        "capabilities": sorted(c.name for c in SUBAGENT_CAPABILITY_CEILING),
        "forbidden_tools": sorted(FORBIDDEN_SUBAGENT_TOOLS),
    }
