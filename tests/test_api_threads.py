"""FastAPI Thread REST API 集成测试（19B 阶段）。

使用 TestClient + 真实 InMemoryThreadStore（经 RuntimeContext 注入）。
Service 层不依赖具体 Store：测试只需替换 RuntimeContext.store 即可验证
Store 抽象对路由层透明。

覆盖：
- 4 个端点的契约（201/200/200/204、404、409、422）
- 序列化字段（不暴露 state_json / schema_version 等内部字段）
- 路由使用注入的 Store（不直接构造 SQLite/InMemory）
- lifespan 关闭后 Runtime 正确 shutdown（与 19A 协同）
- 自定义 api_prefix 行为
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from mini_deerflow.api.app import create_app
from mini_deerflow.api.config import ApiSettings
from mini_deerflow.api.services.threads import ThreadService
from mini_deerflow.runtime import RuntimeContext, create_runtime
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tools import ToolRegistry


# =============================================================================
# 工具：构造一个独立、确定性的 TestClient（共享 InMemoryThreadStore）
# =============================================================================


def _make_client(
    api_prefix: str = "/api",
    runtime_factory=None,
) -> tuple[TestClient, InMemoryThreadStore]:
    """构造带共享内存 Store 的 TestClient；返回 (client, store)。"""
    if runtime_factory is None:
        store = InMemoryThreadStore()
        runtime = create_runtime(
            store=store,
            registry=ToolRegistry(),
            config=__import__(
                "mini_deerflow.runtime", fromlist=["RuntimeConfig"]
            ).RuntimeConfig(),
        )
        runtime_factory = lambda: runtime  # noqa: E731

    app = create_app(
        ApiSettings(api_prefix=api_prefix),
        runtime_factory=runtime_factory,
    )
    client = TestClient(app)
    return client, store  # type: ignore[return-value]


def _runtime_state(app, runtime: RuntimeContext) -> InMemoryThreadStore:
    """从 app.state.runtime 取出 store。失败则 raise。"""
    return runtime.store  # type: ignore[return-value]


# =============================================================================
# 1. POST /api/threads
# =============================================================================


def test_post_threads_returns_201_with_empty_thread() -> None:
    """POST /api/threads 不带 body 时返回 201 + ThreadDetail（messages=[]）。"""
    client, store = _make_client()
    with client:
        response = client.post("/api/threads", json={})
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"thread_id", "messages"}
    assert body["messages"] == []


def test_post_threads_generates_unique_nonempty_id() -> None:
    """自动生成的 thread_id 非空且互不相同。"""
    client, store = _make_client()
    with client:
        ids = set()
        for _ in range(5):
            r = client.post("/api/threads", json={})
            assert r.status_code == 201
            tid = r.json()["thread_id"]
            assert isinstance(tid, str) and tid, f"thread_id 应为非空 str，实际 {tid!r}"
            ids.add(tid)
    assert len(ids) == 5, "5 次创建的 thread_id 应互不相同"


def test_post_threads_persists_to_store() -> None:
    """创建后可以从 Store 直接读取到相同 ID 的 Thread。"""
    client, store = _make_client()
    with client:
        response = client.post("/api/threads", json={})
        assert response.status_code == 201
        tid = response.json()["thread_id"]
        # 直接读 Store（绕过 API）确认存在
        assert store.exists(tid), "Store 中应能找到刚创建的 thread_id"
        thread = store.get(tid)
        assert thread.thread_id == tid
        assert thread.messages == []


def test_post_threads_duplicate_client_id_returns_409() -> None:
    """客户端指定已存在的 thread_id 时返回 409。"""
    client, store = _make_client()
    with client:
        first = client.post("/api/threads", json={"thread_id": "client-1"})
        assert first.status_code == 201
        second = client.post("/api/threads", json={"thread_id": "client-1"})
    assert second.status_code == 409
    assert "client-1" in second.json()["error"]["message"]


def test_post_threads_with_client_id_persists_same_id() -> None:
    """客户端显式提供 ID 时，必须按相同 ID 落库。"""
    client, store = _make_client()
    with client:
        response = client.post("/api/threads", json={"thread_id": "my-thread"})
    assert response.status_code == 201
    assert response.json()["thread_id"] == "my-thread"
    assert store.exists("my-thread")


# =============================================================================
# 2. GET /api/threads
# =============================================================================


def test_list_threads_empty_returns_200_and_empty_list() -> None:
    """空仓库时 GET /api/threads 返回 200 + {threads: []}。"""
    client, store = _make_client()
    with client:
        response = client.get("/api/threads")
    assert response.status_code == 200
    assert response.json() == {"threads": []}


def test_list_threads_includes_created() -> None:
    """创建后列表里能看到对应 ID。"""
    client, store = _make_client()
    with client:
        client.post("/api/threads", json={"thread_id": "alpha"})
        client.post("/api/threads", json={"thread_id": "beta"})
        response = client.get("/api/threads")
    assert response.status_code == 200
    ids = [item["thread_id"] for item in response.json()["threads"]]
    assert "alpha" in ids
    assert "beta" in ids


def test_list_threads_order_is_stable() -> None:
    """列表顺序稳定：多次调用结果一致（按 Store 内部排序）。"""
    client, store = _make_client()
    with client:
        for tid in ("zzz", "aaa", "mmm"):
            client.post("/api/threads", json={"thread_id": tid})
        r1 = client.get("/api/threads").json()
        r2 = client.get("/api/threads").json()
    assert r1 == r2, f"两次列表结果必须一致：{r1} vs {r2}"
    # Store 内部按 thread_id 排序，期望顺序确定
    assert [t["thread_id"] for t in r1["threads"]] == ["aaa", "mmm", "zzz"]


def test_list_threads_response_shape_has_only_thread_id() -> None:
    """列表项只暴露 thread_id；不能含 messages / state_json / schema_version。"""
    client, store = _make_client()
    with client:
        client.post("/api/threads", json={})
        response = client.get("/api/threads")
    item = response.json()["threads"][0]
    assert set(item.keys()) == {"thread_id"}


# =============================================================================
# 3. GET /api/threads/{thread_id}
# =============================================================================


def test_get_thread_existing_returns_200() -> None:
    """查询存在 Thread 返回 200。"""
    client, store = _make_client()
    with client:
        created = client.post("/api/threads", json={"thread_id": "x"}).json()
        response = client.get("/api/threads/x")
    assert response.status_code == 200
    assert response.json() == created


def test_get_thread_missing_returns_404() -> None:
    """查询不存在的 thread_id 返回 404。"""
    client, store = _make_client()
    with client:
        response = client.get("/api/threads/nope")
    assert response.status_code == 404
    assert "nope" in response.json()["error"]["message"]


def test_get_thread_detail_contains_messages() -> None:
    """详情包含 messages 列表（这里为空列表）。"""
    client, store = _make_client()
    with client:
        client.post("/api/threads", json={"thread_id": "msg-thread"})
        response = client.get("/api/threads/msg-thread")
    body = response.json()
    assert body["thread_id"] == "msg-thread"
    assert isinstance(body["messages"], list)
    assert body["messages"] == []


def test_get_thread_does_not_expose_internal_fields() -> None:
    """详情不暴露 schema_version / state_json 等内部字段。"""
    client, store = _make_client()
    with client:
        client.post("/api/threads", json={"thread_id": "internal"})
        response = client.get("/api/threads/internal")
    body = response.json()
    forbidden = {"schema_version", "state_json", "_internal"}
    assert not (set(body) & forbidden), (
        f"响应暴露了内部字段 {set(body) & forbidden}"
    )


# =============================================================================
# 4. DELETE /api/threads/{thread_id}
# =============================================================================


def test_delete_thread_existing_returns_204_with_no_body() -> None:
    """删除存在 Thread 返回 204 且响应体为空。"""
    client, store = _make_client()
    with client:
        client.post("/api/threads", json={"thread_id": "to-delete"})
        response = client.delete("/api/threads/to-delete")
    assert response.status_code == 204
    assert response.content == b""


def test_delete_then_get_returns_404() -> None:
    """删除后再 GET 同一 ID 应返回 404。"""
    client, store = _make_client()
    with client:
        client.post("/api/threads", json={"thread_id": "ghost"})
        client.delete("/api/threads/ghost")
        response = client.get("/api/threads/ghost")
    assert response.status_code == 404
    assert not store.exists("ghost")


def test_delete_thread_missing_returns_404() -> None:
    """删除不存在 ID 返回 404。"""
    client, store = _make_client()
    with client:
        response = client.delete("/api/threads/missing")
    assert response.status_code == 404


# =============================================================================
# 5. 配置 / OpenAPI / 隔离性
# =============================================================================


def test_custom_api_prefix_applies_to_threads_endpoints() -> None:
    """自定义 api_prefix 同时作用于 4 个 thread 端点。"""
    client, store = _make_client(api_prefix="/v1")
    with client:
        # 4 端点在新前缀下可达
        assert client.post("/v1/threads", json={}).status_code == 201
        assert client.get("/v1/threads").status_code == 200
        # 老前缀已不再生效
        assert client.get("/api/threads").status_code == 404
        assert client.post("/api/threads", json={}).status_code == 404


def test_openapi_includes_all_four_thread_endpoints() -> None:
    """OpenAPI schema 暴露 4 个 thread 端点。"""
    client, store = _make_client()
    with client:
        schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    assert paths.get("/api/threads") is not None
    assert "post" in paths["/api/threads"]
    assert "get" in paths["/api/threads"]
    detail_path = paths.get("/api/threads/{thread_id}")
    assert detail_path is not None
    assert "get" in detail_path
    assert "delete" in detail_path


def test_openapi_groups_threads_under_tag() -> None:
    """thread 路由打上 'threads' 标签，便于 OpenAPI UI 归类。"""
    client, store = _make_client()
    with client:
        schema = client.get("/openapi.json").json()
    op = schema["paths"]["/api/threads"]["get"]
    assert "threads" in op["tags"]


def test_routes_use_runtime_store_not_concrete_class() -> None:
    """路由通过 RuntimeContext.store 拿到 Store；不直接构造具体实现。

    验证方式：自定义一个非 InMemory/SQLite 的轻量 Store，
    用相同 RuntimeContext 注入，断言 4 个端点都从它读取/写入。
    """
    from mini_deerflow.thread_store import (
        ThreadAlreadyExistsError,
        ThreadNotFoundError,
    )

    class TrackingStore:
        """最小线程仓库：仅记录调用，用于验证注入路径。"""

        def __init__(self) -> None:
            self._data: dict[str, list[dict[str, str]]] = {}
            self.create_calls = 0

        def create(self, thread_id: str):
            if thread_id in self._data:
                raise ThreadAlreadyExistsError(f"dup {thread_id}")
            self._data[thread_id] = []
            self.create_calls += 1
            from mini_deerflow.thread_state import ThreadState
            return ThreadState(thread_id=thread_id)

        def get(self, thread_id: str):
            if thread_id not in self._data:
                raise ThreadNotFoundError(f"miss {thread_id}")
            from mini_deerflow.thread_state import ThreadState
            return ThreadState(thread_id=thread_id)

        def save(self, thread) -> None:
            pass

        def delete(self, thread_id: str) -> None:
            if thread_id not in self._data:
                raise ThreadNotFoundError(f"miss {thread_id}")
            del self._data[thread_id]

        def exists(self, thread_id: str) -> bool:
            return thread_id in self._data

        def list_threads(self):
            from mini_deerflow.thread_state import ThreadState
            return tuple(
                ThreadState(thread_id=tid) for tid in sorted(self._data)
            )

    tracker = TrackingStore()
    runtime = create_runtime(
        store=tracker,
        registry=ToolRegistry(),
        config=__import__(
            "mini_deerflow.runtime", fromlist=["RuntimeConfig"]
        ).RuntimeConfig(),
    )
    factory = lambda: runtime  # noqa: E731

    app = create_app(ApiSettings(), runtime_factory=factory)
    client = TestClient(app)
    with client:
        c1 = client.post("/api/threads", json={"thread_id": "tracked-1"})
        c2 = client.post("/api/threads", json={"thread_id": "tracked-2"})
        assert c1.status_code == 201
        assert c2.status_code == 201
        listed = client.get("/api/threads").json()
        assert {t["thread_id"] for t in listed["threads"]} == {
            "tracked-1",
            "tracked-2",
        }
        detail = client.get("/api/threads/tracked-1")
        assert detail.status_code == 200
        client.delete("/api/threads/tracked-1")
        assert tracker.create_calls == 2
        assert not tracker.exists("tracked-1")


def test_service_layer_does_not_depend_on_fastapi() -> None:
    """ThreadService 不应 import FastAPI；纯领域层。"""
    import ast
    from pathlib import Path

    service_path = Path(
        "src/mini_deerflow/api/services/threads.py"
    ).resolve()
    tree = ast.parse(service_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        mod: str | None = None
        if isinstance(node, ast.Import):
            mod = node.names[0].name
        elif isinstance(node, ast.ImportFrom):
            mod = node.module
        assert not (mod and mod.startswith("fastapi")), (
            f"Service 不应依赖 FastAPI：违规 import {mod}"
        )


# =============================================================================
# 6. lifespan 协同（19A + 19B 衔接）
# =============================================================================


def test_lifespan_shutdown_runs_after_thread_routes_used() -> None:
    """lifespan 启动后跑一次 thread API，关闭时仍正常 shutdown（19A 协同）。"""
    calls: dict[str, int] = {"shutdown": 0, "create": 0}

    class CountingRuntime:
        def __init__(self) -> None:
            self.store = InMemoryThreadStore()

        def shutdown(self) -> None:
            calls["shutdown"] += 1

    class CountingFactory:
        def __init__(self) -> None:
            self._runtime = CountingRuntime()

        def __call__(self) -> CountingRuntime:
            calls["create"] += 1
            return self._runtime

    factory = CountingFactory()
    app = create_app(ApiSettings(), runtime_factory=factory)
    with TestClient(app) as c:
        c.post("/api/threads", json={})
        assert c.get("/api/threads").status_code == 200

    assert calls["create"] == 1
    assert calls["shutdown"] == 1


def test_health_endpoints_still_work_after_threads_added() -> None:
    """新增 thread 路由后，health / ready 不受影响。"""
    client, store = _make_client()
    with client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/ready").status_code == 200


def test_thread_routes_503_when_runtime_missing() -> None:
    """Runtime 不在时 thread 路由应返回 503（与 19A get_runtime 一致）。"""
    from unittest.mock import MagicMock
    from fastapi import HTTPException

    from mini_deerflow.api.dependencies import get_runtime

    mock_request = MagicMock()
    mock_request.app.state.runtime = None
    with pytest.raises(HTTPException) as exc:
        get_runtime(mock_request)
    assert exc.value.status_code == 503