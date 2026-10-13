"""Phase 24 / 7.8 Agent 集成。

覆盖规格 7.8：短会话行为不变、长会话触发压缩、模型实际收到 Summary /
Active Notes / 最近消息、原 Thread 保留完整历史、Memory 与 Skill 不重复注入、
摘要调用不递归、摘要使用独立 Budget、Trace 事件完整、Subagent 上下文隔离、
软阈值失败继续、硬上限失败。
"""

from __future__ import annotations

import json

import pytest

from mini_deerflow.agent import Agent
from mini_deerflow.context_assembler import ContextAssembler
from mini_deerflow.context_compactor import ContextCompactor
from mini_deerflow.context_errors import ContextWindowExceededError
from mini_deerflow.context_state import (
    ContextPolicy,
    ContextState,
    ConversationSummary,
    TaskNote,
    TaskNoteCategory,
)
from mini_deerflow.context_store import InMemoryContextStore, cas_update
from mini_deerflow.history_index import (
    HistoryIndexService,
    HistoryVectorIndex,
    LocalHashEmbeddingProvider,
)
from mini_deerflow.middleware import Budget, InMemoryTraceSink
from mini_deerflow.models import FakeModel, FinalAnswer, Message, ToolCall
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.token_counter import HeuristicTokenCounter
from mini_deerflow.tools import ToolRegistry, create_default_registry

THREAD = "thread-1"


def _policy(max_tokens: int = 8000, threshold: float = 0.8) -> ContextPolicy:
    return ContextPolicy(
        max_context_tokens=max_tokens,
        reserved_output_tokens=1000,
        safety_margin_tokens=500,
        compaction_threshold=threshold,
    )


class RecordingModel:
    """记录每次 decide 收到的完整消息序列，并按脚本返回响应。"""

    def __init__(self, responses: list | None = None) -> None:
        self._responses = list(responses or [])
        self.seen: list[list[Message]] = []

    def decide(self, messages, tools=None):
        self.seen.append(list(messages))
        if self._responses:
            response = self._responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response
        # 默认：按最后一条消息决定，模拟一轮就结束
        if messages and messages[-1].role == "user":
            return ToolCall(name="calculator", arguments={"expression": "1+1"}, id="c1")
        if messages and messages[-1].role == "tool":
            return FinalAnswer(content="计算结果: 2")
        return FinalAnswer(content="完成")

    @property
    def call_count(self) -> int:
        return len(self.seen)


def _summarizer_payload(narrative: str = "被压缩掉的早期讨论") -> str:
    return json.dumps(
        {
            "narrative": narrative,
            "facts": ["事实A"],
            "constraints": ["约束A"],
            "decisions": [],
            "failed_attempts": [],
            "open_items": [],
        },
        ensure_ascii=False,
    )


class SummarizingModel(RecordingModel):
    """同时扮演决策模型与摘要模型：按 system prompt 是否为摘要 prompt 区分。"""

    def __init__(self, narrative: str = "被压缩掉的早期讨论", fail_decision: bool = False):
        super().__init__([])
        self.narrative = narrative
        self.fail_decision = fail_decision
        self.summary_calls = 0

    def decide(self, messages, tools=None):
        self.seen.append(list(messages))
        is_summary_request = (
            bool(tools) is False
            and messages
            and messages[0].role == "system"
            and "上下文压缩器" in messages[0].content
        )
        if is_summary_request:
            self.summary_calls += 1
            return FinalAnswer(content=_summarizer_payload(self.narrative))
        if self.fail_decision:
            raise RuntimeError("决策路径故障")
        return FinalAnswer(content="回答完成")


def _long_thread(thread_id: str = THREAD, pairs: int = 60) -> InMemoryThreadStore:
    """构造一条长历史：每条消息都能被独立分组，方便精确控制压缩范围。

    默认 60 组（120 条）× 约 52 token ≈ 6240 token，
    超过默认 policy 的软阈值 5200，因此一定能触发压缩。
    """
    store = InMemoryThreadStore()
    store.create(thread_id)
    thread = store.get(thread_id)
    for index in range(pairs):
        thread.append(
            Message(role="user", content=f"第{index}个关于部署流程的长问题" * 4)
        )
        thread.append(
            Message(role="assistant", content=f"第{index}个详细回答内容" * 4)
        )
    store.save(thread)
    return store


