"""Phase 24 / 风险 5：生命周期一致性。

覆盖：
- Runtime shutdown 正确关闭 ContextStore 并清空向量索引
- Thread 删除后事实、ContextState、向量索引**三者全部**清理
- 任一步清理失败：有日志，但**不返回成功**冒充全清
- SQLite 重启后 ContextState 与 History Search 行为一致
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from mini_deerflow.api.services.threads import ThreadNotFoundError, ThreadService
from mini_deerflow.context_assembler import ContextAssembler
from mini_deerflow.context_state import (
    ContextPolicy,
    ContextState,
    TaskNote,
    TaskNoteCategory,
)
from mini_deerflow.context_store import InMemoryContextStore, SQLiteContextStore
from mini_deerflow.history_index import (
    HistoryIndexService,
    HistoryVectorIndex,
    LocalHashEmbeddingProvider,
)
from mini_deerflow.models import Message
from mini_deerflow.runtime import RuntimeConfig, create_runtime
from mini_deerflow.thread_store import InMemoryThreadStore, SQLiteThreadStore
from mini_deerflow.token_counter import HeuristicTokenCounter
from mini_deerflow.tools import ToolRegistry

THREAD = "thread-a"


def _policy() -> ContextPolicy:
    return ContextPolicy(
        max_context_tokens=8000,
        reserved_output_tokens=1000,
        safety_margin_tokens=500,
    )


def _assembler(
    context_store, thread_store, *, trace_sink=None
) -> ContextAssembler:
    return ContextAssembler(
        policy=_policy(),
        counter=HeuristicTokenCounter(),
        context_store=context_store,
        trace_sink=trace_sink,
        history_service=HistoryIndexService(
            index=HistoryVectorIndex(LocalHashEmbeddingProvider(dimensions=32)),
            thread_store=thread_store,
            context_store=context_store,
            trace_sink=trace_sink,
        ),
    )


def _seed(thread_store, context_store, assembler, thread_id: str = THREAD) -> None:
    """建线程 + 写一条 Note + 建索引，三处都留痕，方便验证是否全清。"""
    if not thread_store.exists(thread_id):
        thread_store.create(thread_id)
    thread = thread_store.get(thread_id)
    thread.append(Message(role="user", content="部署到生产环境失败"))
    thread_store.save(thread)

    assembler.ensure_state(thread_id)
    note = TaskNote.create(
        note_id="n1",
        category=TaskNoteCategory.CONSTRAINT,
        content="必须离线测试",
        source_run_id="run-1",
    )
    from mini_deerflow.context_store import cas_update

    cas_update(context_store, thread_id, lambda state: state.add_note(note))
    assembler.history_service.sync(thread_id, run_id="seed")


# ---------------------------------------------------------------------------
# 1. Runtime shutdown
# ---------------------------------------------------------------------------


def test_runtime_shutdown_closes_context_store_and_clears_index() -> None:
    runtime = create_runtime(
        store=InMemoryThreadStore(),
        registry=ToolRegistry(),
        config=RuntimeConfig(context_enabled=True),
    )
    assembler = runtime.context_assembler
    assert assembler is not None
    _seed(runtime.store, assembler.context_store, assembler)

    assert assembler.context_store.exists(THREAD)
    assert assembler.history_service.index.size(THREAD) == 1

    runtime.shutdown()

    # ContextStore 已关闭：再访问必须明确失败，而不是静默返回旧数据
    with pytest.raises(Exception, match="已关闭|不存在"):
        assembler.context_store.get(THREAD)
    # 向量索引已清空
    assert assembler.history_service.index.size(THREAD) == 0


def test_runtime_shutdown_is_idempotent_with_context() -> None:
    runtime = create_runtime(
        store=InMemoryThreadStore(),
        registry=ToolRegistry(),
        config=RuntimeConfig(context_enabled=True),
    )
    runtime.shutdown()
    runtime.shutdown()  # 第二次不得抛


def test_runtime_shutdown_without_context_is_safe() -> None:
    """context_enabled=False 时 shutdown 不应触碰 None。"""
    runtime = create_runtime(
        store=InMemoryThreadStore(),
        registry=ToolRegistry(),
        config=RuntimeConfig(context_enabled=False),
    )
    assert runtime.context_store is None
    assert runtime.context_assembler is None
    runtime.shutdown()


# ---------------------------------------------------------------------------
# 2. Thread 删除 → 三者全清
# ---------------------------------------------------------------------------


def test_thread_deletion_clears_all_three_layers() -> None:
    thread_store = InMemoryThreadStore()
    context_store = InMemoryContextStore()
    assembler = _assembler(context_store, thread_store)
    _seed(thread_store, context_store, assembler)

    service = ThreadService(thread_store, assembler)
    service.delete(THREAD)

    # 事实、投影、向量索引三者都不剩
    assert thread_store.exists(THREAD) is False
    assert context_store.exists(THREAD) is False
    assert assembler.history_service.index.size(THREAD) == 0


def test_thread_deletion_leaves_other_threads_untouched() -> None:
    thread_store = InMemoryThreadStore()
    context_store = InMemoryContextStore()
    assembler = _assembler(context_store, thread_store)
    _seed(thread_store, context_store, assembler)
    _seed(thread_store, context_store, assembler, thread_id="other")

    ThreadService(thread_store, assembler).delete(THREAD)

    assert thread_store.exists("other") is True
    assert context_store.exists("other") is True
    assert assembler.history_service.index.size("other") == 1


def test_thread_deletion_without_assembler_only_removes_fact() -> None:
    """没接上下文层时（context_enabled=False），删除只清事实源，且不报错。"""
    thread_store = InMemoryThreadStore()
    thread_store.create(THREAD)
    ThreadService(thread_store, None).delete(THREAD)
    assert thread_store.exists(THREAD) is False


def test_thread_deletion_of_unknown_thread_raises() -> None:
    thread_store = InMemoryThreadStore()
    with pytest.raises(ThreadNotFoundError):
        ThreadService(thread_store, None).delete("ghost")


# ---------------------------------------------------------------------------
# 3. 清理失败：有日志，但不返回成功
# ---------------------------------------------------------------------------


class _ExplodingAssembler:
    """drop 时抛错的替身——用来证明失败不会被吞成成功。"""

    context_store = None
    history_service = None

    def drop_thread_projections(self, thread_id: str) -> None:
        raise RuntimeError("投影清理失败")


def test_projection_cleanup_failure_does_not_succeed_silently(
    caplog: pytest.LogCaptureFixture,
) -> None:
    thread_store = InMemoryThreadStore()
    thread_store.create(THREAD)

    with caplog.at_level(logging.ERROR, logger="mini_deerflow.api.services.threads"):
        with pytest.raises(Exception) as excinfo:
            ThreadService(thread_store, _ExplodingAssembler()).delete(THREAD)

    # 对外是明确失败，而不是 204/无异常
    assert "ThreadServiceError" in type(excinfo.value).__name__
    assert "已删除" in str(excinfo.value)
    assert "清理未完成" in str(excinfo.value)
    # 日志里留了完整细节（异常类型 + 栈）
    assert any(
        "上下文投影清理失败" in record.getMessage()
        for record in caplog.records
    ), "失败必须落日志"
    error_records = [
        r for r in caplog.records if r.levelno >= logging.ERROR
    ]
    assert error_records, "必须是 error 级别而不是被降级成 warning"
    assert error_records[0].exc_info is not None, "必须带异常栈"
    # 事实源确实已删（这条不能因为投影失败而假装没发生）
    assert thread_store.exists(THREAD) is False


def test_projection_cleanup_failure_message_leaks_no_internals() -> None:
    """对外文案不含绝对路径、异常类名以外的内部细节。"""
    thread_store = InMemoryThreadStore()
    thread_store.create(THREAD)
    try:
        ThreadService(thread_store, _ExplodingAssembler()).delete(THREAD)
    except Exception as exc:
        message = str(exc)
        assert "Traceback" not in message
        assert "\\" not in message or "线程" in message  # 不带 Windows 绝对路径
        assert "RuntimeError" not in message  # 内部异常类型不外泄
    else:
        pytest.fail("应当抛错")


def test_successful_cleanup_returns_normally() -> None:
    """清理成功时正常返回，不产生多余异常。"""
    thread_store = InMemoryThreadStore()
    context_store = InMemoryContextStore()
    assembler = _assembler(context_store, thread_store)
    _seed(thread_store, context_store, assembler)
    ThreadService(thread_store, assembler).delete(THREAD)  # 不应抛


# ---------------------------------------------------------------------------
# 4. SQLite 重启：ContextState 与 History Search 行为一致
# ---------------------------------------------------------------------------


def test_sqlite_restart_keeps_context_state_and_history_search(tmp_path: Path) -> None:
    thread_db = tmp_path / "threads.db"
    context_db = tmp_path / "context.db"

    # ---- 第一次运行 ----
    ts1 = SQLiteThreadStore(thread_db)
    cs1 = SQLiteContextStore(context_db)
    assembler1 = _assembler(cs1, ts1)
    _seed(ts1, cs1, assembler1)
    state_before = cs1.get(THREAD)
    hits_before = assembler1.history_service.search(
        THREAD, "部署 生产", top_k=3, run_id="run-1"
    )
    assert hits_before
    ts1.close()
    cs1.close()

    # ---- 重启：同一份 SQLite ----
    ts2 = SQLiteThreadStore(thread_db)
    cs2 = SQLiteContextStore(context_db)
    assembler2 = _assembler(cs2, ts2)

    # ContextState 完整保留
    state_after = cs2.get(THREAD)
    assert state_after.revision == state_before.revision
    assert len(state_after.task_notes) == len(state_before.task_notes)
    assert state_after.task_notes[0].content == "必须离线测试"
    assert state_after.indexed_through == state_before.indexed_through

    # 向量索引是空的，但 history_search 必须自己补回来
    assert assembler2.history_service.index.size(THREAD) == 0
    hits_after = assembler2.history_service.search(
        THREAD, "部署 生产", top_k=3, run_id="run-2"
    )
    assert hits_after, "重启后检索行为必须与重启前一致"
    assert {h.reference for h in hits_after} == {h.reference for h in hits_before}
    assert assembler2.history_service.index.size(THREAD) == 1

    # Task Note 也照常可用
    assert state_after.find_note("n1") is not None
    ts2.close()
    cs2.close()


def test_sqlite_restart_then_thread_deletion_clears_everything(tmp_path: Path) -> None:
    """重启后走删除路径，三处仍能一起清干净。"""
    thread_db = tmp_path / "threads.db"
    context_db = tmp_path / "context.db"

    ts1 = SQLiteThreadStore(thread_db)
    cs1 = SQLiteContextStore(context_db)
    assembler1 = _assembler(cs1, ts1)
    _seed(ts1, cs1, assembler1)
    ts1.close()
    cs1.close()

    ts2 = SQLiteThreadStore(thread_db)
    cs2 = SQLiteContextStore(context_db)
    assembler2 = _assembler(cs2, ts2)
    # 重启后先触发一次检索（把索引建起来）
    assembler2.history_service.search(THREAD, "部署", top_k=2, run_id="r")
    assert assembler2.history_service.index.size(THREAD) == 1

    ThreadService(ts2, assembler2).delete(THREAD)

    assert ts2.exists(THREAD) is False
    assert cs2.exists(THREAD) is False
    assert assembler2.history_service.index.size(THREAD) == 0
    ts2.close()
    cs2.close()


def test_shutdown_after_sqlite_restart_releases_resources(tmp_path: Path) -> None:
    """真实 runtime 在 SQLite 配置下，shutdown 后 store 关闭、索引清空。"""
    thread_db = tmp_path / "threads.db"
    context_db = tmp_path / "context.db"

    runtime = create_runtime(
        store=SQLiteThreadStore(thread_db),
        registry=ToolRegistry(),
        config=RuntimeConfig(
            context_enabled=True,
            context_db_path=str(context_db),
        ),
    )
    assembler = runtime.context_assembler
    assert assembler is not None
    _seed(runtime.store, assembler.context_store, assembler)
    assembler.history_service.search(THREAD, "部署", top_k=1, run_id="r")
    assert assembler.history_service.index.size(THREAD) == 1

    runtime.shutdown()

    assert assembler.history_service.index.size(THREAD) == 0
    with pytest.raises(Exception, match="已关闭|不存在"):
        assembler.context_store.get(THREAD)


def test_context_enabled_false_wires_nothing() -> None:
    from mini_deerflow.tools import create_default_registry

    runtime = create_runtime(
        store=InMemoryThreadStore(),
        registry=create_default_registry(),
        config=RuntimeConfig(context_enabled=False),
    )
    # 关闭时既不建 store，也不注册三个 context 工具
    assert runtime.context_store is None
    assert runtime.context_assembler is None
    names = {tool.name for tool in runtime.agent._registry.list_tools()}
    assert "task_note" not in names
    assert "history_search" not in names
    assert "history_read" not in names
    # 既有工具不受影响
    assert "calculator" in names
    runtime.shutdown()


def test_context_enabled_registers_the_three_tools() -> None:
    from mini_deerflow.tools import create_default_registry

    runtime = create_runtime(
        store=InMemoryThreadStore(),
        registry=create_default_registry(),
        config=RuntimeConfig(context_enabled=True),
    )
    names = {tool.name for tool in runtime.agent._registry.list_tools()}
    assert {"task_note", "history_search", "history_read"} <= names
    assert "calculator" in names
    # Agent 确实挂上了统一入口
    assert runtime.context_assembler is runtime.agent._context_assembler
    runtime.shutdown()
