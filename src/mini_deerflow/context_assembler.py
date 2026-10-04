"""Phase 24 ContextAssembler：每次 `Model.decide()` 前的**唯一**组装入口。

为什么必须唯一：Agent、CLI、FastAPI、Subagent 只要各写一份上下文拼装逻辑，
「摘要有没有进上下文」「Memory 是否重复注入」就会在四条路径上表现不一致，
而且没有单一位置可以做 Token 核算与压缩触发。

## 输出顺序

```
[system] summary          —— 被压缩的历史投影（有则注入）
[system] task_notes       —— active Task Notes（有则注入）
[system] memory_context    —— Memory 上下文（调用方提供）
[system] skill_context     —— Skill 上下文（调用方提供）
[...]   recent_messages   —— ThreadState 中未被摘要覆盖的原始消息
[...]   pending           —— 当前用户输入 + 本轮工具往返
```

Memory / Skill 仍由调用方各调一次 `build()`（沿用现有注入点，不重复实现），
本模块只负责**装配与计量**，不重复实现它们的构建逻辑。

## 压缩失败的降级策略（规格 4.8）

```
达软阈值 → 压缩 → 失败
  ├─ 原上下文仍低于硬上限 → 记 context.compaction.failed，本轮用原上下文继续
  └─ 已达 / 超硬上限      → 抛 ContextWindowExceededError
```

绝不静默删除历史来「让它能跑下去」——那会让 Agent 带着缺失的上下文
给出自信但错误的回答，比直接失败危险得多。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from mini_deerflow.context_shared import emit_event
from mini_deerflow.context_compactor import CompactionResult, ContextCompactor
from mini_deerflow.context_errors import ContextWindowExceededError
from mini_deerflow.context_state import ContextPolicy, ContextState
from mini_deerflow.context_store import ContextStore, cas_update
from mini_deerflow.message_group import MessageGroup, MessageGroupBuilder
from mini_deerflow.middleware import TraceSink
from mini_deerflow.model_context import (
    ContextTokenBreakdown,
    ElasticTokenAllocator,
    ModelContext,
)
from mini_deerflow.models import Message, ToolSpec
from mini_deerflow.thread_state import ThreadState
from mini_deerflow.token_counter import TokenCounter

__all__ = ["AssembledContext", "ContextAssembler"]

# 一次压缩覆盖的组数比例：每次吃掉最旧的一半组，
# 既避免一次压太多丢细节，也避免只压一条导致压不完。
COMPACT_TARGET_RATIO = 0.5

SUMMARY_HEADER = "【已压缩的早期对话摘要】"
TASK_NOTES_HEADER = "【当前任务笔记（请持续维护，不要重复询问）】"


@dataclass(frozen=True)
class AssembledContext:
    """组装结果：给模型的输入 + 本轮的状态变更与可观测数据。"""

    context: ModelContext
    state: ContextState
    """本轮结束时的 ContextState（可能因压缩或补索引而变化）。"""
    compacted: bool
    compaction_error: str | None
    """压缩失败时的错误类型名；None 表示未失败。"""
    compaction_result: CompactionResult | None


def render_summary_block(state: ContextState) -> str | None:
    """把摘要渲染成注入上下文的一段 system 文本。无摘要返回 None。"""
    summary = state.summary
    if summary is None:
        return None
    parts: list[str] = [SUMMARY_HEADER, summary.narrative]
    sections = (
        ("已确认事实", summary.facts),
        ("必须遵守的约束", summary.constraints),
        ("已做出的决策", summary.decisions),
        ("试过但失败的方案", summary.failed_attempts),
        ("待办与未决事项", summary.open_items),
    )
    for title, items in sections:
        if not items:
            continue
        parts.append(f"{title}：")
        parts.extend(f"- {item}" for item in items)
    parts.append(
        f"（覆盖消息序号 {summary.summarized_from}..{summary.summarized_through}；"
        "原始消息仍完整保留在会话记录中，可用 history_search / history_read 找回）"
    )
    return "\n".join(parts)


def render_task_notes_block(state: ContextState) -> str | None:
    """把 **active** Task Notes 渲染成一段 system 文本。无 active 笔记返回 None。"""
    active = state.active_notes
    if not active:
        return None
    parts: list[str] = [TASK_NOTES_HEADER]
    for note in active:
        parts.append(f"- [{note.category.value}] {note.content}")
    return "\n".join(parts)


class ContextAssembler:
    """组装、计量、按需压缩。Agent / CLI / API / Subagent 共用这一个入口。"""

    def __init__(
        self,
        *,
        policy: ContextPolicy,
        counter: TokenCounter,
        context_store: ContextStore,
        compactor: ContextCompactor | None = None,
        trace_sink: TraceSink | None = None,
        group_builder: MessageGroupBuilder | None = None,
        history_service: "HistoryIndexService | None" = None,
    ) -> None:
        self._policy = policy
        self._counter = counter
        self._context_store = context_store
        self._compactor = compactor
        self._trace_sink = trace_sink
        self._groups = group_builder or MessageGroupBuilder()
        self._allocator = ElasticTokenAllocator(policy, counter)
        # 可选：仅用于线程销毁时同步清理投影（向量索引）。
        # 组装路径本身不碰检索，避免把两个职责搅在一起。
        self._history_service = history_service

    @property
    def context_store(self) -> ContextStore:
        """底层 ContextState 存储。供线程销毁时同步删除投影使用。"""
        return self._context_store

    @property
    def history_service(self) -> "HistoryIndexService | None":
        """历史检索服务；未接入检索时为 None。"""
        return self._history_service

    def drop_thread_projections(self, thread_id: str) -> None:
        """删除某个线程的全部**投影**状态（ContextState + 向量索引）。

        事实源 ThreadState 不在此处删除——那归 ThreadStore 管。
        本方法幂等，线程不存在时静默返回。
        """
        self._context_store.delete(thread_id)
        if self._history_service is not None:
            self._history_service.index.drop(thread_id)

    @property
    def policy(self) -> ContextPolicy:
        return self._policy

    @property
    def allocator(self) -> ElasticTokenAllocator:
        return self._allocator

    # ------------------------------------------------------------------
    # 状态获取
    # ------------------------------------------------------------------

    def ensure_state(self, thread_id: str) -> ContextState:
        """读取 ContextState；不存在则初始化（新线程 / Subagent）。"""
        if self._context_store.exists(thread_id):
            return self._context_store.get(thread_id)
        try:
            self._context_store.create(thread_id)
        except Exception:  # 并发创建冲突 → 改走读取
            pass
        return self._context_store.get(thread_id)

    # ------------------------------------------------------------------
    # 组装
    # ------------------------------------------------------------------

    def assemble(
        self,
        *,
        thread_id: str,
        thread: ThreadState,
        pending: tuple[Message, ...] | list[Message],
        memory_context: str = "",
        skill_context: str = "",
        tool_specs: tuple[ToolSpec, ...] | list[ToolSpec] = (),
        run_id: str = "",
        state: ContextState | None = None,
    ) -> AssembledContext:
        """组装一轮模型输入；达到软阈值时先压缩再组装。

        Args:
            thread_id: 当前线程（也用于定位 ContextState）。
            thread: 事实源，**只读**。
            pending: 本轮新增消息（当前用户输入 + 工具往返）。
            memory_context / skill_context: 调用方已构建好的上下文文本。
            tool_specs: 本轮允许的工具描述。
            run_id: 用于 Trace 关联。
            state: 已持有的 ContextState；不传则自行读取。

        Raises:
            ContextWindowExceededError: 必要内容超硬上限，或压缩失败后
                原上下文也已越过硬上限。
        """
        current_state = state if state is not None else self.ensure_state(thread_id)
        pending_tuple = tuple(pending)
        started = time.monotonic()

        composed, breakdown, raw_total = self._compose(
            thread=thread,
            state=current_state,
            pending=pending_tuple,
            memory_context=memory_context,
            skill_context=skill_context,
            tool_specs=tuple(tool_specs),
        )

        emit_event(
            self._trace_sink,
            name="context.assembled",
            run_id=run_id,
            thread_id=thread_id,
            operation="MODEL_CALL",
            status="success",
            metadata={
                **breakdown.to_dict(),
                # 分配与裁剪**之前**的原始估算，软阈值判断的依据
                "raw_total": raw_total,
                "soft_limit": self._policy.soft_compaction_limit,
                "hard_limit": self._policy.hard_input_limit,
                "summarized_through": current_state.summarized_through,
                "indexed_through": current_state.indexed_through,
                "active_notes": len(current_state.active_notes),
                "pending_count": len(pending_tuple),
                "message_count": len(composed.messages),
            },
            start=started,
        )

        # 软阈值判断**必须用 raw_total**：
        # breakdown.total 是按额度分配、且内容已被裁剪之后的值。
        # 拿它当依据，超长的 Memory / Skill / 历史会先被砍掉，再「发现」
        # 没超阈值，于是绕过压缩——被裁掉的那段既没进摘要，也没了原文。
        need_compaction = (
            self._compactor is not None
            and raw_total >= self._policy.soft_compaction_limit
        )
        if not need_compaction:
            return AssembledContext(
                context=composed,
                state=current_state,
                compacted=False,
                compaction_error=None,
                compaction_result=None,
            )

        # ---- 达到软阈值：压缩最旧的安全组，然后重新组装 ----
        try:
            result = self._compact(
                thread=thread,
                state=current_state,
                run_id=run_id,
                tokens_before=breakdown.total,
            )
        except Exception as exc:
            return self._handle_compaction_failure(
                composed=composed,
                breakdown=breakdown,
                state=current_state,
                error=exc,
                run_id=run_id,
                thread_id=thread_id,
                pending=pending_tuple,
                memory_context=memory_context,
                skill_context=skill_context,
                tool_specs=tuple(tool_specs),
                started=started,
            )

        saved = cas_update(self._context_store, thread_id, lambda _: result.state)
        # 规格 4.8 第 7 步「重新组装后再次校验硬上限」由 `_compose` 末尾的
        # `_enforce_hard_limit` 完成——它数的是真实 messages，比看 breakdown 可靠。
        recomposed, _rebreakdown, _raw_after = self._compose(
            thread=thread,
            state=saved,
            pending=pending_tuple,
            memory_context=memory_context,
            skill_context=skill_context,
            tool_specs=tuple(tool_specs),
        )
        return AssembledContext(
            context=recomposed,
            state=saved,
            compacted=True,
            compaction_error=None,
            compaction_result=result,
        )

    # ------------------------------------------------------------------
    # 内部：真正拼消息
    # ------------------------------------------------------------------

    def _clip_to_tokens(self, text: str, budget: int) -> str:
        """把文本裁到不超过 `budget` 个 Token。

        `ElasticTokenAllocator` 只负责**给额度**，它并不知道内容有多长；
        若不裁，长 Memory 会被算进 required 直接抛
        `ContextWindowExceededError`，或者被 cap 后仍按全文发送——
        两种都违反「Memory / Skill 是弹性区块」这条规格。
        """
        if not text:
            return ""
        if budget <= 0:
            return ""
        if self._counter.count_text(text) <= budget:
            return text
        tokens = self._counter.count_text(text)
        clipped = text[: max(1, int(len(text) * budget / tokens))]
        # 逐轮收紧：按比例粗截在 ASCII/CJK 混排时会偏乐观
        while clipped and self._counter.count_text(clipped) > budget and len(clipped) > 1:
            clipped = clipped[: max(1, int(len(clipped) * 0.9))]
        return clipped if clipped and self._counter.count_text(clipped) <= budget else ""

    def _trim_recent_to_budget(
        self, messages: tuple[Message, ...], budget: int
    ) -> tuple[Message, ...]:
        """按 Token 预算裁剪近期历史，**边界始终吸附到组起点**。

        从最后一组往前累加，一旦超预算就停——绝不会从工具链中间切开。
        预算为 0 时返回空（近期历史是可裁的，当前输入与工具回合不在此列）。
        """
        if not messages:
            return ()
        if budget <= 0:
            return ()
        if self._counter.count_messages(messages) <= budget:
            return messages
        groups = self._groups.build(messages, strict=False)
        keep: list[Message] = []
        for group in reversed(groups):
            candidate = tuple(group.messages) + tuple(keep)
            if self._counter.count_messages(candidate) <= budget:
                keep = list(candidate)
            else:
                break
        return tuple(keep)

    def _enforce_hard_limit(self, context: ModelContext) -> None:
        """对**最终要发出的 ModelContext** 数真实 Token，硬上限在最后一道生效。

        不看 `breakdown`：分配值只是「打算发多少」，实际内容一旦因裁剪逻辑
        出纰漏，靠 breakdown 自查永远查不出来。这里直接数 messages + 工具
        Schema，把 `hard_input_limit` 变成对真实产物的保证。
        """
        actual = self._counter.count_messages(context.messages) + (
            self._counter.count_tool_specs(list(context.tool_specs))
        )
        if actual > self._policy.hard_input_limit:
            raise ContextWindowExceededError(
                required_tokens=actual,
                hard_limit=self._policy.hard_input_limit,
            )

    def _compose(
        self,
        *,
        thread: ThreadState,
        state: ContextState,
        pending: tuple[Message, ...],
        memory_context: str,
        skill_context: str,
        tool_specs: tuple[ToolSpec, ...],
    ) -> tuple[ModelContext, ContextTokenBreakdown, int]:
        """组装一条 ModelContext。

        Returns:
            (context, breakdown, raw_total)

            `raw_total` 是**分配与裁剪之前**的原始估算总量，供调用方判断
            软阈值。判断必须用它而不是 `breakdown.total`：后者是按额度
            分配（且内容已被裁剪）之后的值，拿它当阈值依据会让超长的
            Memory / Skill / 历史先被砍掉，再「发现」没超阈值，从而绕过压缩。
        """
        snapshot = thread.snapshot()

        # 未被摘要覆盖的原始消息 = 近期工作集
        watermark = state.summarized_through
        recent = snapshot if watermark is None else snapshot[watermark + 1 :]

        summary_block = render_summary_block(state) or ""
        notes_block = render_task_notes_block(state) or ""

        # 分离 pending 中的「当前输入」与「进行中的工具回合」
        pending_groups = self._groups.build(pending, strict=False)
        cycle: list[Message] = []
        if pending_groups and pending_groups[-1].is_tool_cycle:
            cycle = list(pending_groups[-1].messages)
            leading = pending[: pending_groups[-1].start]
        else:
            leading = list(pending)
        current_input = "\n".join(m.content for m in leading)

        # --- 各段的原始需要量（未分配、未裁剪） ---
        summary_need = self._counter.count_text(summary_block)
        notes_need = self._counter.count_text(notes_block)
        memory_need = self._counter.count_text(memory_context)
        skills_need = self._counter.count_text(skill_context)
        cycle_tokens = self._counter.count_messages(cycle)
        tool_tokens = self._counter.count_tool_specs(list(tool_specs))
        current_tokens = self._counter.count_text(current_input)
        recent_tokens = self._counter.count_messages(recent)

        # 非弹性 system 段：目前没有固定 system 提示词，恒为 0。
        # Memory / Skill / Summary / TaskNotes **全部是弹性的**，
        # 它们通过下面的 *_need 进入弹性区块，绝不能重复计入 required——
        # 否则一条长 Memory 就会被当成「不可删除内容」而直接抛超限错误。
        fixed_system_tokens = 0

        # 判断软阈值用的原始总量：所有将发送内容在分配/裁剪前的估算
        raw_total = (
            summary_need
            + notes_need
            + memory_need
            + skills_need
            + current_tokens
            + cycle_tokens
            + tool_tokens
            + recent_tokens
        )

        breakdown = self._allocator.allocate(
            system_tokens=fixed_system_tokens,
            current_input_tokens=current_tokens,
            active_tool_cycle_tokens=cycle_tokens,
            tool_schema_tokens=tool_tokens,
            summary_need=summary_need,
            task_notes_need=notes_need,
            memory_need=memory_need,
            skills_need=skills_need,
            recent_tokens=recent_tokens,
        )

        # allocate 只给额度；内容必须按拿到的额度实际裁掉，
        # 否则「报告的 total」与「真实发送量」会对不上。
        summary_block = self._clip_to_tokens(summary_block, breakdown.summary)
        notes_block = self._clip_to_tokens(notes_block, breakdown.task_notes)
        memory_context = self._clip_to_tokens(memory_context, breakdown.memory)
        skill_context = self._clip_to_tokens(skill_context, breakdown.skills)
        recent = self._trim_recent_to_budget(recent, breakdown.recent_messages)

        messages: list[Message] = []
        if summary_block:
            messages.append(Message(role="system", content=summary_block))
        if notes_block:
            messages.append(Message(role="system", content=notes_block))
        if memory_context:
            messages.append(Message(role="system", content=memory_context))
        if skill_context:
            messages.append(Message(role="system", content=skill_context))
        messages.extend(recent)
        # 顺序必须是「当前输入 → 本轮工具往返」：
        # 末条消息决定模型看到的最新状态，把 tool_result 排在 user 前面
        # 会让模型以为还没处理当前提问，从而无限重发同一个工具调用。
        messages.extend(leading)
        messages.extend(cycle)

        composed = ModelContext(
            messages=tuple(messages),
            tool_specs=tuple(tool_specs),
            breakdown=breakdown,
        )
        # 硬上限对真实产物生效：组装完立刻按实际内容数一遍
        self._enforce_hard_limit(composed)
        return composed, breakdown, raw_total

    # ------------------------------------------------------------------
    # 内部：压缩与失败降级
    # ------------------------------------------------------------------

    def _compact(
        self,
        *,
        thread: ThreadState,
        state: ContextState,
        run_id: str,
        tokens_before: int,
    ) -> CompactionResult:
        snapshot = thread.snapshot()
        watermark = state.summarized_through
        recent = snapshot if watermark is None else snapshot[watermark + 1 :]
        if not recent:
            raise ValueError("没有可压缩的近期消息")

        groups = self._groups.build(recent)
        if not groups:
            raise ValueError("没有可安全压缩的消息组")

        # 最旧的一半组（至少 1 组），保持组边界完整
        batch = max(1, int(len(groups) * COMPACT_TARGET_RATIO))
        batch = min(batch, len(groups))
        target_groups = groups[:batch]
        # 分组序号要平移到 ThreadState 全局坐标系
        offset = 0 if watermark is None else watermark + 1
        shifted = [
            MessageGroup(
                start=g.start + offset,
                end=g.end + offset,
                messages=g.messages,
                is_tool_cycle=g.is_tool_cycle,
                is_complete=g.is_complete,
            )
            for g in target_groups
        ]
        assert self._compactor is not None
        return self._compactor.compact(
            thread=thread,
            state=state,
            groups=shifted,
            tokens_before=tokens_before,
            run_id=run_id,
            thread_id=thread.thread_id,
            counter=self._counter,
        )

    def _handle_compaction_failure(
        self,
        *,
        composed: ModelContext,
        breakdown: ContextTokenBreakdown,
        state: ContextState,
        error: Exception,
        run_id: str,
        thread_id: str,
        pending: tuple[Message, ...],
        memory_context: str,
        skill_context: str,
        tool_specs: tuple[ToolSpec, ...],
        started: float,
    ) -> AssembledContext:
        """压缩失败的降级判断（规格 4.8 失败策略）。

        这里**不再重复发** `context.compaction.failed`：`ContextCompactor`
        已在失败现场发过一次（带消息范围与压缩前 Token）。重复发会让消费方
        看到两条同名事件，无法判断是失败了两次还是记录了两次。
        降级决策本身记在随后那条 `context.assembled` 的
        `compaction_fallback` / `compaction_error` 里。
        """

        if breakdown.total > self._policy.hard_input_limit:
            # 已经压不下去又超了硬上限 → 明确失败，不删历史硬跑
            raise ContextWindowExceededError(
                required_tokens=breakdown.total,
                hard_limit=self._policy.hard_input_limit,
            ) from error

        # 原上下文仍在硬上限内：记失败 Trace，本轮用原上下文继续
        emit_event(
            self._trace_sink,
            name="context.assembled",
            run_id=run_id,
            thread_id=thread_id,
            operation="MODEL_CALL",
            status="success",
            metadata={
                **breakdown.to_dict(),
                "compaction_fallback": True,
                "compaction_error": type(error).__name__,
            },
            start=started,
        )
        return AssembledContext(
            context=composed,
            state=state,
            compacted=False,
            compaction_error=type(error).__name__,
            compaction_result=None,
        )
