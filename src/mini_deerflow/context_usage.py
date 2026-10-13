"""最近一次「实际送给模型的输入」快照。

唯一权威来源：`ContextAssembler.assemble()` 返回的 `AssembledContext`。
不重复实现估算——所有 Token 数都直接搬运 `ContextTokenBreakdown`，
**分母**沿用 `ContextPolicy.hard_input_limit`（即「输入预算」：
已扣除 reserved_output_tokens 与 safety_margin_tokens）。

## 与 Trace 事件的边界

`context.assembled` Trace 事件已携带 `breakdown.to_dict()` 等同名字段；
本快照专门给前端的「上下文用量指示器」用，与 Trace 不冲突也不重复——
Trace 是按 run_id / 时间排序的事件流，本快照是「最近一次发送的视图」。

## 字段含义

- `budget` —— 后端可用输入预算（hard_input_limit），前端做分母并显示。
- `total` —— 分配+裁剪**之后**实际送给模型的 Token；UI 上是「当前用量」，
  占用率 = `total / budget`。
- `tokens_before` —— 仅当本轮发生过压缩时有值，表示压缩前 `breakdown.total`；
  与 `total` 并列即「压缩前 / 压缩后」对照。
- `breakdown` —— 9 个分区之和恒等于 `total`，无重复计数。
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from mini_deerflow.context_assembler import AssembledContext
from mini_deerflow.context_state import ContextPolicy
from mini_deerflow.model_context import ContextTokenBreakdown

__all__ = ["ContextUsageSnapshot"]


@dataclass(frozen=True)
class ContextUsageSnapshot:
    """最近一次发送的上下文用量快照。"""

    thread_id: str
    """所属 Thread。"""

    run_id: str | None
    """关联的 Run ID（若有；纯诊断用途，前端不展示）。"""

    budget: int
    """后端可用输入预算（= ContextPolicy.hard_input_limit）。"""

    total: int
    """实际送给模型的 Token（= breakdown.total）。"""

    breakdown: dict[str, int]
    """9 个分区的 Token 数，键名与 `ContextTokenBreakdown.to_dict()` 一致。"""

    compacted: bool
    """本轮是否触发过压缩。"""

    tokens_before: int | None
    """压缩前的 `breakdown.total`，仅 `compacted=True` 时有值。"""

    soft_limit: int
    """触发压缩的软阈值（= ContextPolicy.soft_compaction_limit）。"""

    captured_at: float = field(default_factory=time.time)
    """捕获时间戳（Unix 秒）；前端据此排序或显示「最近一次」。"""

    @classmethod
    def from_assembled(
        cls,
        *,
        thread_id: str,
        policy: ContextPolicy,
        assembled: AssembledContext,
        run_id: str | None = None,
    ) -> "ContextUsageSnapshot":
        """从 `AssembledContext` 构造快照。

        必须传入 `policy`，因为「输入预算」属于策略而非组装结果。
        `run_id` 由调用方提供（不在 `AssembledContext` 内）。
        """
        breakdown_dict = _breakdown_to_dict(assembled.context.breakdown)
        tokens_before: int | None = None
        if assembled.compacted and assembled.compaction_result is not None:
            tokens_before = assembled.compaction_result.tokens_before
        return cls(
            thread_id=thread_id,
            run_id=run_id,
            budget=policy.hard_input_limit,
            total=assembled.context.breakdown.total,
            breakdown=breakdown_dict,
            compacted=assembled.compacted,
            tokens_before=tokens_before,
            soft_limit=policy.soft_compaction_limit,
        )

    def to_dict(self) -> dict[str, Any]:
        """前端 / Trace 通用 dict 视图。"""
        return asdict(self)


def _breakdown_to_dict(breakdown: ContextTokenBreakdown) -> dict[str, int]:
    """9 分区 → dict。和 `ContextTokenBreakdown.to_dict()` 同键名。"""
    return {
        "system": breakdown.system,
        "current_input": breakdown.current_input,
        "active_tool_cycle": breakdown.active_tool_cycle,
        "recent_messages": breakdown.recent_messages,
        "summary": breakdown.summary,
        "task_notes": breakdown.task_notes,
        "memory": breakdown.memory,
        "skills": breakdown.skills,
        "tool_schemas": breakdown.tool_schemas,
    }
