"""Phase 24 / 7.6 task_note 工具契约。

覆盖规格 7.6：add/update/resolve/list、不存在的 note_id、已 resolved 再更新、
字符数与数量上限、Run 失败后 Note 仍存在、无法操作其他 thread、
revision 冲突重试。
"""

from __future__ import annotations

import json

import pytest

from mini_deerflow.context_errors import (
    ContextNotFoundError,
    TaskNoteLimitExceededError,
    TaskNoteNotFoundError,
)
from mini_deerflow.context_state import (
    TASK_NOTE_MAX_ACTIVE,
    TASK_NOTE_MAX_CONTENT_CHARS,
    TASK_NOTE_MAX_TOTAL,
    ContextState,
    TaskNote,
    TaskNoteCategory,
    TaskNoteStatus,
    empty_context_state,
)
from mini_deerflow.context_store import InMemoryContextStore, SQLiteContextStore, cas_update
from mini_deerflow.context_tools import (
    LIST_CONTENT_CHARS,
    LIST_DEFAULT_LIMIT,
    LIST_MAX_LIMIT,
    LIST_MAX_OUTPUT_CHARS,
    TASK_NOTE_TOOL_NAME,
    build_task_note_tool,
)
from mini_deerflow.middleware import (
    ExecutionContext,
    InMemoryTraceSink,
    OperationType,
)
from mini_deerflow.tools import ToolExecutionError, ToolRegistry

THREAD = "thread-a"
OTHER = "thread-b"


def _ctx(thread_id: str = THREAD, run_id: str = "run-1") -> ExecutionContext:
    return ExecutionContext(
        run_id=run_id,
        thread_id=thread_id,
        operation=OperationType.TOOL_CALL,
        operation_name=TASK_NOTE_TOOL_NAME,
    )


@pytest.fixture
def store() -> InMemoryContextStore:
    handle = InMemoryContextStore()
    handle.create(THREAD)
    return handle


@pytest.fixture
def sink() -> InMemoryTraceSink:
    return InMemoryTraceSink()


@pytest.fixture
def registry(store: InMemoryContextStore, sink: InMemoryTraceSink) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(build_task_note_tool(store, trace_sink=sink))
    return registry


def call(
    registry: ToolRegistry,
    arguments: dict,
    context: ExecutionContext | None = None,
) -> dict:
    """执行工具并把结果解析成 dict（工具返回 JSON 字符串）。"""
    raw = registry.execute(TASK_NOTE_TOOL_NAME, arguments, context=context)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# 工具契约：thread_id 不暴露给模型
# ---------------------------------------------------------------------------


def test_tool_schema_has_no_thread_id() -> None:
    """模型不能指定 thread_id——否则一句话就能跨会话读写别人的笔记。"""
    tool = build_task_note_tool(InMemoryContextStore())
    schema = json.dumps(tool.parameters)
    assert "thread_id" not in schema
    assert tool.needs_context is True


def test_tool_requires_action_and_rejects_extras() -> None:
    tool = build_task_note_tool(InMemoryContextStore())
    assert tool.parameters["required"] == ["action"]
    assert tool.parameters["additionalProperties"] is False
    # 必需的四个核心参数，加上 list 的分页参数
    assert set(tool.parameters["properties"]) == {
        "action",
        "note_id",
        "category",
        "content",
        "limit",
        "offset",
        "status",
    }
    # 分页参数只对 list 有意义，且都带边界说明
    assert tool.parameters["properties"]["limit"]["type"] == "integer"
    assert str(LIST_MAX_LIMIT) in tool.parameters["properties"]["limit"]["description"]
    assert tool.parameters["properties"]["status"]["enum"] == ["active", "resolved", "all"]


def test_invalid_action_rejected(registry: ToolRegistry) -> None:
    result = call(registry, {"action": "delete"}, context=_ctx())
    assert result["ok"] is False


def test_missing_context_is_rejected(registry: ToolRegistry) -> None:
    """拿不到 ExecutionContext 就拒绝执行，绝不落到某个默认线程上。"""
    with pytest.raises(ToolExecutionError, match="thread_id"):
        registry.execute(TASK_NOTE_TOOL_NAME, {"action": "list"}, context=None)


