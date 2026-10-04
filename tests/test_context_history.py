"""Phase 24 / 7.7 History Search。

覆盖规格 7.7：同 Thread 召回、不同 Thread 完全隔离、upsert 幂等、
索引失败不回滚消息、水位落后时延迟补齐、history_read 邻近窗口、
参数上限、不可信内容边界、离线确定性。

**不声称语义检索质量**：默认实现是词袋哈希，只在词面重合时相似。
测试只验证隔离、幂等、水位与边界，不验证「找得准」。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mini_deerflow.context_errors import (
    HistoryIndexLagError,
    HistoryReferenceError,
)
from mini_deerflow.context_state import ContextPolicy
from mini_deerflow.context_store import InMemoryContextStore, SQLiteContextStore
from mini_deerflow.history_index import (
    DEFAULT_EMBEDDING_DIMENSIONS,
    HISTORY_SEARCH_LIMITS,
    HistoryIndexService,
    HistoryVectorIndex,
    LocalHashEmbeddingProvider,
)
from mini_deerflow.history_tools import (
    HISTORY_NOTICE,
    build_history_read_tool,
    build_history_search_tool,
)
from mini_deerflow.middleware import ExecutionContext, InMemoryTraceSink, OperationType
from mini_deerflow.models import Message
from mini_deerflow.thread_store import InMemoryThreadStore, SQLiteThreadStore
from mini_deerflow.tools import ToolExecutionError, ToolRegistry

THREAD_A = "thread-a"
THREAD_B = "thread-b"

CORPUS_A = [
    ("user", "部署到生产环境时报 502，上游 Nginx 返回 bad gateway"),
    ("assistant", "先看 Nginx 错误日志，确认是 upstream 连接失败"),
    ("user", "日志显示 upstream timed out"),
    ("assistant", "把 proxy_read_timeout 从 60s 调到 180s 后重试"),
    ("user", "还是失败，回滚到上一个版本"),
]
CORPUS_B = [
    ("user", "设计登录页的配色方案"),
    ("assistant", "主色用蓝色，辅色用浅灰，强调色用橙色"),
]


def _build(
    *,
    trace_sink: InMemoryTraceSink | None = None,
    dimensions: int = 64,
) -> tuple[HistoryIndexService, InMemoryThreadStore, InMemoryContextStore]:
    thread_store = InMemoryThreadStore()
    context_store = InMemoryContextStore()
    index = HistoryVectorIndex(LocalHashEmbeddingProvider(dimensions=dimensions))
    service = HistoryIndexService(
        index=index,
        thread_store=thread_store,
        context_store=context_store,
        trace_sink=trace_sink,
    )
    for thread_id, corpus in ((THREAD_A, CORPUS_A), (THREAD_B, CORPUS_B)):
        thread_store.create(thread_id)
        context_store.create(thread_id)
        thread = thread_store.get(thread_id)
        for role, content in corpus:
            thread.append(Message(role=role, content=content))
        thread_store.save(thread)
    return service, thread_store, context_store


def _ctx(thread_id: str = THREAD_A, run_id: str = "run-1") -> ExecutionContext:
    return ExecutionContext(
        run_id=run_id,
        thread_id=thread_id,
        operation=OperationType.TOOL_CALL,
        operation_name="history_search",
    )


def _registry(service: HistoryIndexService) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(build_history_search_tool(service))
    registry.register(build_history_read_tool(service))
    return registry


def _call(registry: ToolRegistry, tool: str, arguments: dict, context=None) -> dict:
    return json.loads(registry.execute(tool, arguments, context=context))


# ---------------------------------------------------------------------------
# 离线确定性向量
# ---------------------------------------------------------------------------


def test_embedding_is_offline_and_deterministic() -> None:
    provider = LocalHashEmbeddingProvider(dimensions=32)
    first = provider.embed("部署到生产环境")
    second = provider.embed("部署到生产环境")
    assert first == second
    assert len(first) == 32


def test_embedding_does_not_depend_on_python_hash_seed() -> None:
    """必须用 md5 而不是内置 hash()：后者受 PYTHONHASHSEED 影响。"""
    import hashlib

    token = "部署"
    digest = hashlib.md5(token.encode("utf-8")).digest()
    expected = int.from_bytes(digest[:4], "big") % DEFAULT_EMBEDDING_DIMENSIONS
    provider = LocalHashEmbeddingProvider()
    # 同一 token 在新实例上落进同一个桶
    other = LocalHashEmbeddingProvider()
    assert provider._bucket(token) % DEFAULT_EMBEDDING_DIMENSIONS == expected
    assert other._bucket(token) % DEFAULT_EMBEDDING_DIMENSIONS == expected


def test_embedding_is_normalized() -> None:
    vector = LocalHashEmbeddingProvider(dimensions=64).embed("部署流程与回滚")
    norm = sum(value * value for value in vector) ** 0.5
    assert abs(norm - 1.0) < 1e-9


def test_empty_text_yields_zero_vector() -> None:
    vector = LocalHashEmbeddingProvider(dimensions=8).embed("")
    assert vector == (0.0,) * 8


def test_different_texts_yield_different_vectors() -> None:
    provider = LocalHashEmbeddingProvider(dimensions=64)
    assert provider.embed("部署生产") != provider.embed("完全无关的另一句话")


def test_embedding_rejects_non_string() -> None:
    with pytest.raises(TypeError, match="str"):
        LocalHashEmbeddingProvider().embed(123)  # type: ignore[arg-type]


def test_embedding_rejects_non_positive_dimensions() -> None:
    with pytest.raises(ValueError, match="为正"):
        LocalHashEmbeddingProvider(dimensions=0)


def test_word_overlap_scores_higher_than_disjoint_text() -> None:
    """只验证「词面重合得分更高」这一最低限度，不声称语义质量。"""
    index = HistoryVectorIndex(LocalHashEmbeddingProvider(dimensions=128))
    index.upsert(THREAD_A, [Message(role="user", content="部署生产环境失败")])
    hits = index.search(THREAD_A, "部署生产", top_k=5)
    unrelated = index.search(THREAD_A, "紫色大象在跳舞", top_k=5)
    assert hits, "词面重合应当命中"
    assert not unrelated or hits[0].score > unrelated[0].score


# ---------------------------------------------------------------------------
# 水位与补齐
# ---------------------------------------------------------------------------


def test_first_sync_builds_index_and_sets_watermark() -> None:
    sink = InMemoryTraceSink()
    service, _, context_store = _build(trace_sink=sink)
    assert context_store.get(THREAD_A).indexed_through is None

    service.sync(THREAD_A, run_id="run-1")

    assert service.index.size(THREAD_A) == len(CORPUS_A)
    assert context_store.get(THREAD_A).indexed_through == len(CORPUS_A) - 1


def test_first_sync_does_not_emit_repaired_event() -> None:
    """首次建索引不是「修复」，不该冒充 repaired。"""
    sink = InMemoryTraceSink()
    service, _, _ = _build(trace_sink=sink)
    service.sync(THREAD_A, run_id="run-1")
    assert [e.name for e in sink.get_events() if e.name == "context.index.repaired"] == []


def test_lagging_watermark_is_repaired_on_search() -> None:
    sink = InMemoryTraceSink()
    service, thread_store, context_store = _build(trace_sink=sink)
    service.sync(THREAD_A)  # 水位到 4

    # 追加新消息后水位落后
    thread = thread_store.get(THREAD_A)
    thread.append(Message(role="assistant", content="健康检查通过，端口 8080"))
    thread_store.save(thread)
    assert context_store.get(THREAD_A).indexed_through == len(CORPUS_A) - 1
    assert thread.message_count == len(CORPUS_A) + 1

    hits = service.search(THREAD_A, "健康检查 8080", top_k=3, run_id="run-2")

    # 补齐后水位推进到最新
    assert context_store.get(THREAD_A).indexed_through == len(CORPUS_A)
    assert service.index.size(THREAD_A) == len(CORPUS_A) + 1
    # 新消息可被召回
    assert any(hit.reference == len(CORPUS_A) for hit in hits)

    repaired = [e for e in sink.get_events() if e.name == "context.index.repaired"]
    assert len(repaired) == 1
    assert repaired[0].metadata["from"] == len(CORPUS_A)
    assert repaired[0].metadata["through"] == len(CORPUS_A)
    assert repaired[0].metadata["watermark_before"] == len(CORPUS_A) - 1


def test_sync_is_idempotent() -> None:
    service, _, context_store = _build()
    service.sync(THREAD_A)
    first_size = service.index.size(THREAD_A)
    first_watermark = context_store.get(THREAD_A).indexed_through

    service.sync(THREAD_A)
    service.sync(THREAD_A)

    assert service.index.size(THREAD_A) == first_size
    assert context_store.get(THREAD_A).indexed_through == first_watermark


def test_upsert_is_idempotent_for_same_reference() -> None:
    index = HistoryVectorIndex(LocalHashEmbeddingProvider(dimensions=32))
    message = Message(role="user", content="重复内容")
    index.upsert(THREAD_A, [message], start_index=5)
    index.upsert(THREAD_A, [message], start_index=5)
    assert index.size(THREAD_A) == 1
    assert index.indexed_through(THREAD_A) == 5


def test_index_failure_does_not_roll_back_saved_messages() -> None:
    """索引失败不能回滚已保存的真实消息——投影坏了可以重建，事实不能丢。"""
    service, thread_store, context_store = _build()

    class ExplodingIndex:
        def upsert(self, *args, **kwargs):
            raise RuntimeError("索引写入失败")

        def drop(self, thread_id: str) -> None:  # 协议其余部分
            pass

        def indexed_through(self, thread_id: str):
            """索引为空：让 sync 认为需要从 0 开始建，从而走到 upsert 抛错。"""
            return None

        def size(self, thread_id: str) -> int:
            return 0

    broken = HistoryIndexService(
        index=ExplodingIndex(),  # type: ignore[arg-type]
        thread_store=thread_store,
        context_store=context_store,
    )
    before = thread_store.get(THREAD_A).to_dict()
    watermark_before = context_store.get(THREAD_A).indexed_through

    with pytest.raises(HistoryIndexLagError):
        broken.sync(THREAD_A, run_id="run-1")

    # 事实源分毫未动，水位也没被伪造成「已索引」
    assert thread_store.get(THREAD_A).to_dict() == before
    assert context_store.get(THREAD_A).indexed_through == watermark_before


def test_index_failure_emits_repair_failed_trace() -> None:
    sink = InMemoryTraceSink()
    service, thread_store, context_store = _build(trace_sink=sink)

    class ExplodingIndex:
        def upsert(self, *args, **kwargs):
            raise RuntimeError("索引写入失败")

        def drop(self, thread_id: str) -> None:
            pass

        def indexed_through(self, thread_id: str):
            """索引为空：让 sync 认为需要从 0 开始建，从而走到 upsert 抛错。"""
            return None

        def size(self, thread_id: str) -> int:
            return 0

    broken = HistoryIndexService(
        index=ExplodingIndex(),  # type: ignore[arg-type]
        thread_store=thread_store,
        context_store=context_store,
        trace_sink=sink,
    )
    with pytest.raises(HistoryIndexLagError):
        broken.sync(THREAD_A, run_id="run-7")

    events = [e for e in sink.get_events() if e.name == "context.index.repair_failed"]
    assert len(events) == 1
    assert events[0].error_type == "RuntimeError"
    assert events[0].run_id == "run-7"


def test_lagging_error_carries_watermark_and_available() -> None:
    service, thread_store, context_store = _build()

    class ExplodingIndex:
        def upsert(self, *args, **kwargs):
            raise RuntimeError("boom")

        def drop(self, thread_id: str) -> None:
            pass

        def indexed_through(self, thread_id: str):
            """索引为空：让 sync 认为需要从 0 开始建，从而走到 upsert 抛错。"""
            return None

        def size(self, thread_id: str) -> int:
            return 0

    broken = HistoryIndexService(
        index=ExplodingIndex(),  # type: ignore[arg-type]
        thread_store=thread_store,
        context_store=context_store,
    )
    with pytest.raises(HistoryIndexLagError) as excinfo:
        broken.sync(THREAD_A)
    assert excinfo.value.thread_id == THREAD_A
    assert excinfo.value.available == len(CORPUS_A) - 1


def test_sync_on_empty_thread_is_noop() -> None:
    service, thread_store, context_store = _build()
    thread_store.create("empty-thread")
    context_store.create("empty-thread")
    result = service.sync("empty-thread")
    assert result.indexed_through is None
    assert service.index.size("empty-thread") == 0


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------


def test_same_thread_recall() -> None:
    service, _, _ = _build()
    hits = service.search(THREAD_A, "部署 生产 502", top_k=3)
    assert hits
    assert hits[0].reference < len(CORPUS_A)
    assert hits[0].role in {"user", "assistant"}
    assert hits[0].score > 0


def test_search_returns_snippet_not_full_message() -> None:
    """命中只带安全截断的片段，避免一次拉回整段历史。"""
    service, _, _ = _build()
    service.index.upsert(
        THREAD_A, [Message(role="user", content="很长的内容" * 100)], start_index=99
    )
    hits = service.search(THREAD_A, "很长的内容", top_k=3)
    assert hits
    assert len(hits[0].snippet) <= 161  # 160 + 省略号
    assert hits[0].snippet.endswith("…") or len(hits[0].snippet) < 161


def test_thread_isolation_on_content() -> None:
    """B 线程独有的内容在 A 线程里检索不到。"""
    service, _, _ = _build()
    service.sync(THREAD_A)
    service.sync(THREAD_B)

    b_only = service.search(THREAD_B, "配色 蓝色", top_k=5)
    assert b_only, "B 线程自己应当能搜到"

    # A 线程的 bucket 里根本没有 B 的消息
    a_refs = {hit.reference for hit in service.search(THREAD_A, "配色 蓝色", top_k=10)}
    a_bucket = service.index._store.get(THREAD_A, {})
    assert all(
        "配色" not in entry[2] for entry in a_bucket.values()
    ), "A 线程索引里不应出现 B 线程的内容"
    # 即使有词面碰撞，返回的也只可能是 A 自己的 reference
    assert all(ref < len(CORPUS_A) or ref == 99 for ref in a_refs)


def test_thread_isolation_on_watermark() -> None:
    service, _, context_store = _build()
    service.sync(THREAD_A)
    assert context_store.get(THREAD_A).indexed_through == len(CORPUS_A) - 1
    # B 还没索引，水位独立
    assert context_store.get(THREAD_B).indexed_through is None
    service.sync(THREAD_B)
    assert context_store.get(THREAD_B).indexed_through == len(CORPUS_B) - 1
    assert context_store.get(THREAD_A).indexed_through == len(CORPUS_A) - 1


def test_drop_removes_only_target_thread() -> None:
    service, _, _ = _build()
    service.sync(THREAD_A)
    service.sync(THREAD_B)
    service.index.drop(THREAD_A)
    assert service.index.size(THREAD_A) == 0
    assert service.index.size(THREAD_B) == len(CORPUS_B)


def test_empty_result_for_unmatched_query() -> None:
    service, _, _ = _build()
    hits = service.search(THREAD_A, "zzzzqqqqxxxx", top_k=5)
    assert hits == []


# ---------------------------------------------------------------------------
# 参数上限
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"top_k": 0},
        {"top_k": -1},
        {"top_k": HISTORY_SEARCH_LIMITS["top_k"] + 1},
    ],
)
def test_search_top_k_bounds(kwargs: dict) -> None:
    service, _, _ = _build()
    with pytest.raises(HistoryReferenceError, match="top_k"):
        service.search(THREAD_A, "部署", **kwargs)


def test_search_query_length_bound() -> None:
    service, _, _ = _build()
    limit = HISTORY_SEARCH_LIMITS["query_chars"]
    with pytest.raises(HistoryReferenceError, match="query 超长"):
        service.search(THREAD_A, "x" * (limit + 1), top_k=1)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"before": -1, "after": 0},
        {"before": HISTORY_SEARCH_LIMITS["before"] + 1, "after": 0},
        {"before": 0, "after": -1},
        {"before": 0, "after": HISTORY_SEARCH_LIMITS["after"] + 1},
    ],
)
def test_read_window_bounds(kwargs: dict) -> None:
    service, _, _ = _build()
    with pytest.raises(HistoryReferenceError):
        service.read(THREAD_A, 0, **kwargs)


def test_read_unknown_reference_raises() -> None:
    service, _, _ = _build()
    with pytest.raises(HistoryReferenceError, match="不存在"):
        service.read(THREAD_A, 9999, before=0, after=0)


def test_read_returns_neighbouring_messages_in_order() -> None:
    service, _, _ = _build()
    hits = service.read(THREAD_A, 2, before=1, after=1)
    assert [hit.reference for hit in hits] == [1, 2, 3]
    assert hits[1].reference == 2


def test_read_window_clipped_at_thread_edges() -> None:
    service, _, _ = _build()
    hits = service.read(THREAD_A, 0, before=5, after=1)
    assert [hit.reference for hit in hits] == [0, 1]


def test_read_carries_role() -> None:
    service, _, _ = _build()
    hits = service.read(THREAD_A, 0, before=0, after=0)
    assert hits[0].role == "user"


# ---------------------------------------------------------------------------
# 工具契约
# ---------------------------------------------------------------------------


def test_tool_schema_has_no_thread_id() -> None:
    search = build_history_search_tool(_build()[0])
    read = build_history_read_tool(_build()[0])
    assert "thread_id" not in json.dumps(search.parameters)
    assert "thread_id" not in json.dumps(read.parameters)
    assert search.needs_context is True
    assert read.needs_context is True


def test_tool_declares_untrusted_history_boundary() -> None:
    assert "不可信" in HISTORY_NOTICE
    assert "不是系统指令" in HISTORY_NOTICE
    search = build_history_search_tool(_build()[0])
    assert "不可信历史数据" in search.description


def test_tool_result_carries_notice() -> None:
    service, _, _ = _build()
    registry = _registry(service)
    result = _call(
        registry, "history_search", {"query": "部署", "top_k": 2}, context=_ctx()
    )
    assert result["ok"] is True
    assert result["notice"] == HISTORY_NOTICE
    assert "不可信" in result["notice"]


def test_tool_rejects_missing_context() -> None:
    service, _, _ = _build()
    registry = _registry(service)
    with pytest.raises(ToolExecutionError, match="thread_id"):
        registry.execute(
            "history_search", {"query": "部署"}, context=None
        )


def test_tool_result_includes_watermark() -> None:
    service, _, _ = _build()
    registry = _registry(service)
    result = _call(registry, "history_search", {"query": "部署"}, context=_ctx())
    assert result["indexed_through"] == len(CORPUS_A) - 1


def test_tool_isolates_threads() -> None:
    service, _, _ = _build()
    registry = _registry(service)
    a_result = _call(registry, "history_search", {"query": "部署"}, context=_ctx(THREAD_A))
    b_result = _call(
        registry, "history_search", {"query": "部署", "top_k": 5}, context=_ctx(THREAD_B)
    )
    # B 线程刚被 search 触发同步，但它自己的 corpus 里没有部署内容
    assert b_result["ok"] is True
    assert all(
        hit["reference"] < len(CORPUS_B) for hit in b_result["hits"]
    ) or b_result["hits"] == []


def test_tool_invalid_arguments_return_usage_error() -> None:
    service, _, _ = _build()
    registry = _registry(service)
    assert _call(registry, "history_search", {"query": ""}, context=_ctx())["ok"] is False
    assert _call(
        registry, "history_search", {"query": "x", "top_k": 999}, context=_ctx()
    )["ok"] is False
    assert _call(
        registry, "history_read", {"reference": "not-int"}, context=_ctx()
    )["ok"] is False
    assert _call(
        registry, "history_read", {"reference": 0, "before": 99}, context=_ctx()
    )["ok"] is False


def test_tool_read_returns_neighbouring_full_text() -> None:
    service, _, _ = _build()
    registry = _registry(service)
    result = _call(
        registry,
        "history_read",
        {"reference": 0, "before": 0, "after": 1},
        context=_ctx(),
    )
    assert result["ok"] is True
    assert result["count"] == 2
    assert result["messages"][0]["reference"] == 0
    assert "部署到生产环境" in result["messages"][0]["snippet"]


def test_tool_on_unknown_thread_raises_clear_error() -> None:
    """线程不存在时给出明确错误，而不是伪造「搜到了 0 条」的空结果。"""
    service, _, _ = _build()
    registry = _registry(service)
    with pytest.raises(ToolExecutionError):
        registry.execute(
            "history_search",
            {"query": "任何东西"},
            context=_ctx(thread_id="no-such-thread"),
        )


def test_index_trace_is_correlated_with_run_id() -> None:
    """补齐索引的 Trace 必须带上发起它的 run_id，否则串不起来。"""
    sink = InMemoryTraceSink()
    service, thread_store, _ = _build(trace_sink=sink)
    service.sync(THREAD_A)  # 首次建索引：不发 repaired

    thread = thread_store.get(THREAD_A)
    thread.append(Message(role="user", content="新增待索引的消息"))
    thread_store.save(thread)

    service.search(THREAD_A, "新增", top_k=3, run_id="run-9")

    repaired = [e for e in sink.get_events() if e.name == "context.index.repaired"]
    assert len(repaired) == 1
    assert repaired[0].run_id == "run-9"
    assert repaired[0].thread_id == THREAD_A
    assert repaired[0].operation == "TOOL_CALL"


def test_service_exposes_index_for_cleanup() -> None:
    service, _, _ = _build()
    assert isinstance(service.index, HistoryVectorIndex)
    service.sync(THREAD_A)
    assert service.index.size(THREAD_A) > 0
    service.index.drop(THREAD_A)
    assert service.index.size(THREAD_A) == 0


def test_service_defaults_to_local_hash_provider() -> None:
    service, _, _ = _build(dimensions=DEFAULT_EMBEDDING_DIMENSIONS)
    assert isinstance(service.index.provider, LocalHashEmbeddingProvider)
    assert service.index.provider.dimensions == DEFAULT_EMBEDDING_DIMENSIONS


def test_context_policy_not_required_for_history() -> None:
    """检索模块不依赖 ContextPolicy——两者可独立使用。"""
    _ = ContextPolicy(
        max_context_tokens=8000, reserved_output_tokens=1000, safety_margin_tokens=500
    )
    service, _, _ = _build()
    assert service.search(THREAD_A, "部署", top_k=1) is not None


# ---------------------------------------------------------------------------
# 进程重启：水位已持久化但索引是空的（风险 1）
# ---------------------------------------------------------------------------


def test_restart_rebuilds_index_despite_persisted_watermark(tmp_path: Path) -> None:
    """重启后 `indexed_through` 还在 SQLite 里，内存索引却是空的。

    若 sync 拿持久化水位当起点，`start >= total` 会直接返回——索引永远
    重建不起来，history_search 静默返回空结果，模型会误以为「历史里确实
    没有这件事」。因此起点必须取自索引实例的实际状态。
    """
    thread_db = tmp_path / "threads.db"
    context_db = tmp_path / "context.db"

    # ---- 进程 1：建线程、建索引、推进水位并落库 ----
    ts1 = SQLiteThreadStore(thread_db)
    cs1 = SQLiteContextStore(context_db)
    ts1.create(THREAD_A)
    cs1.create(THREAD_A)
    thread = ts1.get(THREAD_A)
    for role, content in CORPUS_A:
        thread.append(Message(role=role, content=content))
    ts1.save(thread)

    svc1 = HistoryIndexService(
        index=HistoryVectorIndex(LocalHashEmbeddingProvider(dimensions=64)),
        thread_store=ts1,
        context_store=cs1,
    )
    first_hits = svc1.search(THREAD_A, "部署 生产", top_k=3, run_id="run-1")
    persisted_watermark = cs1.get(THREAD_A).indexed_through
    assert first_hits, "进程 1 内应当能召回"
    assert persisted_watermark == len(CORPUS_A) - 1
    ts1.close()
    cs1.close()

    # ---- 进程 2：SQLite 还在，向量索引是全新的空实例 ----
    ts2 = SQLiteThreadStore(thread_db)
    cs2 = SQLiteContextStore(context_db)
    assert ts2.get(THREAD_A).message_count == len(CORPUS_A), "事实源持久化"
    assert cs2.get(THREAD_A).indexed_through == persisted_watermark, "水位持久化"
    assert cs2.get(THREAD_A).summary is None

    svc2 = HistoryIndexService(
        index=HistoryVectorIndex(LocalHashEmbeddingProvider(dimensions=64)),
        thread_store=ts2,
        context_store=cs2,
    )
    assert svc2.index.size(THREAD_A) == 0, "新索引实例必须是空的"

    # 关键断言：重启后搜索仍能召回，且不因旧 watermark 跳过重建
    restarted_hits = svc2.search(THREAD_A, "部署 生产", top_k=3, run_id="run-2")
    assert restarted_hits, "重启后 history_search 必须自动重建索引并召回"
    assert svc2.index.size(THREAD_A) == len(CORPUS_A), "索引已全量重建"
    assert cs2.get(THREAD_A).indexed_through == persisted_watermark
    # 召回的 reference 是真实消息序号
    assert all(0 <= hit.reference < len(CORPUS_A) for hit in restarted_hits)
    assert {hit.reference for hit in restarted_hits} == {
        hit.reference for hit in first_hits
    }, "同一查询在重启前后应命中同一批消息"
    ts2.close()
    cs2.close()


def test_restart_does_not_emit_spurious_repair(tmp_path: Path) -> None:
    """重启后的全量重建不算「修复」——索引本来就是空的，没有缺口可补。"""
    thread_db = tmp_path / "threads.db"
    context_db = tmp_path / "context.db"

    ts1 = SQLiteThreadStore(thread_db)
    cs1 = SQLiteContextStore(context_db)
    ts1.create(THREAD_A)
    cs1.create(THREAD_A)
    thread = ts1.get(THREAD_A)
    for role, content in CORPUS_A:
        thread.append(Message(role=role, content=content))
    ts1.save(thread)
    HistoryIndexService(
        index=HistoryVectorIndex(LocalHashEmbeddingProvider(dimensions=64)),
        thread_store=ts1,
        context_store=cs1,
    ).sync(THREAD_A, run_id="first")
    ts1.close()
    cs1.close()

    ts2 = SQLiteThreadStore(thread_db)
    cs2 = SQLiteContextStore(context_db)
    sink = InMemoryTraceSink()
    svc2 = HistoryIndexService(
        index=HistoryVectorIndex(LocalHashEmbeddingProvider(dimensions=64)),
        thread_store=ts2,
        context_store=cs2,
        trace_sink=sink,
    )
    svc2.sync(THREAD_A, run_id="after-restart")

    repaired = [e for e in sink.get_events() if e.name == "context.index.repaired"]
    assert repaired == [], "索引为空时的重建不该冒充 repaired"
    assert svc2.index.size(THREAD_A) == len(CORPUS_A)
    ts2.close()
    cs2.close()


def test_partial_index_repairs_from_actual_position(tmp_path: Path) -> None:
    """索引只覆盖到一半时，补齐从**索引实际位置**继续，而不是持久化水位。"""
    from mini_deerflow.context_state import ContextState
    from dataclasses import replace as _replace

    thread_db = tmp_path / "threads.db"
    context_db = tmp_path / "context.db"
    ts = SQLiteThreadStore(thread_db)
    cs = SQLiteContextStore(context_db)
    ts.create(THREAD_A)
    cs.create(THREAD_A)
    thread = ts.get(THREAD_A)
    for role, content in CORPUS_A:
        thread.append(Message(role=role, content=content))
    ts.save(thread)

    index = HistoryVectorIndex(LocalHashEmbeddingProvider(dimensions=64))
    # 手动只索引前两条
    index.upsert(THREAD_A, tuple(thread.snapshot()[:2]), start_index=0)
    assert index.size(THREAD_A) == 2
    # 而持久化水位被写成「已全部处理」——两者不一致，必须以索引为准
    current = cs.get(THREAD_A)
    cs.save(
        THREAD_A,
        _replace(current, indexed_through=len(CORPUS_A) - 1),
        expected_revision=current.revision,
    )

    sink = InMemoryTraceSink()
    svc = HistoryIndexService(
        index=index, thread_store=ts, context_store=cs, trace_sink=sink
    )
    svc.search(THREAD_A, "回滚 版本", top_k=3, run_id="r")

    # 索引从第 2 条（实际位置）继续补齐，最终覆盖全部
    assert index.size(THREAD_A) == len(CORPUS_A)
    assert index.indexed_through(THREAD_A) == len(CORPUS_A) - 1
    repaired = [e for e in sink.get_events() if e.name == "context.index.repaired"]
    assert len(repaired) == 1
    assert repaired[0].metadata["from"] == 2, "缺口从索引实际位置开始"
    ts.close()
    cs.close()
