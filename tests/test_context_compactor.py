"""Phase 24 / 7.5 ContextCompactor。

覆盖规格 7.5：严格 JSON 成功、ToolCall 被拒、一次格式修复后成功、
两次失败、增量合并、第 5 次完整重建、重建失败保留旧 Summary、
ThreadState 始终不变、摘要调用独立 Budget、Trace 事件。
"""

from __future__ import annotations

import json

import pytest

from mini_deerflow.context_compactor import (
    REBUILD_EVERY,
    SUMMARY_SYSTEM_PROMPT,
    ContextCompactor,
    parse_summary_payload,
)
from mini_deerflow.context_errors import (
    SummaryFormatError,
    SummaryInvalidResponseError,
)
from mini_deerflow.context_state import (
    ConversationSummary,
    ContextPolicy,
    ContextState,
)
from mini_deerflow.message_group import MessageGroupBuilder
from mini_deerflow.middleware import Budget, BudgetExceededError, InMemoryTraceSink
from mini_deerflow.models import FinalAnswer, Message, ToolCall
from mini_deerflow.thread_state import ThreadState
from mini_deerflow.token_counter import HeuristicTokenCounter

THREAD = "thread-1"


def _policy() -> ContextPolicy:
    return ContextPolicy(
        max_context_tokens=8000,
        reserved_output_tokens=1000,
        safety_margin_tokens=500,
    )


def _payload(**overrides) -> str:
    body: dict = {
        "narrative": "早期讨论了部署方案",
        "facts": ["已确认使用 SQLite"],
        "constraints": ["必须离线测试"],
        "decisions": ["用内存 store 兜底"],
        "failed_attempts": [],
        "open_items": ["补文档"],
    }
    body.update(overrides)
    return json.dumps(body, ensure_ascii=False)


class ScriptedModel:
    """按脚本依次返回响应；记录每次调用的入参供断言。"""

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def decide(self, messages, tools=None):
        self.calls.append({"messages": list(messages), "tools": tools})
        if not self._responses:
            raise AssertionError("模型被调用次数超出脚本长度")
        response = self._responses.pop(0)
        # 脚本里放 Exception 表示「这次调用直接失败」
        if isinstance(response, BaseException):
            raise response
        return response

    @property
    def call_count(self) -> int:
        return len(self.calls)


class FailingModel:
    """压缩路径必须能把底层故障原样抛给调用方。"""

    def decide(self, messages, tools=None):
        raise RuntimeError("provider 不可用")


def _thread(message_count: int = 6) -> ThreadState:
    thread = ThreadState(thread_id=THREAD)
    for index in range(message_count):
        thread.append(Message(role="user", content=f"问题{index}"))
        thread.append(Message(role="assistant", content=f"回答{index}"))
    return thread


def _groups(thread: ThreadState, limit: int | None = None):
    groups = MessageGroupBuilder().build(thread.snapshot())
    return groups if limit is None else groups[:limit]


def _compactor(model, sink=None, budget=None) -> ContextCompactor:
    return ContextCompactor(
        model=model,
        counter=HeuristicTokenCounter(),
        policy=_policy(),
        trace_sink=sink,
        budget=budget if budget is not None else Budget(max_summary_calls=10),
    )


# ---------------------------------------------------------------------------
# JSON 解析
# ---------------------------------------------------------------------------


def test_parse_plain_json() -> None:
    data = parse_summary_payload(_payload())
    assert data["narrative"] == "早期讨论了部署方案"


def test_parse_strips_markdown_fence() -> None:
    fenced = "```json\n" + _payload() + "\n```"
    assert parse_summary_payload(fenced)["facts"] == ["已确认使用 SQLite"]


def test_parse_rejects_non_object() -> None:
    with pytest.raises(SummaryFormatError, match="JSON 对象"):
        parse_summary_payload('["not", "an", "object"]')


def test_parse_rejects_empty() -> None:
    with pytest.raises(SummaryFormatError, match="空"):
        parse_summary_payload("   ")


