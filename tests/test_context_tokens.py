"""Phase 24 / 7.3 Token 与分配、7.4 安全消息分组。

7.3 覆盖：低于阈值不压缩、达到 80% 触发、reserved output / safety margin、
空 Memory/Skill 额度回流、必要内容超过硬上限、Breakdown 总和正确。

7.4 覆盖：普通消息、单/多工具调用、工具失败、ToolCall 与 ToolResult 不被
拆分、损坏序列、压缩边界恰好落在 ToolCall 前后。
"""

from __future__ import annotations

import pytest

from mini_deerflow.context_errors import (
    ContextWindowExceededError,
    MalformedMessageSequenceError,
)
from mini_deerflow.context_state import ContextPolicy
from mini_deerflow.message_group import MessageGroupBuilder, is_tool_call_message
from mini_deerflow.model_context import ContextTokenBreakdown, ElasticTokenAllocator
from mini_deerflow.models import Message
from mini_deerflow.token_counter import MESSAGE_OVERHEAD_TOKENS, HeuristicTokenCounter


def _policy(
    max_tokens: int = 8000,
    reserved: int = 1000,
    margin: int = 500,
    threshold: float = 0.8,
) -> ContextPolicy:
    return ContextPolicy(
        max_context_tokens=max_tokens,
        reserved_output_tokens=reserved,
        safety_margin_tokens=margin,
        compaction_threshold=threshold,
    )


def _allocator(policy: ContextPolicy | None = None) -> ElasticTokenAllocator:
    return ElasticTokenAllocator(policy or _policy(), HeuristicTokenCounter())


def _alloc(allocator: ElasticTokenAllocator, **overrides) -> ContextTokenBreakdown:
    defaults: dict[str, int] = dict(
        system_tokens=0,
        current_input_tokens=0,
        active_tool_cycle_tokens=0,
        tool_schema_tokens=0,
        summary_need=0,
        task_notes_need=0,
        memory_need=0,
        skills_need=0,
        recent_tokens=0,
    )
    defaults.update(overrides)
    return allocator.allocate(**defaults)


# ---------------------------------------------------------------------------
# 7.3 Token 计数
# ---------------------------------------------------------------------------


def test_heuristic_counter_counts_ascii_conservatively() -> None:
    """ASCII 按 3 字符 ≈ 1 token（Provider 常见 4）→ 估算偏保守偏高。"""
    counter = HeuristicTokenCounter()
    text = "abcdefgh"  # 8 ASCII → ceil(8/3) = 3
    assert counter.count_text(text) == 3
    assert counter.count_text("") == 0


def test_heuristic_counter_counts_cjk_as_one_token_each() -> None:
    counter = HeuristicTokenCounter()
    assert counter.count_text("部署流程") == 4


def test_counter_adds_per_message_overhead() -> None:
    counter = HeuristicTokenCounter()
    plain = Message(role="user", content="")
    assert counter.count_message(plain) == MESSAGE_OVERHEAD_TOKENS


def test_counter_counts_tool_arguments_and_names() -> None:
    counter = HeuristicTokenCounter()
    with_tools = Message(
        role="assistant",
        content="",
        tool_call_id="c1",
        tool_name="calculator",
        tool_arguments={"expression": "1+1"},
    )
    without = Message(role="assistant", content="", tool_call_id=None)
    assert counter.count_message(with_tools) > counter.count_message(without)


def test_counter_tool_specs_scale_with_count() -> None:
    counter = HeuristicTokenCounter()
    from mini_deerflow.models import ToolSpec

    def spec(name: str) -> ToolSpec:
        return ToolSpec(name=name, description=f"工具 {name}", input_schema={"type": "object"})

    one = counter.count_tool_specs([spec("a")])
    three = counter.count_tool_specs([spec("a"), spec("b"), spec("c")])
    assert three > one > 0


