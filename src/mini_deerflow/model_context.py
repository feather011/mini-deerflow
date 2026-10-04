"""Phase 24 Token 弹性分配与单次模型输入。

分配规则（规格 4.6）：

1. **先扣不可删除内容**：system/safety 指令、当前用户输入、进行中的
   工具回合、必需工具 Schema —— 这些超限直接抛
   `ContextWindowExceededError`，绝不裁剪。
2. **弹性区块各带上限**：summary / task_notes / memory / skills 的额度
   由 `ContextPolicy.cap()` 按 `hard_input_limit` 的比例算出。
3. **回流**：弹性区块没用完的额度**全部**交给最近原始消息，
   不做固定预留——预留等于把 Token 白扔。
4. 分配顺序即优先级：summary → task_notes → memory → skills → recent。
   每一项都受 `min(需要量, 区块上限, 剩余额度)` 三重约束。
"""

from __future__ import annotations

from dataclasses import dataclass

from mini_deerflow.context_errors import ContextWindowExceededError
from mini_deerflow.context_state import ContextPolicy
from mini_deerflow.models import Message, ToolSpec
from mini_deerflow.token_counter import TokenCounter

__all__ = [
    "ContextTokenBreakdown",
    "ElasticTokenAllocator",
    "ModelContext",
]


@dataclass(frozen=True)
class ContextTokenBreakdown:
    """一次模型调用的各区域 Token 实测值。Trace 与调试都读它。"""

    system: int
    current_input: int
    active_tool_cycle: int
    recent_messages: int
    summary: int
    task_notes: int
    memory: int
    skills: int
    tool_schemas: int

    @property
    def required(self) -> int:
        """不可删除部分合计。"""
        return (
            self.system
            + self.current_input
            + self.active_tool_cycle
            + self.tool_schemas
        )

    @property
    def elastic(self) -> int:
        """弹性区块合计（可被裁剪/回流的部分）。"""
        return self.summary + self.task_notes + self.memory + self.skills

    @property
    def total(self) -> int:
        return self.required + self.elastic + self.recent_messages

    def to_dict(self) -> dict[str, int]:
        """给 Trace metadata 用的紧凑视图。"""
        return {
            "system": self.system,
            "current_input": self.current_input,
            "active_tool_cycle": self.active_tool_cycle,
            "recent_messages": self.recent_messages,
            "summary": self.summary,
            "task_notes": self.task_notes,
            "memory": self.memory,
            "skills": self.skills,
            "tool_schemas": self.tool_schemas,
            "total": self.total,
        }


@dataclass(frozen=True)
class ModelContext:
    """单次 `Model.decide()` 的临时输入。用完即弃，不持久化。"""

    messages: tuple[Message, ...]
    """按发送顺序排列的最终消息序列（含 system 段）。"""
    tool_specs: tuple[ToolSpec, ...]
    breakdown: ContextTokenBreakdown


class ElasticTokenAllocator:
    """按 ContextPolicy 计算各区域 Token 预算。"""

    __slots__ = ("_policy", "_counter")

    def __init__(self, policy: ContextPolicy, counter: TokenCounter) -> None:
        if not isinstance(policy, ContextPolicy):
            raise TypeError(f"policy 必须是 ContextPolicy，收到 {type(policy).__name__}")
        self._policy = policy
        self._counter = counter

    @property
    def policy(self) -> ContextPolicy:
        return self._policy

    @property
    def counter(self) -> TokenCounter:
        return self._counter

    def measure_required(
        self,
        *,
        system: str,
        current_input: str,
        active_tool_cycle: tuple[Message, ...] | list[Message],
        tool_specs: tuple[ToolSpec, ...] | list[ToolSpec],
    ) -> int:
        """只统计不可删除部分（压缩前的前置校验用）。"""
        total = self._counter.count_text(system)
        total += self._counter.count_text(current_input)
        total += self._counter.count_messages(active_tool_cycle)
        total += self._counter.count_tool_specs(list(tool_specs))
        return total

    def allocate(
        self,
        *,
        system_tokens: int,
        current_input_tokens: int,
        active_tool_cycle_tokens: int,
        tool_schema_tokens: int,
        summary_need: int,
        task_notes_need: int,
        memory_need: int,
        skills_need: int,
        recent_tokens: int,
    ) -> ContextTokenBreakdown:
        """执行弹性分配。

        Args:
            *_tokens: 各区域的**实测需要量**（非预算）。
            recent_tokens: 最近原始消息在未分配弹性额度时的完整 Token 数，
                回流后会被放大到剩余额度上限（由调用方决定是否截断）。

        Raises:
            ContextWindowExceededError: 不可删除部分自身就超过硬上限。
        """
        for name, value in (
            ("system_tokens", system_tokens),
            ("current_input_tokens", current_input_tokens),
            ("active_tool_cycle_tokens", active_tool_cycle_tokens),
            ("tool_schema_tokens", tool_schema_tokens),
            ("summary_need", summary_need),
            ("task_notes_need", task_notes_need),
            ("memory_need", memory_need),
            ("skills_need", skills_need),
            ("recent_tokens", recent_tokens),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{name} 必须是整数")
            if value < 0:
                raise ValueError(f"{name} 不能为负: {value}")

        required = (
            system_tokens
            + current_input_tokens
            + active_tool_cycle_tokens
            + tool_schema_tokens
        )
        hard_limit = self._policy.hard_input_limit
        if required > hard_limit:
            raise ContextWindowExceededError(
                required_tokens=required, hard_limit=hard_limit
            )

        remaining = hard_limit - required

        summary_tokens = min(summary_need, self._policy.cap("summary"), remaining)
        remaining -= summary_tokens
        notes_tokens = min(
            task_notes_need, self._policy.cap("task_notes"), remaining
        )
        remaining -= notes_tokens
        memory_tokens = min(memory_need, self._policy.cap("memory"), remaining)
        remaining -= memory_tokens
        skills_tokens = min(skills_need, self._policy.cap("skills"), remaining)
        remaining -= skills_tokens

        # 回流：弹性区块没用完的额度成为最近消息的**可用预算上限**。
        # 用 min(实际需要, 剩余额度) 而非「填满剩余额度」——回流放宽的是
        # 上限，不是凭空造 Token。填满会让 total 恒等于硬上限，
        # 软阈值判定随之失效（压不压缩全看错）。
        recent_tokens = min(recent_tokens, remaining)

        return ContextTokenBreakdown(
            system=system_tokens,
            current_input=current_input_tokens,
            active_tool_cycle=active_tool_cycle_tokens,
            recent_messages=recent_tokens,
            summary=summary_tokens,
            task_notes=notes_tokens,
            memory=memory_tokens,
            skills=skills_tokens,
            tool_schemas=tool_schema_tokens,
        )

    def should_compact(self, breakdown: ContextTokenBreakdown) -> bool:
        """是否达到软阈值、需要触发压缩。"""
        return breakdown.total >= self._policy.soft_compaction_limit

    def is_over_hard_limit(self, breakdown: ContextTokenBreakdown) -> bool:
        """是否已越过硬上限（压缩失败后的降级判断用）。"""
        return breakdown.total > self._policy.hard_input_limit