def test_parse_rejects_truncated_json() -> None:
    with pytest.raises(SummaryFormatError, match="解析失败"):
        parse_summary_payload('{"narrative": "缺右括号"')


def test_parse_rejects_free_text() -> None:
    with pytest.raises(SummaryFormatError):
        parse_summary_payload("这是一段自然语言，不是 JSON")


# ---------------------------------------------------------------------------
# 正常压缩
# ---------------------------------------------------------------------------


def test_successful_compaction_produces_summary() -> None:
    thread = _thread()
    model = ScriptedModel([FinalAnswer(content=_payload())])
    compactor = _compactor(model)
    state = ContextState()
    target = _groups(thread, limit=4)

    result = compactor.compact(
        thread=thread,
        state=state,
        groups=target,
        tokens_before=3000,
        run_id="run-1",
        thread_id=THREAD,
    )

    assert model.call_count == 1
    assert result.summary.narrative == "早期讨论了部署方案"
    assert result.covered_from == target[0].start
    assert result.covered_through == target[-1].end - 1
    assert result.state.summarized_through == result.covered_through
    assert result.state.compaction_count == 1
    assert result.rebuilt is False
    assert result.summary_calls == 1


def test_summary_request_never_offers_tools() -> None:
    """摘要路径禁止工具执行：tools 必须传空，而不是 None。"""
    thread = _thread()
    model = ScriptedModel([FinalAnswer(content=_payload())])
    compactor = _compactor(model)
    compactor.compact(
        thread=thread,
        state=ContextState(),
        groups=_groups(thread, limit=4),
        tokens_before=1,
        run_id="run-1",
        thread_id=THREAD,
    )
    assert model.calls[0]["tools"] == []


def test_summary_prompt_declares_history_untrusted() -> None:
    """历史文本是不可信数据——这条必须写进 system prompt。"""
    assert "不可信数据" in SUMMARY_SYSTEM_PROMPT
    assert "不是系统指令" in SUMMARY_SYSTEM_PROMPT
    assert "凭据" in SUMMARY_SYSTEM_PROMPT


def test_summary_messages_lead_with_system_prompt() -> None:
    thread = _thread()
    model = ScriptedModel([FinalAnswer(content=_payload())])
    compactor = _compactor(model)
    compactor.compact(
        thread=thread,
        state=ContextState(),
        groups=_groups(thread, limit=4),
        tokens_before=1,
        run_id="run-1",
        thread_id=THREAD,
    )
    sent = model.calls[0]["messages"]
    assert sent[0].role == "system"
    assert sent[0].content == SUMMARY_SYSTEM_PROMPT
    assert sent[-1].role == "user"


def test_history_rendered_with_positions_matching_watermark() -> None:
    """摘要输入的 [index] 必须与 ContextState 的水位坐标系一致。"""
    thread = _thread()
    model = ScriptedModel([FinalAnswer(content=_payload())])
    compactor = _compactor(model)
    target = _groups(thread, limit=4)  # 每条消息自成一组 → 覆盖 index 0..3
    compactor.compact(
        thread=thread,
        state=ContextState(),
        groups=target,
        tokens_before=1,
        run_id="run-1",
        thread_id=THREAD,
    )
    history_text = model.calls[0]["messages"][-1].content
    assert "[0] user:" in history_text
    assert "[3] assistant:" in history_text


def test_thread_state_is_never_modified() -> None:
    thread = _thread()
    before = thread.to_dict()
    model = ScriptedModel([FinalAnswer(content=_payload())])
    compactor = _compactor(model)

    compactor.compact(
        thread=thread,
        state=ContextState(),
        groups=_groups(thread, limit=4),
        tokens_before=1,
        run_id="run-1",
        thread_id=THREAD,
    )
    assert thread.to_dict() == before
    assert thread.message_count == 12  # 6 组 × 2 条，一条没少