def test_count_messages_is_sum_of_individual() -> None:
    counter = HeuristicTokenCounter()
    messages = [
        Message(role="user", content="第一段"),
        Message(role="assistant", content="second"),
    ]
    assert counter.count_messages(messages) == sum(
        counter.count_message(m) for m in messages
    )


# ---------------------------------------------------------------------------
# 7.3 分配
# ---------------------------------------------------------------------------


def test_policy_formula_and_threshold() -> None:
    policy = _policy(max_tokens=8000, reserved=1000, margin=500, threshold=0.8)
    assert policy.hard_input_limit == 6500
    assert policy.soft_compaction_limit == 5200


def test_reserved_and_margin_reduce_hard_limit() -> None:
    allocator = _allocator(_policy(max_tokens=8000, reserved=2000, margin=1000))
    assert allocator.policy.hard_input_limit == 5000


def test_below_threshold_does_not_trigger_compaction() -> None:
    allocator = _allocator()
    breakdown = _alloc(
        allocator,
        system_tokens=1000,
        current_input_tokens=100,
        recent_tokens=1000,
    )
    assert breakdown.total == 2100
    assert allocator.should_compact(breakdown) is False


def test_exactly_at_soft_threshold_triggers_compaction() -> None:
    policy = _policy()
    allocator = _allocator(policy)
    # recent 补到正好 5200 - 1000 = 4200 → total == soft limit
    breakdown = _alloc(allocator, system_tokens=1000, recent_tokens=4200)
    assert breakdown.total == policy.soft_compaction_limit
    assert allocator.should_compact(breakdown) is True


def test_just_below_soft_threshold_does_not_trigger() -> None:
    policy = _policy()
    allocator = _allocator(policy)
    breakdown = _alloc(allocator, system_tokens=1000, recent_tokens=4199)
    assert breakdown.total == policy.soft_compaction_limit - 1
    assert allocator.should_compact(breakdown) is False


def test_empty_memory_and_skills_allow_fallback_to_recent() -> None:
    """Memory/Skill 一个字都没有时，它们的额度必须让给最近原始消息。

    回流放宽的是**上限**而不是凭空填满：recent 需要多少就给多少
    （直到剩余额度），不会被任何固定份额截断，也不会把没用到的
    额度凭空算进 total。
    """
    policy = _policy()
    allocator = _allocator(policy)
    breakdown = _alloc(
        allocator,
        system_tokens=1000,
        memory_need=0,
        skills_need=0,
        summary_need=0,
        task_notes_need=0,
        recent_tokens=1000,
    )
    assert breakdown.memory == 0
    assert breakdown.skills == 0
    assert breakdown.recent_messages == 1000  # 拿到需要的全部，没被截断
    # 未使用的额度不被「预留」：total 只反映真实内容
    assert breakdown.total == 1000 + 1000
    assert breakdown.total < policy.hard_input_limit

    # 关键对比：recent 需求超过任一弹性 cap 时仍能吃满剩余额度，
    # 说明没有给 recent 设固定份额（固定份额 = 6500-1000-sum(caps) = 2250）
    generous = _alloc(
        allocator,
        system_tokens=1000,
        recent_tokens=policy.hard_input_limit - 1000,
    )
    fixed_share_equivalent = policy.hard_input_limit - 1000 - sum(
        policy.cap(name) for name in ("summary", "task_notes", "memory", "skills")
    )
    assert generous.recent_messages > fixed_share_equivalent
    assert generous.recent_messages == policy.hard_input_limit - 1000


def test_unused_elastic_budget_falls_back_even_when_recent_is_large() -> None:
    """回流放宽的是**上限**：recent 需要多少就能用多少（直到剩余额度）。"""
    policy = _policy()
    allocator = _allocator(policy)
    breakdown = _alloc(
        allocator,
        system_tokens=1000,
        recent_tokens=policy.hard_input_limit - 1000 - 1,
    )
    assert breakdown.recent_messages == policy.hard_input_limit - 1001
    assert breakdown.total == policy.hard_input_limit - 1