def _assembler(
    store: InMemoryThreadStore,
    model,
    *,
    policy: ContextPolicy | None = None,
    sink: InMemoryTraceSink | None = None,
    summary_budget: Budget | None = None,
    enable_compaction: bool = True,
) -> ContextAssembler:
    context_store = InMemoryContextStore()
    counter = HeuristicTokenCounter()
    compactor = None
    if enable_compaction:
        compactor = ContextCompactor(
            model=model,
            counter=counter,
            policy=policy or _policy(),
            trace_sink=sink,
            budget=summary_budget or Budget(max_summary_calls=10),
        )
    return ContextAssembler(
        policy=policy or _policy(),
        counter=counter,
        context_store=context_store,
        compactor=compactor,
        trace_sink=sink,
    )


def _agent(
    model,
    store: InMemoryThreadStore,
    assembler: ContextAssembler | None,
    registry: ToolRegistry | None = None,
    max_steps: int = 10,
) -> Agent:
    return Agent(
        model=model,
        registry=registry if registry is not None else create_default_registry(),
        store=store,
        max_steps=max_steps,
        context_assembler=assembler,
    )


# ---------------------------------------------------------------------------
# 短会话行为不变
# ---------------------------------------------------------------------------


def test_short_session_without_assembler_is_unchanged() -> None:
    """不注入 assembler → 走原有拼装逻辑，输出与 Phase 23 完全一致。"""
    store = InMemoryThreadStore()
    store.create(THREAD)
    agent = _agent(FakeModel(), store, assembler=None)
    answer, trace = agent.run(THREAD, "计算 1+1")
    assert "2" in answer
    assert agent._last_assembled is None


def test_short_session_with_assembler_matches_without() -> None:
    """短会话下有无 assembler 的最终回答必须一致。"""
    store_a = InMemoryThreadStore()
    store_a.create(THREAD)
    plain = _agent(FakeModel(), store_a, assembler=None)
    answer_plain, _ = plain.run(THREAD, "计算 1+1")

    store_b = InMemoryThreadStore()
    store_b.create(THREAD)
    model = RecordingModel()
    assembler = _assembler(store_b, model, enable_compaction=False)
    with_assembler = _agent(FakeModel(), store_b, assembler)
    answer_with, _ = with_assembler.run(THREAD, "计算 1+1")

    assert answer_plain == answer_with


def test_short_session_does_not_trigger_compaction() -> None:
    store = _long_thread(pairs=1)
    model = SummarizingModel()
    assembler = _assembler(store, model)
    agent = _agent(model, store, assembler)

    agent.run(THREAD, "现在的问题")

    assert assembler.context_store.get(THREAD).summary is None
    assert assembler.context_store.get(THREAD).compaction_count == 0
    assert model.summary_calls == 0


# ---------------------------------------------------------------------------
# 长会话压缩
# ---------------------------------------------------------------------------


def test_long_session_triggers_compaction() -> None:
    store = _long_thread()
    model = SummarizingModel()
    assembler = _assembler(store, model)
    agent = _agent(model, store, assembler)

    agent.run(THREAD, "现在的问题")

    state = assembler.context_store.get(THREAD)
    assert state.compaction_count >= 1
    assert state.summary is not None
    assert state.summarized_through is not None
    assert model.summary_calls >= 1


def test_original_thread_keeps_every_message_after_compaction() -> None:
    """压缩只动投影，事实源一条都不能少。"""
    store = _long_thread()
    thread_before = store.get(THREAD).to_dict()
    model = SummarizingModel()
    assembler = _assembler(store, model)
    agent = _agent(model, store, assembler)

    agent.run(THREAD, "现在的问题")

    thread_after = store.get(THREAD).to_dict()
    # 消息内容未被改写（本轮新增的 user/assistant 会追加，但旧消息原样）
    assert thread_after["messages"][: len(thread_before["messages"])] == thread_before["messages"]
    assert thread_after["messages"] != thread_before["messages"]  # 本轮确实追加了