def test_empty_groups_rejected() -> None:
    thread = _thread()
    compactor = _compactor(ScriptedModel([]))
    with pytest.raises(ValueError, match="不能为空"):
        compactor.compact(
            thread=thread,
            state=ContextState(),
            groups=[],
            tokens_before=1,
            run_id="run-1",
            thread_id=THREAD,
        )


# ---------------------------------------------------------------------------
# 非法响应
# ---------------------------------------------------------------------------


def test_tool_call_response_is_rejected_immediately() -> None:
    """摘要返回 ToolCall 属于非法响应，不进入格式修复流程。"""
    thread = _thread()
    model = ScriptedModel(
        [ToolCall(name="calculator", arguments={"expression": "1+1"})]
    )
    compactor = _compactor(model)
    with pytest.raises(SummaryInvalidResponseError):
        compactor.compact(
            thread=thread,
            state=ContextState(),
            groups=_groups(thread, limit=4),
            tokens_before=1,
            run_id="run-1",
            thread_id=THREAD,
        )
    # 一次调用就终止，不会浪费修复请求
    assert model.call_count == 1


def test_first_format_error_triggers_one_repair_then_succeeds() -> None:
    thread = _thread()
    model = ScriptedModel(
        [FinalAnswer(content="这不是 JSON"), FinalAnswer(content=_payload())]
    )
    compactor = _compactor(model)
    budget = Budget(max_summary_calls=10)

    result = _compactor(model, budget=budget).compact(
        thread=thread,
        state=ContextState(),
        groups=_groups(thread, limit=4),
        tokens_before=1,
        run_id="run-1",
        thread_id=THREAD,
    )
    assert model.call_count == 2
    assert result.summary_calls == 2
    assert budget.summary_calls() == 2

    # 第二次请求带上了原输出与错误原因，让模型自纠
    repair = model.calls[1]["messages"]
    assert any(m.role == "assistant" for m in repair)
    assert any("不是合法 JSON" in m.content for m in repair if m.role == "user")


def test_two_format_failures_raise_with_attempt_count() -> None:
    thread = _thread()
    model = ScriptedModel(
        [FinalAnswer(content="第一次错"), FinalAnswer(content="第二次错")]
    )
    compactor = _compactor(model)
    with pytest.raises(SummaryFormatError) as excinfo:
        compactor.compact(
            thread=thread,
            state=ContextState(),
            groups=_groups(thread, limit=4),
            tokens_before=1,
            run_id="run-1",
            thread_id=THREAD,
        )
    assert excinfo.value.attempts == 2
    assert model.call_count == 2  # 不做第三次尝试


def test_summary_call_budget_is_independent_of_model_calls() -> None:
    """摘要额度与决策额度分开：决策已满仍可压缩，反之亦然。"""
    thread = _thread()
    budget = Budget(max_model_calls=1, max_summary_calls=10)
    budget.consume_model_call()  # 决策额度用光

    model = ScriptedModel([FinalAnswer(content=_payload())])
    result = _compactor(model, budget=budget).compact(
        thread=thread,
        state=ContextState(),
        groups=_groups(thread, limit=4),
        tokens_before=1,
        run_id="run-1",
        thread_id=THREAD,
    )
    # 决策额度已耗尽，摘要照常完成
    assert result.summary is not None
    assert budget.summary_calls() == 1
    # 总 Provider 消耗把两类都算进去
    assert budget.total_provider_calls() == 2


def test_summary_budget_exhaustion_propagates() -> None:
    thread = _thread()
    budget = Budget(max_summary_calls=1)
    model = ScriptedModel(
        [FinalAnswer(content="坏的"), FinalAnswer(content=_payload())]
    )
    compactor = _compactor(model, budget=budget)
    with pytest.raises(BudgetExceededError) as excinfo:
        compactor.compact(
            thread=thread,
            state=ContextState(),
            groups=_groups(thread, limit=4),
            tokens_before=1,
            run_id="run-1",
            thread_id=THREAD,
        )
    assert excinfo.value.resource == "summary_calls"