def test_elastic_sections_capped_by_their_ratio() -> None:
    policy = _policy()
    allocator = _allocator(policy)
    breakdown = _alloc(
        allocator,
        system_tokens=1000,
        summary_need=10_000_000,
        task_notes_need=10_000_000,
        memory_need=10_000_000,
        skills_need=10_000_000,
    )
    assert breakdown.summary == policy.cap("summary")
    assert breakdown.task_notes == policy.cap("task_notes")
    assert breakdown.memory == policy.cap("memory")
    assert breakdown.skills == policy.cap("skills")


def test_required_content_over_hard_limit_raises() -> None:
    allocator = _allocator(_policy(max_tokens=8000, reserved=1000, margin=500))
    with pytest.raises(ContextWindowExceededError) as excinfo:
        _alloc(allocator, system_tokens=7000)
    assert excinfo.value.required_tokens == 7000
    assert excinfo.value.hard_limit == 6500


def test_required_content_exactly_at_hard_limit_is_allowed() -> None:
    allocator = _allocator(_policy())
    breakdown = _alloc(allocator, system_tokens=6500)
    assert breakdown.required == 6500
    assert breakdown.recent_messages == 0


def test_breakdown_total_equals_sum_of_all_sections() -> None:
    allocator = _allocator()
    breakdown = _alloc(
        allocator,
        system_tokens=1000,
        current_input_tokens=200,
        active_tool_cycle_tokens=300,
        tool_schema_tokens=400,
        summary_need=500,
        task_notes_need=100,
        memory_need=50,
        skills_need=60,
        recent_tokens=3000,
    )
    expected = (
        breakdown.system
        + breakdown.current_input
        + breakdown.active_tool_cycle
        + breakdown.tool_schemas
        + breakdown.summary
        + breakdown.task_notes
        + breakdown.memory
        + breakdown.skills
        + breakdown.recent_messages
    )
    assert breakdown.total == expected
    assert breakdown.total == breakdown.required + breakdown.elastic + breakdown.recent_messages


def test_breakdown_serializes_every_section() -> None:
    breakdown = ContextTokenBreakdown(
        system=1,
        current_input=2,
        active_tool_cycle=3,
        recent_messages=4,
        summary=5,
        task_notes=6,
        memory=7,
        skills=8,
        tool_schemas=9,
    )
    payload = breakdown.to_dict()
    assert payload == {
        "system": 1,
        "current_input": 2,
        "active_tool_cycle": 3,
        "recent_messages": 4,
        "summary": 5,
        "task_notes": 6,
        "memory": 7,
        "skills": 8,
        "tool_schemas": 9,
        "total": 45,
    }


def test_over_hard_limit_detection() -> None:
    allocator = _allocator(_policy())
    ok = _alloc(allocator, system_tokens=1000, recent_tokens=5000)
    assert allocator.is_over_hard_limit(ok) is False


def test_allocator_rejects_negative_and_non_int_inputs() -> None:
    allocator = _allocator()
    with pytest.raises(ValueError, match="不能为负"):
        _alloc(allocator, system_tokens=-1)
    with pytest.raises(TypeError, match="必须是整数"):
        _alloc(allocator, system_tokens=1.5)  # type: ignore[arg-type]


def test_allocator_rejects_non_policy() -> None:
    with pytest.raises(TypeError, match="ContextPolicy"):
        ElasticTokenAllocator("nope", HeuristicTokenCounter())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 7.4 消息分组
# ---------------------------------------------------------------------------


def _user(text: str) -> Message:
    return Message(role="user", content=text)


def _assistant(text: str) -> Message:
    return Message(role="assistant", content=text)


def _call(call_id: str, name: str = "calculator") -> Message:
    return Message(
        role="assistant",
        content=f"调用 {name}",
        tool_call_id=call_id,
        tool_name=name,
        tool_arguments={"expression": "1+1"},
    )