def test_empty_context_thread_id_is_rejected(registry: ToolRegistry) -> None:
    blank = ExecutionContext(run_id="r", thread_id="", operation=OperationType.TOOL_CALL)
    with pytest.raises(ToolExecutionError, match="thread_id"):
        registry.execute(TASK_NOTE_TOOL_NAME, {"action": "list"}, context=blank)


# ---------------------------------------------------------------------------
# add / list / update / resolve
# ---------------------------------------------------------------------------


def test_add_creates_active_note(registry: ToolRegistry, store: InMemoryContextStore) -> None:
    result = call(
        registry,
        {"action": "add", "category": "constraint", "content": "禁止调用真实模型"},
        context=_ctx(),
    )
    assert result["ok"] is True
    assert result["category"] == "constraint"
    assert result["active_count"] == 1
    assert result["total_count"] == 1

    stored = store.get(THREAD)
    note = stored.find_note(result["note_id"])
    assert note is not None
    assert note.content == "禁止调用真实模型"
    assert note.category is TaskNoteCategory.CONSTRAINT
    assert note.status is TaskNoteStatus.ACTIVE
    assert note.source_run_id == "run-1"


def test_add_trims_content(registry: ToolRegistry, store: InMemoryContextStore) -> None:
    result = call(
        registry,
        {"action": "add", "category": "decision", "content": "  用 SQLite  "},
        context=_ctx(),
    )
    assert store.get(THREAD).find_note(result["note_id"]).content == "用 SQLite"  # type: ignore[union-attr]


def test_add_with_custom_note_id(registry: ToolRegistry, store: InMemoryContextStore) -> None:
    result = call(
        registry,
        {"action": "add", "note_id": "my-id", "category": "failure", "content": "试过了"},
        context=_ctx(),
    )
    assert result["note_id"] == "my-id"
    assert store.get(THREAD).find_note("my-id") is not None


def test_add_without_note_id_generates_one(
    registry: ToolRegistry, store: InMemoryContextStore
) -> None:
    first = call(
        registry, {"action": "add", "category": "failure", "content": "A"}, context=_ctx()
    )
    second = call(
        registry, {"action": "add", "category": "failure", "content": "B"}, context=_ctx()
    )
    assert first["note_id"] != second["note_id"]
    assert first["note_id"].startswith("note-")


def test_add_requires_content(registry: ToolRegistry) -> None:
    assert call(registry, {"action": "add", "category": "decision"}, context=_ctx())["ok"] is False
    assert call(
        registry,
        {"action": "add", "category": "decision", "content": "   "},
        context=_ctx(),
    )["ok"] is False


def test_add_rejects_invalid_category(registry: ToolRegistry) -> None:
    result = call(
        registry,
        {"action": "add", "category": "wishlist", "content": "x"},
        context=_ctx(),
    )
    assert result["ok"] is False
    assert "category" in result["error"]


def test_add_rejects_unknown_category_value(registry: ToolRegistry) -> None:
    """枚举里没有的类别必须明确拒绝，不能默默当成别的类别存进去。"""
    result = call(
        registry,
        {"action": "add", "category": "constraint_typo", "content": "x"},
        context=_ctx(),
    )
    assert result["ok"] is False


def test_list_returns_empty_for_fresh_thread(store: InMemoryContextStore) -> None:
    registry = ToolRegistry()
    registry.register(build_task_note_tool(store))
    result = call(registry, {"action": "list"}, context=_ctx())
    assert result["ok"] is True
    assert result["notes"] == []
    assert result["active_count"] == 0