def test_model_receives_summary_and_recent_messages() -> None:
    store = _long_thread()
    model = SummarizingModel(narrative="这是压缩后的叙述")
    assembler = _assembler(store, model)
    agent = _agent(model, store, assembler)

    agent.run(THREAD, "现在的问题")

    # 至少有一次请求里同时带了摘要段与当前提问
    matched = [
        messages
        for messages in model.seen
        if any("已压缩的早期对话摘要" in m.content for m in messages)
        and any(m.role == "user" for m in messages)
    ]
    assert matched, "压缩后模型必须能收到摘要"
    last_call = matched[-1]
    summary_block = next(m for m in last_call if "已压缩的早期对话摘要" in m.content)
    assert "这是压缩后的叙述" in summary_block.content
    # 摘要段说明了原始消息仍可找回
    assert "history_search" in summary_block.content
    # 当前提问一定在场
    assert any(m.role == "user" and m.content == "现在的问题" for m in last_call)
    # 摘要在前、提问在后（system 先行）
    summary_index = last_call.index(summary_block)
    user_index = next(
        i for i, m in enumerate(last_call) if m.role == "user" and m.content == "现在的问题"
    )
    assert summary_index < user_index


def _seed_note(assembler: ContextAssembler, note: TaskNote) -> None:
    """把笔记写进 ContextState；新线程要先建好状态行。"""
    assembler.ensure_state(THREAD)
    cas_update(
        assembler.context_store, THREAD, lambda state: state.add_note(note)
    )


def test_model_receives_active_task_notes() -> None:
    store = _long_thread()
    model = SummarizingModel()
    assembler = _assembler(store, model)
    note = TaskNote.create(
        note_id="n1",
        category=TaskNoteCategory.CONSTRAINT,
        content="禁止调用真实模型",
        source_run_id="run-1",
    )
    _seed_note(assembler, note)

    agent = _agent(model, store, assembler)
    agent.run(THREAD, "现在的问题")

    has_note = any(
        "当前任务笔记" in m.content and "禁止调用真实模型" in m.content
        for messages in model.seen
        for m in messages
    )
    assert has_note, "active Task Note 必须进上下文"


def test_resolved_notes_are_not_injected() -> None:
    store = _long_thread()
    model = SummarizingModel()
    assembler = _assembler(store, model)
    note = TaskNote.create(
        note_id="n1",
        category=TaskNoteCategory.NEXT_STEP,
        content="已经做完的事",
        source_run_id="run-1",
    ).resolve()
    _seed_note(assembler, note)

    agent = _agent(model, store, assembler)
    agent.run(THREAD, "现在的问题")

    injected = any(
        "已经做完的事" in m.content for messages in model.seen for m in messages
    )
    assert injected is False


def test_memory_and_skill_injected_exactly_once() -> None:
    store = _long_thread(pairs=3)
    model = SummarizingModel()

    class MemoryProvider:
        def build(self) -> str:
            return "MEMORY_CONTEXT_MARK"

    class SkillProvider:
        def build(self, user_input: str, thread_id: str = "") -> str:
            return "SKILL_CONTEXT_MARK"

    assembler = _assembler(store, model, enable_compaction=False)
    recorder = RecordingModel(responses=[FinalAnswer(content="ok")])
    agent = Agent(
        model=recorder,
        registry=create_default_registry(),
        store=store,
        context_provider=MemoryProvider(),
        skill_context_provider=SkillProvider(),
        context_assembler=assembler,
    )
    agent.run(THREAD, "计算 1+1")

    # 每次 decide 各只看到一份 Memory / 一份 Skill（不会随循环重复叠加）
    assert recorder.seen, "模型应当被调用过"
    for messages in recorder.seen:
        assert sum(1 for m in messages if m.content == "MEMORY_CONTEXT_MARK") == 1
        assert sum(1 for m in messages if m.content == "SKILL_CONTEXT_MARK") == 1
        # 顺序：Memory 在 Skill 之前，两者都在历史之前
        memory_index = next(
            i for i, m in enumerate(messages) if m.content == "MEMORY_CONTEXT_MARK"
        )
        skill_index = next(
            i for i, m in enumerate(messages) if m.content == "SKILL_CONTEXT_MARK"
        )
        assert memory_index < skill_index


# ---------------------------------------------------------------------------
# 摘要调用的边界
# ---------------------------------------------------------------------------


def test_summary_request_never_reaches_agent_loop() -> None:
    """摘要只调 Model.decide，绝不进 Agent.run（否则会写进 Thread 并可递归）。"""
    store = _long_thread()

    class TrackingAgent(Agent):
        runs = 0

        def run(self, *args, **kwargs):
            TrackingAgent.runs += 1
            return super().run(*args, **kwargs)

    model = SummarizingModel()
    assembler = _assembler(store, model)
    agent = TrackingAgent(
        model=model,
        registry=create_default_registry(),
        store=store,
        context_assembler=assembler,
    )
    agent.run(THREAD, "现在的问题")

    assert TrackingAgent.runs == 1  # 摘要没有额外触发一轮 Agent.run
    assert model.summary_calls >= 1