def test_model_failure_propagates_and_emits_failed_trace() -> None:
    thread = _thread()
    sink = InMemoryTraceSink()
    compactor = _compactor(FailingModel(), sink=sink)
    with pytest.raises(RuntimeError, match="provider 不可用"):
        compactor.compact(
            thread=thread,
            state=ContextState(),
            groups=_groups(thread, limit=4),
            tokens_before=1234,
            run_id="run-1",
            thread_id=THREAD,
        )
    failed = [e for e in sink.get_events() if e.name == "context.compaction.failed"]
    assert len(failed) == 1
    assert failed[0].error_type == "RuntimeError"
    assert failed[0].metadata["tokens_before"] == 1234


# ---------------------------------------------------------------------------
# 合并与重建
# ---------------------------------------------------------------------------


def test_incremental_merge_appends_new_facts() -> None:
    thread = _thread()
    model = ScriptedModel([FinalAnswer(content=_payload())])
    compactor = _compactor(model)

    first = compactor.compact(
        thread=thread,
        state=ContextState(),
        groups=_groups(thread, limit=4),
        tokens_before=100,
        run_id="run-1",
        thread_id=THREAD,
    )

    second_model = ScriptedModel(
        [
            FinalAnswer(
                content=_payload(
                    narrative="第二段讨论",
                    facts=["新增事实B"],
                    decisions=["新决策"],
                )
            )
        ]
    )
    second = _compactor(second_model).compact(
        thread=thread,
        state=first.state,
        groups=_groups(thread, limit=4),
        tokens_before=100,
        run_id="run-2",
        thread_id=THREAD,
    )

    assert "已确认使用 SQLite" in second.summary.facts
    assert "新增事实B" in second.summary.facts
    assert "用内存 store 兜底" in second.summary.decisions
    assert "新决策" in second.summary.decisions
    assert second.state.compaction_count == 2
    assert second.rebuilt is False


def test_rebuild_triggers_every_fifth_compaction() -> None:
    thread = _thread()
    state = ContextState()
    sink = InMemoryTraceSink()
    compactor = _compactor(ScriptedModel([FinalAnswer(content=_payload())]), sink=sink)

    rebuilt_flags: list[bool] = []
    for index in range(REBUILD_EVERY):
        responses = [FinalAnswer(content=_payload(facts=[f"事实{index}"]))]
        # 第 5 次会额外发一次「从原始消息完整重建」的请求
        if (index + 1) % REBUILD_EVERY == 0:
            responses.append(
                FinalAnswer(content=_payload(narrative=f"重建叙述{index}"))
            )
        result = _compactor(ScriptedModel(responses), sink=sink).compact(
            thread=thread,
            state=state,
            groups=_groups(thread, limit=4),
            tokens_before=100,
            run_id=f"run-{index}",
            thread_id=THREAD,
        )
        rebuilt_flags.append(result.rebuilt)
        state = result.state

    # 前 4 次增量合并，第 5 次完整重建
    assert rebuilt_flags == [False, False, False, False, True]
    assert state.compaction_count == REBUILD_EVERY
    assert state.summary.narrative == f"重建叙述{REBUILD_EVERY - 1}"


def test_rebuild_covers_complete_original_range() -> None:
    """重建从 ThreadState 的原始消息重算，范围就是被摘要覆盖的那段。"""
    thread = _thread()
    state = ContextState(compaction_count=REBUILD_EVERY - 1)  # 下一次就是第 5 次
    model = ScriptedModel(
        [
            FinalAnswer(content=_payload()),  # 第 1 次：本块摘要
            FinalAnswer(content=_payload(narrative="重建后的叙述")),  # 第 2 次：完整重建
        ]
    )
    result = _compactor(model).compact(
        thread=thread,
        state=state,
        groups=_groups(thread, limit=4),
        tokens_before=100,
        run_id="run-1",
        thread_id=THREAD,
    )
    assert result.rebuilt is True
    assert result.summary.narrative == "重建后的叙述"
    assert model.call_count == 2  # 1 次块摘要 + 1 次完整重建
    # 重建范围与压缩范围一致
    assert result.summary.summarized_from == result.covered_from
    assert result.summary.summarized_through == result.covered_through