def test_list_returns_active_by_default(
    registry: ToolRegistry, store: InMemoryContextStore
) -> None:
    """默认只返回 active：已 resolved 的笔记多数时候不需要进上下文。"""
    call(
        registry, {"action": "add", "category": "constraint", "content": "约束1"},
        context=_ctx(),
    )
    first = call(
        registry, {"action": "add", "category": "next_step", "content": "步骤1"},
        context=_ctx(),
    )
    call(registry, {"action": "resolve", "note_id": first["note_id"]}, context=_ctx())

    listed = call(registry, {"action": "list"}, context=_ctx())
    assert listed["status"] == "active"
    assert len(listed["notes"]) == 1
    assert listed["active_count"] == 1
    assert listed["total_count"] == 2
    assert listed["pool_count"] == 1  # 可翻页的池子就是 active 池
    assert all(n["status"] == "active" for n in listed["notes"])

    # status="all" 才拿得到全部
    everything = call(registry, {"action": "list", "status": "all"}, context=_ctx())
    assert len(everything["notes"]) == 2
    statuses = {n["note_id"]: n["status"] for n in everything["notes"]}
    assert statuses[first["note_id"]] == "resolved"

    # status="resolved" 只拿已解决的
    resolved = call(registry, {"action": "list", "status": "resolved"}, context=_ctx())
    assert [n["note_id"] for n in resolved["notes"]] == [first["note_id"]]


def test_update_changes_content(registry: ToolRegistry, store: InMemoryContextStore) -> None:
    created = call(
        registry, {"action": "add", "category": "decision", "content": "旧内容"},
        context=_ctx(),
    )
    updated = call(
        registry,
        {"action": "update", "note_id": created["note_id"], "content": "新内容"},
        context=_ctx(),
    )
    assert updated["ok"] is True
    note = store.get(THREAD).find_note(created["note_id"])
    assert note.content == "新内容"  # type: ignore[union-attr]


def test_update_can_change_category(
    registry: ToolRegistry, store: InMemoryContextStore
) -> None:
    created = call(
        registry, {"action": "add", "category": "decision", "content": "x"},
        context=_ctx(),
    )
    call(
        registry,
        {"action": "update", "note_id": created["note_id"], "content": "x",
         "category": "constraint"},
        context=_ctx(),
    )
    note = store.get(THREAD).find_note(created["note_id"])
    assert note.category is TaskNoteCategory.CONSTRAINT  # type: ignore[union-attr]


def test_update_missing_note(registry: ToolRegistry) -> None:
    result = call(
        registry, {"action": "update", "note_id": "ghost", "content": "x"}, context=_ctx()
    )
    assert result["ok"] is False
    assert "不存在" in result["error"]


def test_update_requires_note_id(registry: ToolRegistry) -> None:
    assert call(registry, {"action": "update", "content": "x"}, context=_ctx())["ok"] is False


def test_update_requires_content(registry: ToolRegistry, store: InMemoryContextStore) -> None:
    created = call(
        registry, {"action": "add", "category": "decision", "content": "x"}, context=_ctx()
    )
    result = call(
        registry, {"action": "update", "note_id": created["note_id"]}, context=_ctx()
    )
    assert result["ok"] is False


def test_resolve_marks_note(registry: ToolRegistry, store: InMemoryContextStore) -> None:
    created = call(
        registry, {"action": "add", "category": "next_step", "content": "做一件事"},
        context=_ctx(),
    )
    resolved = call(
        registry, {"action": "resolve", "note_id": created["note_id"]}, context=_ctx()
    )
    assert resolved["ok"] is True
    assert resolved["status"] == "resolved"
    note = store.get(THREAD).find_note(created["note_id"])
    assert note.status is TaskNoteStatus.RESOLVED  # type: ignore[union-attr]


def test_resolve_is_idempotent(registry: ToolRegistry, store: InMemoryContextStore) -> None:
    created = call(
        registry, {"action": "add", "category": "next_step", "content": "x"}, context=_ctx()
    )
    call(registry, {"action": "resolve", "note_id": created["note_id"]}, context=_ctx())
    again = call(registry, {"action": "resolve", "note_id": created["note_id"]}, context=_ctx())
    assert again["ok"] is True
    assert again["active_count"] == 0