def test_summary_uses_separate_budget_from_decisions() -> None:
    store = _long_thread()
    model = SummarizingModel()
    # 决策额度给满，摘要额度单独一份
    decision_budget = Budget(max_model_calls=50)
    summary_budget = Budget(max_summary_calls=5)
    assembler = _assembler(store, model, summary_budget=summary_budget)
    agent = Agent(
        model=model,
        registry=create_default_registry(),
        store=store,
        budget=decision_budget,
        context_assembler=assembler,
    )
    agent.run(THREAD, "现在的问题")

    # 摘要消耗只记在 summary_budget 上，决策额度只被那一次正常 decide 占用——
    # 两类调用互不串账，才是「独立 Budget」的含义。
    assert summary_budget.summary_calls() >= 1
    assert summary_budget.model_calls() == 0  # 决策没走摘要额度
    assert decision_budget.model_calls() == 1  # 本轮恰好一次正常决策
    assert decision_budget.summary_calls() == 0  # 摘要没走决策额度
    assert decision_budget.total_provider_calls() == 1
    assert summary_budget.total_provider_calls() == summary_budget.summary_calls()


def test_summary_budget_exhaustion_falls_back_when_under_hard_limit() -> None:
    """摘要额度用光 → 压缩失败 → 只要还在硬上限内就用原上下文继续。"""
    store = _long_thread()
    model = SummarizingModel(fail_decision=False)
    summary_budget = Budget(max_summary_calls=0)  # 一次都不许
    assembler = _assembler(store, model, summary_budget=summary_budget)
    agent = _agent(model, store, assembler)

    answer, _ = agent.run(THREAD, "现在的问题")

    # 没崩，仍然给出了回答
    assert answer
    state = assembler.context_store.get(THREAD)
    assert state.compaction_count == 0  # 压缩没成功
    assert state.summary is None


def test_decision_failure_over_hard_limit_raises() -> None:
    """必要内容本身超硬上限 → 必须抛错，不能悄悄裁掉当前提问。"""
    store = _long_thread(pairs=1)
    # 硬上限 = 1000 - 500 - 100 = 400，一条长消息就能超
    tiny = ContextPolicy(
        max_context_tokens=1000,
        reserved_output_tokens=500,
        safety_margin_tokens=100,
    )
    model = SummarizingModel()
    assembler = _assembler(store, model, policy=tiny, enable_compaction=False)
    agent = _agent(model, store, assembler)

    with pytest.raises(ContextWindowExceededError):
        agent.run(THREAD, "一个非常长的提问" * 100)


# ---------------------------------------------------------------------------
# Trace
# ---------------------------------------------------------------------------


def test_trace_contains_assembled_and_compaction_events() -> None:
    store = _long_thread()
    sink = InMemoryTraceSink()
    model = SummarizingModel()
    assembler = _assembler(store, model, sink=sink)
    agent = _agent(model, store, assembler)

    agent.run(THREAD, "现在的问题")

    names = [event.name for event in sink.get_events()]
    assert "context.assembled" in names
    assert "context.compaction.started" in names
    assert "context.compaction.completed" in names


def test_assembled_trace_carries_token_breakdown() -> None:
    store = _long_thread(pairs=2)
    sink = InMemoryTraceSink()
    model = SummarizingModel()
    assembler = _assembler(store, model, sink=sink, enable_compaction=False)
    agent = _agent(model, store, assembler)

    agent.run(THREAD, "现在的问题")

    assembled = [e for e in sink.get_events() if e.name == "context.assembled"]
    assert assembled
    metadata = assembled[0].metadata
    for key in ("system", "current_input", "recent_messages", "summary",
                "task_notes", "memory", "skills", "tool_schemas", "total"):
        assert key in metadata, f"缺少 Breakdown 字段 {key}"
    assert metadata["total"] == sum(
        metadata[k]
        for k in (
            "system", "current_input", "active_tool_cycle", "recent_messages",
            "summary", "task_notes", "memory", "skills", "tool_schemas",
        )
    )