def _result(call_id: str, text: str = "2") -> Message:
    return Message(role="tool", content=text, tool_call_id=call_id)


def test_plain_messages_each_form_their_own_group() -> None:
    groups = MessageGroupBuilder().build([_user("q"), _assistant("a")])
    assert [(g.start, g.end) for g in groups] == [(0, 1), (1, 2)]
    assert all(not g.is_tool_cycle for g in groups)


def test_single_tool_cycle_stays_in_one_group() -> None:
    messages = [_user("计算"), _call("c1"), _result("c1"), _assistant("结果是 2")]
    groups = MessageGroupBuilder().build(messages)
    assert len(groups) == 2
    assert groups[0].is_tool_cycle is False
    assert groups[1].is_tool_cycle is True
    assert groups[1].messages == tuple(messages[1:])
    assert (groups[1].start, groups[1].end) == (1, 4)


def test_multiple_parallel_and_sequential_calls_form_one_group() -> None:
    messages = [
        _user("q"),
        _call("c1"),
        _call("c2"),
        _result("c1"),
        _result("c2"),
        _call("c3"),
        _result("c3"),
        _assistant("done"),
    ]
    groups = MessageGroupBuilder().build(messages)
    assert len(groups) == 2
    assert groups[1].is_tool_cycle is True
    assert len(groups[1].messages) == 7
    assert (groups[1].start, groups[1].end) == (1, 8)


def test_failed_tool_result_still_binds_to_its_call() -> None:
    messages = [
        _user("q"),
        _call("c1"),
        _result("c1", "boom"),
        _assistant("失败了"),
    ]
    groups = MessageGroupBuilder().build(messages)
    assert len(groups) == 2
    cycle = groups[1]
    assert cycle.is_tool_cycle is True
    assert cycle.messages[1].content == "boom"
    assert cycle.messages[-1].content == "失败了"


def test_orphan_tool_result_is_rejected() -> None:
    with pytest.raises(MalformedMessageSequenceError, match="孤立"):
        MessageGroupBuilder().build([_user("q"), _result("c1"), _assistant("x")])


def test_dangling_tool_chain_is_rejected() -> None:
    """有 tool_call 却没有收尾 assistant → 不能当作完整回合。"""
    with pytest.raises(MalformedMessageSequenceError, match="悬空"):
        MessageGroupBuilder().build([_user("q"), _call("c1"), _result("c1")])


def test_user_message_interrupting_chain_is_rejected() -> None:
    with pytest.raises(MalformedMessageSequenceError, match="插入工具链"):
        MessageGroupBuilder().build(
            [_call("c1"), _result("c1"), _user("插话"), _assistant("x")]
        )


def test_lenient_mode_marks_broken_groups_incomplete() -> None:
    """strict=False 时不抛错，但必须标出结构不完整，供调用方降级。"""
    groups = MessageGroupBuilder().build(
        [_call("c1"), _result("c1"), _user("插话")], strict=False
    )
    # 破损的工具链自己一组（后接的 user 另成一组）
    broken = next(g for g in groups if g.is_tool_cycle)
    assert broken.is_complete is False
    assert broken.messages[0].tool_call_id == "c1"
    assert any(g.is_complete for g in groups)  # 完好部分仍然标为完整


def test_non_message_elements_rejected() -> None:
    with pytest.raises(TypeError, match="Message"):
        MessageGroupBuilder().build([_user("q"), "not-a-message"])  # type: ignore[list-item]


def test_non_sequence_rejected() -> None:
    with pytest.raises(TypeError, match="序列"):
        MessageGroupBuilder().build("nope")  # type: ignore[arg-type]


def test_empty_sequence_yields_no_groups() -> None:
    assert MessageGroupBuilder().build([]) == []