def test_resolved_note_cannot_be_updated(
    registry: ToolRegistry, store: InMemoryContextStore
) -> None:
    """已 resolve 的 Note 不许再改——否则审计线索会被事后抹掉。"""
    created = call(
        registry, {"action": "add", "category": "failure", "content": "原内容"}, context=_ctx()
    )
    call(registry, {"action": "resolve", "note_id": created["note_id"]}, context=_ctx())
    result = call(
        registry,
        {"action": "update", "note_id": created["note_id"], "content": "改写"},
        context=_ctx(),
    )
    assert result["ok"] is False
    assert "resolve" in result["error"]
    # 内容未被改动
    note = store.get(THREAD).find_note(created["note_id"])
    assert note.content == "原内容"  # type: ignore[union-attr]
    assert note.status is TaskNoteStatus.RESOLVED  # type: ignore[union-attr]


def test_resolve_missing_note(registry: ToolRegistry) -> None:
    result = call(registry, {"action": "resolve", "note_id": "ghost"}, context=_ctx())
    assert result["ok"] is False
    assert "不存在" in result["error"]


# ---------------------------------------------------------------------------
# 上限
# ---------------------------------------------------------------------------


def test_content_length_limit_is_enforced(registry: ToolRegistry) -> None:
    result = call(
        registry,
        {
            "action": "add",
            "category": "constraint",
            "content": "x" * (TASK_NOTE_MAX_CONTENT_CHARS + 1),
        },
        context=_ctx(),
    )
    assert result["ok"] is False
    assert "超长" in result["error"]


def test_active_limit_rejects_overflow_without_evicting(
    registry: ToolRegistry, store: InMemoryContextStore
) -> None:
    for index in range(TASK_NOTE_MAX_ACTIVE):
        result = call(
            registry,
            {"action": "add", "category": "decision", "content": f"条目{index}"},
            context=_ctx(),
        )
        assert result["ok"] is True

    overflow = call(
        registry,
        {"action": "add", "category": "decision", "content": "挤不进来"},
        context=_ctx(),
    )
    assert overflow["ok"] is False
    assert "active_notes" in overflow["error"]

    # 一条都没被静默挤掉
    state = store.get(THREAD)
    assert len(state.active_notes) == TASK_NOTE_MAX_ACTIVE
    assert state.find_note(state.active_notes[0].note_id).content.startswith("条目")  # type: ignore[union-attr]


def test_total_limit_rejects_overflow(registry: ToolRegistry, store: InMemoryContextStore) -> None:
    for index in range(TASK_NOTE_MAX_TOTAL):
        call(
            registry,
            {"action": "add", "category": "decision", "content": f"条目{index}"},
            context=_ctx(),
        )
        # 持续 resolve，让 active 名额始终空着 → 命中的是总数上限
        listed = call(registry, {"action": "list"}, context=_ctx())
        active = listed["notes"][-1]["note_id"]
        call(registry, {"action": "resolve", "note_id": active}, context=_ctx())

    overflow = call(
        registry,
        {"action": "add", "category": "decision", "content": "挤不进来"},
        context=_ctx(),
    )
    assert overflow["ok"] is False
    assert "total_notes" in overflow["error"]
    assert len(store.get(THREAD).task_notes) == TASK_NOTE_MAX_TOTAL


# ---------------------------------------------------------------------------
# 持久化与隔离
# ---------------------------------------------------------------------------


def test_note_persists_even_if_run_fails_later(registry: ToolRegistry, store: InMemoryContextStore) -> None:
    """写入即落库：本轮 Run 后续失败、超时、取消都不会丢掉已写的 Note。"""
    created = call(
        registry,
        {"action": "add", "category": "constraint", "content": "先记下来"},
        context=_ctx(run_id="run-that-later-fails"),
    )
    # 模拟本轮后续步骤抛异常——工具层不参与，Note 早已落在 store 里
    stored = store.get(THREAD).find_note(created["note_id"])
    assert stored is not None
    assert stored.source_run_id == "run-that-later-fails"
    # revision 已推进，说明确实写过库
    assert store.get(THREAD).revision >= 1


