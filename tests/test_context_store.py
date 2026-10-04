"""Phase 24 / 7.2 ContextStore。

覆盖规格 7.2：InMemory CRUD、SQLite 重启恢复、revision CAS、并发冲突、
删除线程时清理、损坏数据、schema 迁移。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from mini_deerflow.context_errors import (
    ContextConflictError,
    ContextDataCorruptedError,
    ContextNotFoundError,
    ContextSchemaVersionError,
    TaskNoteValidationError,
)
from mini_deerflow.context_state import (
    ConversationSummary,
    ContextState,
    TaskNote,
    TaskNoteCategory,
)
from mini_deerflow.context_store import (
    MIGRATIONS,
    ContextStore,
    InMemoryContextStore,
    SQLiteContextStore,
    cas_update,
)

THREAD = "thread-1"


def _note(note_id: str = "n1") -> TaskNote:
    return TaskNote.create(
        note_id=note_id,
        category=TaskNoteCategory.NEXT_STEP,
        content=f"内容 {note_id}",
        source_run_id="run-1",
    )


def _with_summary(state: ContextState) -> ContextState:
    summary = ConversationSummary.build(
        narrative="早期讨论",
        facts=["事实A"],
        summarized_from=0,
        summarized_through=3,
    )
    from dataclasses import replace

    return replace(state, summary=summary, summarized_through=3)


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path):
    if request.param == "memory":
        yield InMemoryContextStore()
    else:
        path = tmp_path / "ctx.db"
        handle = SQLiteContextStore(path)
        yield handle
        handle.close()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_missing_thread_raises_not_found(store: ContextStore) -> None:
    with pytest.raises(ContextNotFoundError):
        store.get("nope")


def test_create_get_and_exists(store: ContextStore) -> None:
    assert store.exists(THREAD) is False
    store.create(THREAD)
    assert store.exists(THREAD) is True
    state = store.get(THREAD)
    assert state.revision == 0
    assert state.summary is None


def test_create_twice_conflicts(store: ContextStore) -> None:
    store.create(THREAD)
    with pytest.raises(ContextConflictError):
        store.create(THREAD)


def test_save_returns_incremented_revision(store: ContextStore) -> None:
    store.create(THREAD)
    current = store.get(THREAD)
    written = store.save(THREAD, current.add_note(_note()), expected_revision=current.revision)
    assert written.revision == current.revision + 1
    assert store.get(THREAD).revision == written.revision


def test_save_requires_existing_thread(store: ContextStore) -> None:
    with pytest.raises(ContextNotFoundError):
        store.save("ghost", ContextState(), expected_revision=0)


def test_save_rejects_wrong_type(store: ContextStore) -> None:
    store.create(THREAD)
    with pytest.raises(TypeError):
        store.save(THREAD, "not-a-state", expected_revision=0)  # type: ignore[arg-type]


def test_delete_is_idempotent(store: ContextStore) -> None:
    store.create(THREAD)
    store.delete(THREAD)
    assert store.exists(THREAD) is False
    store.delete(THREAD)  # 第二次不抛
    assert store.exists(THREAD) is False


def test_delete_missing_thread_is_silent(store: ContextStore) -> None:
    store.delete("never-existed")


# ---------------------------------------------------------------------------
# CAS
# ---------------------------------------------------------------------------


def test_save_with_stale_revision_conflicts(store: ContextStore) -> None:
    store.create(THREAD)
    stale = store.get(THREAD)
    store.save(THREAD, stale, expected_revision=stale.revision)  # rev 0 → 1

    with pytest.raises(ContextConflictError) as excinfo:
        store.save(THREAD, stale, expected_revision=stale.revision)  # 仍以为是 0
    assert excinfo.value.expected == 0
    assert excinfo.value.actual == 1
    assert excinfo.value.thread_id == THREAD


def test_save_with_wrong_explicit_revision_conflicts(store: ContextStore) -> None:
    store.create(THREAD)
    current = store.get(THREAD)
    with pytest.raises(ContextConflictError):
        store.save(THREAD, current, expected_revision=99)


def test_error_message_is_actionable(store: ContextStore) -> None:
    store.create(THREAD)
    current = store.get(THREAD)  # revision 0
    store.save(THREAD, current, expected_revision=0)  # → revision 1
    try:
        store.save(THREAD, current, expected_revision=0)
    except ContextConflictError as exc:
        message = str(exc)
        assert THREAD in message
        assert "0" in message  # expected
        assert "1" in message  # actual，写入已被推进
    else:
        pytest.fail("应当抛 ContextConflictError")


def test_cas_update_retries_on_conflict() -> None:
    store = InMemoryContextStore()
    store.create(THREAD)
    store.save(THREAD, store.get(THREAD), expected_revision=0)  # rev → 1

    calls: list[int] = []

    def mutator(state: ContextState) -> ContextState:
        calls.append(state.revision)
        # 每次都基于「当前读到的版本」构造，因此第一次就会成功
        return state.add_note(_note(f"n{len(calls)}"))

    result = cas_update(store, THREAD, mutator)
    assert result.revision == 2
    assert len(calls) == 1


def test_cas_update_exhausts_retries_and_raises() -> None:
    """mutator 每次都返回同一个过期版本 → 必须重试耗尽后抛错，不返回脏数据。"""
    store = InMemoryContextStore()
    store.create(THREAD)
    stale = ContextState()  # revision=0，而库里已经是 1
    store.save(THREAD, stale, expected_revision=0)

    attempts: list[int] = []

    def mutator(state: ContextState) -> ContextState:
        attempts.append(state.revision)
        return stale  # 故意不跟随最新版本

    with pytest.raises(ContextConflictError):
        cas_update(store, THREAD, mutator, max_retries=3)
    # 1 次读取 + 首次写尝试，之后每次冲突都要重读
    assert len(attempts) == 4
    # store 没被写脏
    assert store.get(THREAD).revision == 1


def test_cas_update_rejects_negative_retries() -> None:
    store = InMemoryContextStore()
    store.create(THREAD)
    with pytest.raises(ValueError, match="max_retries"):
        cas_update(store, THREAD, lambda s: s, max_retries=-1)


def test_cas_update_rejects_non_state_return() -> None:
    store = InMemoryContextStore()
    store.create(THREAD)
    with pytest.raises(TypeError, match="ContextState"):
        cas_update(store, THREAD, lambda s: "nope")  # type: ignore[return-value]


def test_cas_update_on_missing_thread_raises() -> None:
    store = InMemoryContextStore()
    with pytest.raises(ContextNotFoundError):
        cas_update(store, "ghost", lambda s: s)


def test_concurrent_cas_writers_never_overwrite() -> None:
    """两个线程同时改一份状态：绝不允许「后写者赢」。"""
    store = InMemoryContextStore()
    store.create(THREAD)
    errors: list[Exception] = []
    successes: list[ContextState] = []
    barrier = threading.Barrier(8)

    def writer(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            result = cas_update(
                store,
                THREAD,
                lambda state: state.add_note(_note(f"note-{index}")),
                max_retries=50,
            )
            successes.append(result)
        except Exception as exc:  # noqa: BLE001 - 收集断言
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors, f"并发写应当全部通过重试成功: {errors}"
    final = store.get(THREAD)
    # 8 次成功写入 → revision 从 0 涨到 8，8 条 Note 全在
    assert final.revision == 8
    assert len(final.task_notes) == 8
    assert {n.note_id for n in final.task_notes} == {f"note-{i}" for i in range(8)}


# ---------------------------------------------------------------------------
# SQLite 专属：重启恢复 / 损坏 / 迁移
# ---------------------------------------------------------------------------


def test_sqlite_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "ctx.db"
    first = SQLiteContextStore(path)
    first.create(THREAD)
    state = _with_summary(first.get(THREAD)).add_note(_note("n1"))
    saved = first.save(THREAD, state, expected_revision=state.revision)
    first.close()

    reopened = SQLiteContextStore(path)
    restored = reopened.get(THREAD)
    assert restored.revision == saved.revision
    assert restored.summary is not None
    assert restored.summary.narrative == "早期讨论"
    assert restored.summarized_through == 3
    assert len(restored.task_notes) == 1
    reopened.close()


def test_sqlite_uses_dedicated_table(tmp_path: Path) -> None:
    """ContextState 必须独立成表，不能塞回 ThreadState 的 JSON。"""
    path = tmp_path / "ctx.db"
    handle = SQLiteContextStore(path)
    handle.create(THREAD)
    connection = sqlite3.connect(path)
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(context_states)")
    }
    connection.close()
    handle.close()

    assert "context_states" in tables
    # 事实源表由 SQLiteThreadStore 建；这里只要求投影表存在且字段齐全
    assert {"thread_id", "revision", "schema_version", "payload", "updated_at"} <= columns


def test_sqlite_corrupted_payload_raises(tmp_path: Path) -> None:
    path = tmp_path / "ctx.db"
    handle = SQLiteContextStore(path)
    handle.create(THREAD)
    handle.close()

    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE context_states SET payload = 'not-json' WHERE thread_id = ?", (THREAD,)
    )
    connection.commit()
    connection.close()

    reopened = SQLiteContextStore(path)
    with pytest.raises(ContextDataCorruptedError):
        reopened.get(THREAD)
    reopened.close()


def test_sqlite_non_object_payload_raises(tmp_path: Path) -> None:
    path = tmp_path / "ctx.db"
    handle = SQLiteContextStore(path)
    handle.create(THREAD)
    handle.close()

    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE context_states SET payload = '[1,2,3]' WHERE thread_id = ?", (THREAD,)
    )
    connection.commit()
    connection.close()

    reopened = SQLiteContextStore(path)
    with pytest.raises(ContextDataCorruptedError, match="字典"):
        reopened.get(THREAD)
    reopened.close()


def test_sqlite_unknown_schema_version_raises(tmp_path: Path) -> None:
    path = tmp_path / "ctx.db"
    handle = SQLiteContextStore(path)
    handle.create(THREAD)
    handle.close()

    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE context_states SET payload = ? WHERE thread_id = ?",
        ('{"schema_version": 99}', THREAD),
    )
    connection.commit()
    connection.close()

    reopened = SQLiteContextStore(path)
    with pytest.raises(ContextSchemaVersionError) as excinfo:
        reopened.get(THREAD)
    assert excinfo.value.found == 99
    assert excinfo.value.supported == 1
    reopened.close()


def test_sqlite_invalid_schema_version_type_raises(tmp_path: Path) -> None:
    path = tmp_path / "ctx.db"
    handle = SQLiteContextStore(path)
    handle.create(THREAD)
    handle.close()

    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE context_states SET payload = ? WHERE thread_id = ?",
        ('{"schema_version": "one"}', THREAD),
    )
    connection.commit()
    connection.close()

    reopened = SQLiteContextStore(path)
    with pytest.raises(ContextDataCorruptedError):
        reopened.get(THREAD)
    reopened.close()


def test_sqlite_schema_migration_applies_registered_step(tmp_path: Path, monkeypatch) -> None:
    """注册一条 v1 → v2 迁移后，旧版本 payload 能被升到当前版本读出。"""
    path = tmp_path / "ctx.db"
    handle = SQLiteContextStore(path)
    handle.create(THREAD)
    handle.close()

    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE context_states SET payload = ? WHERE thread_id = ?",
        ('{"schema_version": 0, "revision": 3, "task_notes": []}', THREAD),
    )
    connection.commit()
    connection.close()

    def _to_v1(data: dict) -> dict:
        upgraded = dict(data)
        upgraded["schema_version"] = 1
        upgraded.setdefault("compaction_count", 0)
        upgraded.setdefault("summarized_through", None)
        upgraded.setdefault("indexed_through", None)
        upgraded.setdefault("summary", None)
        return upgraded

    monkeypatch.setitem(MIGRATIONS, 0, _to_v1)

    reopened = SQLiteContextStore(path)
    restored = reopened.get(THREAD)
    assert restored.revision == 3
    assert restored.compaction_count == 0
    reopened.close()


def test_sqlite_migration_not_registered_raises(tmp_path: Path) -> None:
    path = tmp_path / "ctx.db"
    handle = SQLiteContextStore(path)
    handle.create(THREAD)
    handle.close()

    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE context_states SET payload = ? WHERE thread_id = ?",
        ('{"schema_version": 0, "revision": 1}', THREAD),
    )
    connection.commit()
    connection.close()

    reopened = SQLiteContextStore(path)
    # MIGRATIONS 默认为空表 → 缺少 v0→v1 迁移时必须明确拒绝
    with pytest.raises(ContextSchemaVersionError) as excinfo:
        reopened.get(THREAD)
    assert excinfo.value.found == 0
    reopened.close()


def test_sqlite_close_is_idempotent(tmp_path: Path) -> None:
    handle = SQLiteContextStore(tmp_path / "ctx.db")
    handle.close()
    handle.close()
    with pytest.raises(ContextNotFoundError, match="已关闭"):
        handle.get(THREAD)


def test_sqlite_create_after_close_raises(tmp_path: Path) -> None:
    handle = SQLiteContextStore(tmp_path / "ctx.db")
    handle.close()
    with pytest.raises(ContextNotFoundError, match="已关闭"):
        handle.create(THREAD)


# ---------------------------------------------------------------------------
# 线程删除 → 投影同步回收
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("factory", [InMemoryContextStore, SQLiteContextStore])
def test_thread_deletion_also_drops_projections(tmp_path: Path, factory) -> None:
    """删除线程必须同步删 ContextState，否则留下指向已消失线程的孤儿数据。"""
    from mini_deerflow.context_assembler import ContextAssembler
    from mini_deerflow.context_state import ContextPolicy
    from mini_deerflow.token_counter import HeuristicTokenCounter

    if factory is SQLiteContextStore:
        context_store = factory(tmp_path / "ctx.db")
    else:
        context_store = factory()
    context_store.create(THREAD)
    state = context_store.get(THREAD).add_note(_note("keep"))
    context_store.save(THREAD, state, expected_revision=state.revision)
    assert context_store.exists(THREAD)

    assembler = ContextAssembler(
        policy=ContextPolicy(
            max_context_tokens=8000,
            reserved_output_tokens=1000,
            safety_margin_tokens=500,
        ),
        counter=HeuristicTokenCounter(),
        context_store=context_store,
    )

    assembler.drop_thread_projections(THREAD)
    assert context_store.exists(THREAD) is False
    # 幂等：重复删除不抛
    assembler.drop_thread_projections(THREAD)


def test_drop_projections_also_clears_vector_index(tmp_path: Path) -> None:
    from mini_deerflow.context_assembler import ContextAssembler
    from mini_deerflow.context_state import ContextPolicy
    from mini_deerflow.history_index import (
        HistoryIndexService,
        HistoryVectorIndex,
        LocalHashEmbeddingProvider,
    )
    from mini_deerflow.models import Message
    from mini_deerflow.thread_store import InMemoryThreadStore
    from mini_deerflow.token_counter import HeuristicTokenCounter

    thread_store = InMemoryThreadStore()
    thread_store.create(THREAD)
    thread = thread_store.get(THREAD)
    thread.append(Message(role="user", content="部署失败"))
    thread_store.save(thread)

    context_store = SQLiteContextStore(tmp_path / "ctx.db")
    context_store.create(THREAD)

    service = HistoryIndexService(
        index=HistoryVectorIndex(LocalHashEmbeddingProvider(dimensions=32)),
        thread_store=thread_store,
        context_store=context_store,
    )
    service.sync(THREAD)
    assert service.index.size(THREAD) == 1
    assert context_store.get(THREAD).indexed_through == 0

    assembler = ContextAssembler(
        policy=ContextPolicy(
            max_context_tokens=8000,
            reserved_output_tokens=1000,
            safety_margin_tokens=500,
        ),
        counter=HeuristicTokenCounter(),
        context_store=context_store,
        history_service=service,
    )

    assembler.drop_thread_projections(THREAD)
    assert service.index.size(THREAD) == 0
    assert context_store.exists(THREAD) is False
    # 事实源不受影响
    assert thread_store.exists(THREAD) is True


def test_duplicate_note_rejected_before_reaching_store() -> None:
    """领域层就拦下重复 Note，store 不会被写入半成品。"""
    store = InMemoryContextStore()
    store.create(THREAD)
    state = store.get(THREAD).add_note(_note("dup"))
    saved = store.save(THREAD, state, expected_revision=state.revision)
    with pytest.raises(TaskNoteValidationError):
        saved.add_note(_note("dup"))