def test_groups_tile_the_sequence_without_gaps() -> None:
    messages = [
        _user("q0"),
        _assistant("a0"),
        _call("c1"),
        _result("c1"),
        _assistant("a1"),
        _user("q1"),
        _call("c2"),
        _call("c3"),
        _result("c2"),
        _result("c3"),
        _assistant("a2"),
    ]
    groups = MessageGroupBuilder().build(messages)
    assert groups[0].start == 0
    assert groups[-1].end == len(messages)
    for previous, current in zip(groups, groups[1:]):
        assert previous.end == current.start
    # 恰好覆盖每条消息一次
    covered = [index for group in groups for index in range(group.start, group.end)]
    assert covered == list(range(len(messages)))


def test_is_tool_call_message_requires_assistant_role() -> None:
    assert is_tool_call_message(_call("c1")) is True
    # role=tool 的是结果，方向相反，不能当成新链的开头
    assert is_tool_call_message(_result("c1")) is False
    assert is_tool_call_message(_assistant("普通回答")) is False


# ---------------------------------------------------------------------------
# 7.4 压缩边界吸附
# ---------------------------------------------------------------------------


def test_boundary_inside_cycle_is_absorbed_to_group_start() -> None:
    """切点落在 tool_call 与 result 之间 → 整组保留，绝不产生孤立 result。"""
    messages = [
        _user("q"),
        _assistant("a1"),
        _call("c1"),
        _result("c1"),
        _assistant("a2"),
        _assistant("a3"),
    ]
    builder = MessageGroupBuilder()
    # 目标切点 = 6 - 3 = 3，正好落在工具链内部（链占 2..4）
    retained_prefix, kept = builder.split_at_safe_boundary(messages, keep_last=3)
    assert retained_prefix == tuple(messages[:2])  # 吸附到链起点 2
    assert kept == tuple(messages[2:])
    # 保留段从 tool_call 起头，而不是从链中间的 tool result 起头
    assert kept[0].tool_call_id == "c1"
    assert all(g.is_complete for g in builder.build(kept))


def test_boundary_on_group_start_keeps_whole_group() -> None:
    messages = [_user("q"), _assistant("a1"), _call("c1"), _result("c1"), _assistant("a2")]
    builder = MessageGroupBuilder()
    # 切点 = 5 - 2 = 3，落在工具链内（链占 2..4）→ 吸附到 2
    retained, kept = builder.split_at_safe_boundary(messages, keep_last=2)
    assert retained == tuple(messages[:2])
    assert kept == tuple(messages[2:])
    groups = builder.build(kept)
    assert all(g.is_complete for g in groups)
    # 保留段里若有 tool 结果，就必须带上配对的 tool_call（在同一组内）
    for group in groups:
        if any(m.role == "tool" for m in group.messages):
            assert group.is_tool_cycle is True
            assert any(m.tool_call_id for m in group.messages if m.role == "assistant")


def test_keep_last_equal_to_length_keeps_everything() -> None:
    messages = [_user("q"), _assistant("a")]
    retained, kept = MessageGroupBuilder().split_at_safe_boundary(
        messages, keep_last=10
    )
    assert retained == ()
    assert kept == tuple(messages)


def test_keep_last_zero_snaps_to_last_group_start() -> None:
    """keep_last=0 时切点吸附到最后一组起点 → 保留段非空，不切出空组。"""
    messages = [_user("q"), _assistant("a")]
    retained, kept = MessageGroupBuilder().split_at_safe_boundary(
        messages, keep_last=0
    )
    # 目标切点 2 落在组 (1,2) 内 → 吸附到组起点 1
    assert retained == tuple(messages[:1])
    assert kept == (messages[-1],)
    assert all(g.is_complete for g in MessageGroupBuilder().build(kept))


def test_negative_keep_last_rejected() -> None:
    with pytest.raises(ValueError, match="keep_last"):
        MessageGroupBuilder().split_at_safe_boundary([_user("q")], keep_last=-1)


def test_split_on_empty_sequence() -> None:
    retained, kept = MessageGroupBuilder().split_at_safe_boundary([], keep_last=3)
    assert retained == ()
    assert kept == ()