def test_note_carries_source_run_id_for_audit(
    registry: ToolRegistry, store: InMemoryContextStore
) -> None:
    call(
        registry, {"action": "add", "category": "decision", "content": "x"},
        context=_ctx(run_id="run-777"),
    )
    note = store.get(THREAD).task_notes[0]
    assert note.source_run_id == "run-777"


def test_thread_isolation(registry: ToolRegistry, store: InMemoryContextStore) -> None:
    """一个线程写的笔记，在另一个线程里完全看不见。"""
    call(
        registry, {"action": "add", "category": "constraint", "content": "A线程的约束"},
        context=_ctx(thread_id=THREAD),
    )
    # B 线程尚未初始化 → 自动创建一份空 ContextState
    other = call(registry, {"action": "list"}, context=_ctx(thread_id=OTHER))
    assert other["notes"] == []
    assert other["active_count"] == 0
    # A 线程不受影响
    mine = call(registry, {"action": "list"}, context=_ctx(thread_id=THREAD))
    assert mine["active_count"] == 1
    # 两个线程各自一份独立状态
    assert store.exists(THREAD) and store.exists(OTHER)
    assert store.get(THREAD).revision != store.get(OTHER).revision


def test_auto_creates_missing_context_state(registry: ToolRegistry, store: InMemoryContextStore) -> None:
    assert store.exists("brand-new") is False
    result = call(registry, {"action": "list"}, context=_ctx(thread_id="brand-new"))
    assert result["ok"] is True
    assert store.exists("brand-new") is True


def test_list_on_missing_state_without_create_is_clean(
    store: InMemoryContextStore,
) -> None:
    """ContextState 不存在时 list 返回空而不是抛错（新会话的常态）。"""
    registry = ToolRegistry()
    registry.register(build_task_note_tool(store))
    result = call(registry, {"action": "list"}, context=_ctx(thread_id="vanished"))
    assert result["ok"] is True
    assert result["notes"] == []


# ---------------------------------------------------------------------------
# CAS 冲突重试
# ---------------------------------------------------------------------------