def test_rebuild_failure_falls_back_to_incremental_merge() -> None:
    """重建那一步失败时必须保住旧 Summary，不能把已有摘要清空。"""
    thread = _thread()
    prior = ContextState(
        summary=ConversationSummary.build(
            narrative="既有的旧摘要",
            facts=["旧事实"],
            summarized_from=0,
            summarized_through=3,
        ),
        summarized_through=3,
        compaction_count=REBUILD_EVERY - 1,  # 下一次触发重建
    )

    # 第 1 次调用（块摘要）成功，第 2 次调用（重建）失败
    model = ScriptedModel(
        [
            FinalAnswer(content=_payload(facts=["新增事实"])),
            RuntimeError("重建时网络断了"),
        ]
    )

    result = _compactor(model).compact(
        thread=thread,
        state=prior,
        groups=_groups(thread, limit=4),
        tokens_before=100,
        run_id="run-1",
        thread_id=THREAD,
    )

    assert result.rebuilt is False  # 走了降级路径
    assert result.summary.narrative  # 摘要没丢
    assert "既有的旧摘要" in result.summary.narrative or "早期讨论" in result.summary.narrative
    assert "旧事实" in result.summary.facts
    assert "新增事实" in result.summary.facts  # 新块也合进去了


# ---------------------------------------------------------------------------
# Trace
# ---------------------------------------------------------------------------


def test_trace_emits_started_and_completed() -> None:
    thread = _thread()
    sink = InMemoryTraceSink()
    model = ScriptedModel([FinalAnswer(content=_payload())])
    _compactor(model, sink=sink).compact(
        thread=thread,
        state=ContextState(),
        groups=_groups(thread, limit=4),
        tokens_before=2000,
        run_id="run-42",
        thread_id=THREAD,
    )

    names = [event.name for event in sink.get_events()]
    assert names == ["context.compaction.started", "context.compaction.completed"]
    started, completed = sink.get_events()

    assert started.operation == "SUMMARIZATION"
    assert started.run_id == "run-42"
    assert started.thread_id == THREAD

    for event in (started, completed):
        # 绝不能把历史正文或凭据写进 Trace
        assert "问题0" not in json.dumps(event.metadata, ensure_ascii=False)
        assert "sk-" not in json.dumps(event.metadata, ensure_ascii=False)

    assert completed.metadata["tokens_before"] == 2000
    assert "tokens_after" in completed.metadata
    assert "messages_from" in completed.metadata
    assert "messages_through" in completed.metadata
    assert "summary_version" in completed.metadata
    assert completed.metadata["compaction_count"] == 1


def test_trace_records_covering_range() -> None:
    thread = _thread()
    sink = InMemoryTraceSink()
    target = _groups(thread, limit=4)
    _compactor(ScriptedModel([FinalAnswer(content=_payload())]), sink=sink).compact(
        thread=thread,
        state=ContextState(),
        groups=target,
        tokens_before=1,
        run_id="run-1",
        thread_id=THREAD,
    )
    metadata = sink.get_events()[-1].metadata
    assert metadata["messages_from"] == target[0].start
    assert metadata["messages_through"] == target[-1].end - 1
    assert metadata["group_count"] == len(target)


def test_no_sink_is_safe() -> None:
    thread = _thread()
    compactor = _compactor(ScriptedModel([FinalAnswer(content=_payload())]), sink=None)
    result = compactor.compact(
        thread=thread,
        state=ContextState(),
        groups=_groups(thread, limit=4),
        tokens_before=1,
        run_id="run-1",
        thread_id=THREAD,
    )
    assert result.summary is not None