def test_trace_never_contains_history_or_credentials() -> None:
    store = _long_thread()
    sink = InMemoryTraceSink()
    model = SummarizingModel()
    assembler = _assembler(store, model, sink=sink)
    agent = _agent(model, store, assembler)

    agent.run(THREAD, "现在的问题")

    payload = json.dumps(
        [event.to_dict() for event in sink.get_events()], ensure_ascii=False
    )
    assert "第0个关于部署流程" not in payload
    assert "sk-" not in payload


def test_compaction_failed_event_is_emitted_on_summary_failure() -> None:
    store = _long_thread()

    class AlwaysFailSummary:
        def decide(self, messages, tools=None):
            if tools == []:
                raise RuntimeError("摘要服务不可用")
            return FinalAnswer(content="ok")

    sink = InMemoryTraceSink()
    model = AlwaysFailSummary()
    assembler = _assembler(store, model, sink=sink)
    agent = _agent(model, store, assembler)

    agent.run(THREAD, "现在的问题")

    failed = [e for e in sink.get_events() if e.name == "context.compaction.failed"]
    assert len(failed) == 1
    assert failed[0].error_type == "RuntimeError"
    # 失败降级后仍然组装出了一次上下文
    assert any(e.name == "context.assembled" for e in sink.get_events())


# ---------------------------------------------------------------------------
# run_id 关联
# ---------------------------------------------------------------------------


def test_run_id_flows_into_context_trace() -> None:
    store = _long_thread(pairs=2)
    sink = InMemoryTraceSink()
    model = SummarizingModel()
    assembler = _assembler(store, model, sink=sink, enable_compaction=False)
    agent = _agent(model, store, assembler)

    agent.run(THREAD, "现在的问题", run_id="run-from-caller")

    assembled = [e for e in sink.get_events() if e.name == "context.assembled"]
    assert assembled
    assert assembled[0].run_id == "run-from-caller"


def test_default_run_id_is_empty_string() -> None:
    store = _long_thread(pairs=2)
    sink = InMemoryTraceSink()
    model = SummarizingModel()
    assembler = _assembler(store, model, sink=sink, enable_compaction=False)
    agent = _agent(model, store, assembler)

    agent.run(THREAD, "现在的问题")  # 不传 run_id

    assembled = [e for e in sink.get_events() if e.name == "context.assembled"]
    assert assembled[0].run_id == ""


# ---------------------------------------------------------------------------
# Subagent 隔离
# ---------------------------------------------------------------------------


def test_two_subagents_do_not_share_summary_notes_or_index() -> None:
    """两个子代理用同一个 assembler，但状态按 thread_id 完全隔离。"""
    thread_store = InMemoryThreadStore()
    context_store = InMemoryContextStore()
    counter = HeuristicTokenCounter()
    service = HistoryIndexService(
        index=HistoryVectorIndex(LocalHashEmbeddingProvider(dimensions=32)),
        thread_store=thread_store,
        context_store=context_store,
    )
    assembler = ContextAssembler(
        policy=_policy(),
        counter=counter,
        context_store=context_store,
        history_service=service,
    )

    child_a = "subagent-task-a"
    child_b = "subagent-task-b"
    for thread_id, text in ((child_a, "子任务 A 的部署细节"), (child_b, "子任务 B 的配色方案")):
        thread_store.create(thread_id)
        thread = thread_store.get(thread_id)
        thread.append(Message(role="user", content=text))
        thread_store.save(thread)
        assembler.ensure_state(thread_id)

    # A 写笔记
    note = TaskNote.create(
        note_id="only-a",
        category=TaskNoteCategory.CONSTRAINT,
        content="A 专属约束",
        source_run_id="run-a",
    )
    cas_update(context_store, child_a, lambda state: state.add_note(note))
    # A 建索引
    service.sync(child_a)

    state_a = context_store.get(child_a)
    state_b = context_store.get(child_b)

    assert state_a.task_notes and state_b.task_notes == ()
    assert state_a.revision != state_b.revision
    assert state_a.indexed_through == 0
    assert state_b.indexed_through is None
    # 父线程从未被创建
    assert context_store.exists("default") is False
    # 向量索引也各自独立
    assert service.index.size(child_a) == 1
    assert service.index.size(child_b) == 0