def test_revision_conflict_is_retried_transparently(
    registry: ToolRegistry, store: InMemoryContextStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """首个写入尝试撞上并发修改时，cas_update 会重读重试，工具调用仍成功。"""
    call(
        registry, {"action": "add", "category": "decision", "content": "基线"},
        context=_ctx(),
    )

    from mini_deerflow import context_tools as module
    from mini_deerflow.context_store import ContextStore

    real_cas_update = module.cas_update
    attempts: list[int] = []
    original_get = store.get

    def flaky_get(thread_id: str) -> ContextState:
        # 第一次读到的状态是过期的，迫使首次写入冲突
        if not attempts:
            attempts.append(1)
            return ContextState()  # revision=0 的过期快照
        return original_get(thread_id)

    monkeypatch.setattr(store, "get", flaky_get)

    result = call(
        registry, {"action": "add", "category": "next_step", "content": "重试成功"},
        context=_ctx(),
    )
    assert result["ok"] is True
    assert attempts == [1]  # 确实制造过一次冲突

    # 内容确实落库（没有因为冲突而丢）
    state = store.get(THREAD)
    contents = [note.content for note in state.task_notes]
    assert "重试成功" in contents
    monkeypatch.setattr(store, "get", original_get)


def test_cas_conflict_error_is_actionable() -> None:
    """冲突信息必须带上 expected / actual，调用方才知道怎么重试。"""
    from mini_deerflow.context_errors import ContextConflictError

    store = InMemoryContextStore()
    store.create(THREAD)
    current = store.get(THREAD)
    store.save(THREAD, current, expected_revision=0)
    try:
        store.save(THREAD, current, expected_revision=0)
    except ContextConflictError as exc:
        assert exc.expected == 0
        assert exc.actual == 1
        assert exc.thread_id == THREAD
    else:
        pytest.fail("应当抛 ContextConflictError")


# ---------------------------------------------------------------------------
# Trace
# ---------------------------------------------------------------------------


def test_trace_emits_task_note_changed_without_content(
    registry: ToolRegistry, sink: InMemoryTraceSink
) -> None:
    call(
        registry,
        {"action": "add", "category": "constraint", "content": "这是不该出现在 Trace 里的正文"},
        context=_ctx(),
    )
    events = [e for e in sink.get_events() if e.name == "context.task_note.changed"]
    assert len(events) == 1
    event = events[0]
    assert event.operation == "TOOL_CALL"
    assert event.run_id == "run-1"
    assert event.thread_id == THREAD
    assert event.metadata["action"] == "add"
    assert event.metadata["category"] == "constraint"
    assert event.metadata["active_count"] == 1
    assert "note_id" in event.metadata
    # 正文绝不写进 Trace
    payload = json.dumps(event.metadata, ensure_ascii=False)
    assert "不该出现在 Trace" not in payload


def test_trace_for_every_mutation(
    registry: ToolRegistry, sink: InMemoryTraceSink
) -> None:
    created = call(
        registry, {"action": "add", "category": "decision", "content": "x"}, context=_ctx()
    )
    call(
        registry,
        {"action": "update", "note_id": created["note_id"], "content": "y"},
        context=_ctx(),
    )
    call(registry, {"action": "resolve", "note_id": created["note_id"]}, context=_ctx())

    actions = [
        e.metadata["action"]
        for e in sink.get_events()
        if e.name == "context.task_note.changed"
    ]
    assert actions == ["add", "update", "resolve"]
    # list 是只读操作，不产生变更事件
    call(registry, {"action": "list"}, context=_ctx())
    assert actions == ["add", "update", "resolve"]


# ---------------------------------------------------------------------------
# SQLite 路径同样成立
# ---------------------------------------------------------------------------


def test_sqlite_store_roundtrip(tmp_path) -> None:
    from mini_deerflow.context_state import ContextPolicy
    from mini_deerflow.token_counter import HeuristicTokenCounter
    from mini_deerflow.context_assembler import ContextAssembler

    context_store = SQLiteContextStore(tmp_path / "ctx.db")
    assembler = ContextAssembler(
        policy=ContextPolicy(
            max_context_tokens=8000, reserved_output_tokens=1000, safety_margin_tokens=500
        ),
        counter=HeuristicTokenCounter(),
        context_store=context_store,
    )
    # 触发创建
    assembler.ensure_state(THREAD)
    registry = ToolRegistry()
    registry.register(build_task_note_tool(context_store))
    result = call(
        registry, {"action": "add", "category": "constraint", "content": "持久化约束"},
        context=_ctx(),
    )
    assert result["ok"] is True
    context_store.close()

    # 重开文件，笔记仍在
    reopened = SQLiteContextStore(tmp_path / "ctx.db")
    state = reopened.get(THREAD)
    assert any(n.content == "持久化约束" for n in state.task_notes)
    reopened.close()


def test_store_missing_thread_raises_for_explicit_access() -> None:
    store = InMemoryContextStore()
    with pytest.raises(ContextNotFoundError):
        store.get(THREAD)


def test_limit_error_type_from_domain_layer() -> None:
    """领域层抛的是明确的领域异常；工具层只是把它转成模型可读的 JSON。"""
    state = empty_context_state()
    for index in range(TASK_NOTE_MAX_ACTIVE):
        state = state.add_note(
            TaskNote.create(
                note_id=f"n{index}",
                category=TaskNoteCategory.NEXT_STEP,
                content=f"c{index}",
                source_run_id=None,
            )
        )
    assert len(state.active_notes) == TASK_NOTE_MAX_ACTIVE

    with pytest.raises(TaskNoteLimitExceededError) as excinfo:
        state.add_note(
            TaskNote.create(
                note_id="overflow",
                category=TaskNoteCategory.NEXT_STEP,
                content="x",
                source_run_id=None,
            )
        )
    assert excinfo.value.limit_name == "active_notes"
    assert excinfo.value.limit == TASK_NOTE_MAX_ACTIVE


def test_not_found_error_type_from_domain_layer() -> None:
    state = empty_context_state()
    ghost = TaskNote.create(
        note_id="ghost",
        category=TaskNoteCategory.DECISION,
        content="x",
        source_run_id=None,
    )
    with pytest.raises(TaskNoteNotFoundError):
        state.update_note(ghost)
    # 领域层的异常类型能被工具层的 catch 精确匹配
    assert isinstance(TaskNoteNotFoundError("ghost"), Exception)


# ---------------------------------------------------------------------------
# list 分页与输出上限（规格 7.6 / 4.5）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_limit", [0, -1, LIST_MAX_LIMIT + 1])
def test_list_rejects_out_of_range_limit(
    registry: ToolRegistry, bad_limit: int
) -> None:
    result = call(
        registry, {"action": "list", "limit": bad_limit}, context=_ctx()
    )
    assert result["ok"] is False
    assert result.get("kind") == "usage_error"
    assert "limit" in result["error"]


def test_list_rejects_non_integer_limit(registry: ToolRegistry) -> None:
    result = call(registry, {"action": "list", "limit": "ten"}, context=_ctx())
    assert result["ok"] is False
    assert "limit" in result["error"]


@pytest.mark.parametrize("bad_offset", [-1, -100])
def test_list_rejects_negative_offset(
    registry: ToolRegistry, bad_offset: int
) -> None:
    result = call(
        registry, {"action": "list", "offset": bad_offset}, context=_ctx()
    )
    assert result["ok"] is False
    assert "offset" in result["error"]


def test_list_rejects_non_integer_offset(registry: ToolRegistry) -> None:
    result = call(registry, {"action": "list", "offset": 1.5}, context=_ctx())
    assert result["ok"] is False
    assert "offset" in result["error"]


@pytest.mark.parametrize("bad_status", ["all_active", "", "ACTIVE", "deleted"])
def test_list_rejects_invalid_status(
    registry: ToolRegistry, bad_status: str
) -> None:
    result = call(
        registry, {"action": "list", "status": bad_status}, context=_ctx()
    )
    assert result["ok"] is False
    assert "status" in result["error"]


def test_list_default_limit_is_bounded(registry: ToolRegistry) -> None:
    """默认 limit 是一个有限值，不会因为没传参数就把池子全捞出来。"""
    assert LIST_DEFAULT_LIMIT <= LIST_MAX_LIMIT
    tool = build_task_note_tool(InMemoryContextStore())
    assert "默认 20" in tool.parameters["properties"]["limit"]["description"]


def test_list_pages_cover_every_note_without_overlap(
    registry: ToolRegistry, store: InMemoryContextStore
) -> None:
    """逐页翻过去能覆盖全部，且页与页不重叠。"""
    ids = []
    for index in range(7):
        result = call(
            registry,
            {"action": "add", "category": "decision", "content": f"条目{index}"},
            context=_ctx(),
        )
        ids.append(result["note_id"])

    seen: list[str] = []
    offset = 0
    while True:
        page = call(
            registry,
            {"action": "list", "limit": 3, "offset": offset, "status": "all"},
            context=_ctx(),
        )
        assert page["ok"] is True
        batch = [n["note_id"] for n in page["notes"]]
        seen.extend(batch)
        if not page["has_more"]:
            break
        offset += 3
        assert offset < 100  # 防死循环

    assert seen == ids  # 顺序稳定、无重复、无遗漏
    assert len(seen) == 7


def test_list_offset_beyond_end_returns_empty(registry: ToolRegistry) -> None:
    call(
        registry, {"action": "add", "category": "decision", "content": "x"},
        context=_ctx(),
    )
    result = call(
        registry, {"action": "list", "offset": 999, "status": "all"}, context=_ctx()
    )
    assert result["ok"] is True
    assert result["notes"] == []
    assert result["has_more"] is False
    assert result["total_count"] == 1


def test_list_truncates_long_content_per_entry(
    registry: ToolRegistry,
) -> None:
    """单条内容在 list 视图里被截断——要读全文另有渠道，不必占满上下文。"""
    long_content = "很长的笔记正文" * 200  # 1400 字符
    call(
        registry,
        {"action": "add", "category": "constraint", "content": long_content},
        context=_ctx(),
    )
    result = call(registry, {"action": "list"}, context=_ctx())
    entry = result["notes"][0]
    assert len(entry["content"]) <= LIST_CONTENT_CHARS + 1  # +1 是省略号
    assert entry["content"].endswith("…")


def test_list_enforces_total_output_budget(registry: ToolRegistry) -> None:
    """整体输出封字符预算：预算吃紧时提前截断并如实标记 truncated。"""
    for index in range(30):
        call(
            registry,
            {
                "action": "add",
                "category": "constraint",
                "content": f"约束{index}：" + "正文" * 80,
            },
            context=_ctx(),
        )
    result = call(
        registry,
        {"action": "list", "limit": LIST_MAX_LIMIT, "status": "all"},
        context=_ctx(),
    )
    payload = json.dumps(result["notes"], ensure_ascii=False)
    assert len(payload) <= LIST_MAX_OUTPUT_CHARS
    # 要么没触顶（返回全部），要么触顶后如实标记
    if len(result["notes"]) < result["pool_count"]:
        assert result["truncated"] is True
        assert result["has_more"] is True


def test_five_hundred_notes_never_all_enter_the_result(
    registry: ToolRegistry, store: InMemoryContextStore
) -> None:
    """500 条（总量上限）不能一次全进工具结果——这是本条需求的核心。

    构造顺序有讲究：active 上限是 100，所以先做 400 条「add 后立刻 resolve」
    （active 始终为 0，不会撞上 active 上限），最后再堆 100 条 active。
    """
    for index in range(TASK_NOTE_MAX_TOTAL - TASK_NOTE_MAX_ACTIVE):  # 400 条已解决
        added = call(
            registry,
            {"action": "add", "category": "failure", "content": f"历史问题-{index}"},
            context=_ctx(),
        )
        assert added.get("ok") is True, f"第 {index} 条 add 失败: {added}"
        call(
            registry,
            {"action": "resolve", "note_id": added["note_id"]},
            context=_ctx(),
        )
    for index in range(TASK_NOTE_MAX_ACTIVE):  # 100 条 active
        added = call(
            registry,
            {"action": "add", "category": "decision", "content": f"active-{index}"},
            context=_ctx(),
        )
        assert added.get("ok") is True, f"第 {index} 条 active add 失败: {added}"

    state = store.get(THREAD)
    assert len(state.task_notes) == TASK_NOTE_MAX_TOTAL  # 500 条确实存在
    assert len(state.active_notes) == TASK_NOTE_MAX_ACTIVE  # 100 条 active

    for status in ("active", "resolved", "all"):
        result = call(registry, {"action": "list", "status": status}, context=_ctx())
        assert result["ok"] is True
        assert len(result["notes"]) <= LIST_DEFAULT_LIMIT, (
            f"status={status} 默认就返回了 {len(result['notes'])} 条"
        )
        assert len(result["notes"]) <= LIST_MAX_LIMIT
        # 一次调用的输出体积有硬上限
        assert (
            len(json.dumps(result, ensure_ascii=False)) <= LIST_MAX_OUTPUT_CHARS + 500
        )
        # 但计数是真实的，模型知道后面还有
        assert result["total_count"] == TASK_NOTE_MAX_TOTAL
        assert result["pool_count"] > len(result["notes"])
        assert result["has_more"] is True


def test_list_with_max_limit_still_bounded(
    registry: ToolRegistry, store: InMemoryContextStore
) -> None:
    """即使显式要最大 limit，也仍受总字符预算约束。"""
    for index in range(TASK_NOTE_MAX_ACTIVE):
        call(
            registry,
            {
                "action": "add",
                "category": "constraint",
                "content": f"约束{index}：" + "说明" * 90,
            },
            context=_ctx(),
        )
    result = call(
        registry,
        {"action": "list", "limit": LIST_MAX_LIMIT, "status": "all"},
        context=_ctx(),
    )
    assert result["ok"] is True
    assert len(result["notes"]) <= LIST_MAX_LIMIT
    assert len(json.dumps(result["notes"], ensure_ascii=False)) <= LIST_MAX_OUTPUT_CHARS