def test_subagent_thread_deletion_cleans_its_projections_only() -> None:
    thread_store = InMemoryThreadStore()
    context_store = InMemoryContextStore()
    counter = HeuristicTokenCounter()
    service = HistoryIndexService(
        index=HistoryVectorIndex(LocalHashEmbeddingProvider(dimensions=32)),
        thread_store=thread_store,
        context_store=context_store,
    )
    assembler = ContextAssembler(
        policy=_policy(),
        counter=counter,
        context_store=context_store,
        history_service=service,
    )

    parent = "parent-thread"
    child = "subagent-task-x"
    for thread_id in (parent, child):
        context_store.create(thread_id)
        service.index.upsert(
            thread_id, [Message(role="user", content=f"{thread_id} 的内容")]
        )

    assembler.drop_thread_projections(child)

    assert context_store.exists(child) is False
    assert service.index.size(child) == 0
    # 父线程分毫未动
    assert context_store.exists(parent) is True
    assert service.index.size(parent) == 1


# ---------------------------------------------------------------------------
# 组装顺序
# ---------------------------------------------------------------------------


def test_pending_tool_cycle_follows_current_input() -> None:
    """末条消息必须是最新状态：工具结果排在用户输入之后。"""
    store = InMemoryThreadStore()
    store.create(THREAD)
    model = RecordingModel(
        responses=[
            ToolCall(name="calculator", arguments={"expression": "1+1"}, id="c1"),
            FinalAnswer(content="2"),
        ]
    )
    assembler = _assembler(store, model, enable_compaction=False)
    agent = _agent(model, store, assembler)

    agent.run(THREAD, "计算 1+1")

    second_call = model.seen[-1]
    roles = [m.role for m in second_call]
    user_index = roles.index("user")
    tool_index = roles.index("tool")
    assert user_index < tool_index, "工具结果必须排在当前输入之后"
    assert second_call[-1].role == "tool"


def test_watermark_slices_recent_messages() -> None:
    """压缩后 recent 只包含水位之后的消息，旧的由摘要代替。"""
    store = _long_thread(pairs=10)
    model = SummarizingModel()
    assembler = _assembler(store, model, enable_compaction=False)

    thread = store.get(THREAD)
    mid = 9
    summary = ConversationSummary.build(
        narrative="水位之前的那批讨论",
        facts=["老事实"],
        summarized_from=0,
        summarized_through=mid,
    )
    state = ContextState(summary=summary, summarized_through=mid)

    composed, _breakdown, _raw_total = assembler._compose(  # 内部接口，仅用于验证切片语义
        thread=thread,
        state=state,
        pending=(Message(role="user", content="当前提问"),),
        memory_context="",
        skill_context="",
        tool_specs=(),
    )
    contents = [m.content for m in composed.messages]

    # 水位之前的老消息不再出现在 recent 里
    assert not any(c.startswith("第0个关于部署流程的长问题") for c in contents)
    # 水位之后的消息仍在
    assert any("第5个详细回答内容" in c for c in contents)
    # 摘要段顶替了被压缩的那段
    assert any("已压缩的早期对话摘要" in c for c in contents)
    assert any("水位之前的那批讨论" in c for c in contents)
    # 当前提问仍在最后
    assert composed.messages[-1].content == "当前提问"


# ---------------------------------------------------------------------------
# 风险 2：软阈值判断与弹性区块
# ---------------------------------------------------------------------------


def test_compaction_uses_raw_tokens_not_allocation() -> None:
    """裁剪让 `breakdown.total` 掉到软阈值以下，**也必须触发压缩**。

    构造：两条超长历史，第二条单独就吃不满预算，于是按组边界从后往前累加时
    只留下第二条 —— `recent` 被裁掉近一半，`breakdown.total` 随之跌破软阈值。
    若拿它当判断依据，超长历史会「先被砍、再发现没超阈值」，从而绕过压缩：
    被砍掉的那段既没进摘要，也没了原文。
    """
    store = InMemoryThreadStore()
    store.create(THREAD)
    thread = store.get(THREAD)
    # 两条各约 4000 token（CJK 一字≈一 token），合计远超软阈值 5200
    thread.append(Message(role="user", content="超长提问" + "部署细节说明" * 700))
    thread.append(Message(role="assistant", content="超长回答" + "详细展开说明" * 700))
    store.save(thread)

    counter = HeuristicTokenCounter()
    raw_total = counter.count_messages(thread.snapshot())
    assert raw_total >= _policy().soft_compaction_limit, "测试前提：原始量必须超阈值"

    model = SummarizingModel()
    assembler = _assembler(store, model)
    agent = _agent(model, store, assembler)
    agent.run(THREAD, "当前提问")

    state = assembler.context_store.get(THREAD)
    assert state.compaction_count >= 1, "原始量超软阈值就必须压缩"
    assert state.summary is not None
    assert state.summarized_through is not None, "压缩必须推进水位"

    # 事实源一条没少：原有两条 + 本轮的 user/assistant
    assert thread.message_count == 4
    assert thread.snapshot()[0].content.startswith("超长提问")
    assert thread.snapshot()[1].content.startswith("超长回答")


def test_assembled_trace_carries_raw_total() -> None:
    """Trace 里必须同时记录 raw_total 与分配后的 total，两者可对照。"""
    store = _long_thread(pairs=2)
    sink = InMemoryTraceSink()
    model = SummarizingModel()
    assembler = _assembler(store, model, sink=sink, enable_compaction=False)
    agent = _agent(model, store, assembler)

    agent.run(THREAD, "现在的问题")

    assembled = [e for e in sink.get_events() if e.name == "context.assembled"]
    assert assembled
    metadata = assembled[0].metadata
    assert "raw_total" in metadata
    assert "total" in metadata
    # raw_total 是分配前，必然不小于分配后的 total
    assert metadata["raw_total"] >= metadata["total"]
    assert metadata["soft_limit"] == _policy().soft_compaction_limit
    assert metadata["hard_limit"] == _policy().hard_input_limit


def test_long_memory_is_elastic_not_required() -> None:
    """超长 Memory 是**弹性**区块：被 cap 并裁剪，而不是把整轮打成超限错误。

    修复前 `system_tokens` 把 Memory 计入 required，一条长记忆就会抛
    `ContextWindowExceededError`——那等于说「记忆多一点就别跑了」，
    与规格 4.6「Memory 有弹性上限」直接冲突。
    """
    store = _long_thread(pairs=1)
    model = SummarizingModel()
    assembler = _assembler(store, model, enable_compaction=False)

    long_memory = "记忆内容" * 3000  # 约 18000 token，远超 memory cap 650

    class MemoryProvider:
        def build(self) -> str:
            return long_memory

    agent = Agent(
        model=model,
        registry=create_default_registry(),
        store=store,
        context_provider=MemoryProvider(),
        context_assembler=assembler,
    )
    answer, _ = agent.run(THREAD, "现在的问题")

    assert answer, "长 Memory 不该让本轮失败"
    counter = HeuristicTokenCounter()
    context = agent._last_assembled.context

    # 1) 硬上限对最终产物生效
    assert counter.count_messages(context.messages) <= _policy().hard_input_limit
    # 2) Memory 被 cap，而不是按全文发送
    assert context.breakdown.memory <= _policy().cap("memory")
    memory_msgs = [
        m
        for m in context.messages
        if m.role == "system" and m.content.startswith("记忆内容")
    ]
    assert memory_msgs, "Memory 仍应在场（只是被裁短）"
    assert counter.count_text(memory_msgs[0].content) <= _policy().cap("memory") + 1
    # 3) 全文从未进入发送内容
    assert long_memory not in [m.content for m in context.messages]


def test_hard_limit_applies_to_final_model_context() -> None:
    """硬上限对**最终要发的 ModelContext** 生效，而不是只对分配值生效。"""
    store = InMemoryThreadStore()
    store.create(THREAD)
    thread = store.get(THREAD)
    thread.append(Message(role="user", content="历史" + "内容" * 100))
    store.save(thread)

    # 硬上限很小，弹性区块各自被 cap，recent 受剩余额度约束
    tiny = ContextPolicy(
        max_context_tokens=2000,
        reserved_output_tokens=400,
        safety_margin_tokens=200,
    )
    model = SummarizingModel()
    assembler = _assembler(store, model, policy=tiny, enable_compaction=False)
    agent = _agent(model, store, assembler)
    agent.run(THREAD, "当前提问")

    counter = HeuristicTokenCounter()
    context = agent._last_assembled.context
    actual = counter.count_messages(context.messages) + counter.count_tool_specs(
        list(context.tool_specs)
    )
    assert actual <= tiny.hard_input_limit
    assert context.breakdown.total <= tiny.hard_input_limit
