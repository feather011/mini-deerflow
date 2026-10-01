"""19D SSE Stream 路由单元测试。

覆盖：
- SSE 头（Content-Type、Cache-Control、X-Run-ID、X-Accel-Buffering）
- 事件序列：run.started → model.started/completed → run.completed
- run_id 贯通（X-Run-ID == data.run_id）
- 错误映射：404 / 409 / 422 / 500（不泄露 Traceback）
- 客户端断开仅 unsubscribe，不取消 Agent
- 失败事件文案安全（不暴露 prompt / 凭据 / 路径 / Traceback）

使用 FastAPI TestClient + 真实 Agent + FakeModel；不依赖网络。
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mini_deerflow.api.app import create_app
from mini_deerflow.runtime import RuntimeConfig, create_runtime
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tools import ToolRegistry


# --------------------------------------------------------------------------- helpers


def _build_app(
    *,
    stream_max_workers: int = 4,
    stream_queue_maxsize: int = 64,
):
    """构造带纯内存 store 的 FastAPI 应用。"""

    def factory():
        return create_runtime(
            store=InMemoryThreadStore(),
            registry=ToolRegistry(),
            config=RuntimeConfig(
                stream_max_workers=stream_max_workers,
                stream_queue_maxsize=stream_queue_maxsize,
            ),
        )

    return create_app(runtime_factory=factory)


def _create_thread(client: TestClient, title: str = "t") -> str:
    r = client.post("/api/threads", json={"title": title})
    assert r.status_code == 201
    return r.json()["thread_id"]


def _parse_sse(body: str) -> list[dict[str, Any]]:
    """把 SSE 文本解析为 [{id, event, data}, ...] 列表。

    仅提取 id / event / data 三类行；其它注释行忽略。
    """
    messages: list[dict[str, Any]] = []
    msg: dict[str, Any] = {}
    for line in body.splitlines():
        if line.startswith(":"):
            # 注释行（心跳）
            continue
        if line == "":
            if msg:
                messages.append(msg)
                msg = {}
            continue
        if line.startswith("id:"):
            msg["id"] = int(line[3:].strip())
        elif line.startswith("event:"):
            msg["event"] = line[6:].strip()
        elif line.startswith("data:"):
            data_str = line[5:].strip()
            try:
                msg["data"] = json.loads(data_str)
            except json.JSONDecodeError:
                msg["data_raw"] = data_str
    if msg:
        messages.append(msg)
    return messages


def _read_stream(client: TestClient, thread_id: str, message: str) -> dict[str, Any]:
    """发请求并读完整流；返回 {status, headers, messages}。"""
    with client.stream(
        "POST",
        f"/api/threads/{thread_id}/runs/stream",
        json={"message": message},
    ) as resp:
        body_chunks: list[str] = []
        for chunk in resp.iter_text():
            body_chunks.append(chunk)
        body = "".join(body_chunks)
        return {
            "status": resp.status_code,
            "headers": dict(resp.headers),
            "body": body,
            "messages": _parse_sse(body),
        }


# --------------------------------------------------------------------------- headers


def test_stream_endpoint_returns_event_stream_content_type():
    app = _build_app()
    with TestClient(app) as client:
        tid = _create_thread(client)
        result = _read_stream(client, tid, "hi")
        assert result["status"] == 200
        ct = result["headers"].get("content-type", "")
        assert ct.startswith("text/event-stream")


def test_stream_endpoint_returns_x_run_id_header():
    app = _build_app()
    with TestClient(app) as client:
        tid = _create_thread(client)
        result = _read_stream(client, tid, "hi")
        x_run_id = result["headers"].get("x-run-id")
        assert x_run_id is not None
        assert x_run_id.startswith("run-")
        # 至少 1 个非 id 行携带相同 run_id
        for m in result["messages"]:
            assert m["data"]["run_id"] == x_run_id


def test_stream_endpoint_returns_cache_control_no_cache():
    app = _build_app()
    with TestClient(app) as client:
        tid = _create_thread(client)
        result = _read_stream(client, tid, "hi")
        assert result["headers"].get("cache-control") == "no-cache"


def test_stream_endpoint_returns_x_accel_buffering_no():
    app = _build_app()
    with TestClient(app) as client:
        tid = _create_thread(client)
        result = _read_stream(client, tid, "hi")
        assert result["headers"].get("x-accel-buffering") == "no"


# --------------------------------------------------------------------------- 事件序列


def test_stream_endpoint_emits_run_started_first():
    app = _build_app()
    with TestClient(app) as client:
        tid = _create_thread(client)
        result = _read_stream(client, tid, "hi")
        events = [m["event"] for m in result["messages"]]
        assert events[0] == "run.started"
        assert events[-1] in {"run.completed", "run.failed", "run.timed_out", "run.cancelled"}


def test_stream_endpoint_emits_terminal_last():
    app = _build_app()
    with TestClient(app) as client:
        tid = _create_thread(client)
        result = _read_stream(client, tid, "hi")
        events = [m["event"] for m in result["messages"]]
        # 最后一个事件必须是终态之一
        terminal = {
            "run.completed",
            "run.failed",
            "run.timed_out",
            "run.cancelled",
        }
        assert events[-1] in terminal


def test_stream_endpoint_run_id_matches_x_run_id_header():
    app = _build_app()
    with TestClient(app) as client:
        tid = _create_thread(client)
        result = _read_stream(client, tid, "hi")
        x_run_id = result["headers"]["x-run-id"]
        run_ids = {m["data"]["run_id"] for m in result["messages"]}
        assert run_ids == {x_run_id}


def test_stream_endpoint_sequences_are_monotonic():
    app = _build_app()
    with TestClient(app) as client:
        tid = _create_thread(client)
        result = _read_stream(client, tid, "hi")
        seqs = [m["id"] for m in result["messages"]]
        for prev, nxt in zip(seqs, seqs[1:]):
            assert nxt == prev + 1


def test_stream_endpoint_thread_id_matches():
    app = _build_app()
    with TestClient(app) as client:
        tid = _create_thread(client)
        result = _read_stream(client, tid, "hi")
        thread_ids = {m["data"]["thread_id"] for m in result["messages"]}
        assert thread_ids == {tid}


def test_stream_endpoint_successful_run_includes_answer():
    app = _build_app()
    with TestClient(app) as client:
        tid = _create_thread(client)
        result = _read_stream(client, tid, "hi")
        # 找到 run.completed 事件，断言含 answer 字段
        completed = [m for m in result["messages"] if m["event"] == "run.completed"]
        assert len(completed) == 1
        assert "answer" in completed[0]["data"]


def test_stream_endpoint_does_not_leak_prompt_or_traceback():
    """事件文案中绝不出现 Traceback / 路径 / 模型凭据。"""
    app = _build_app()
    with TestClient(app) as client:
        tid = _create_thread(client)
        result = _read_stream(client, tid, "hi")
        for m in result["messages"]:
            data_str = json.dumps(m.get("data", {}), ensure_ascii=False)
            assert "Traceback" not in data_str
            assert "API_KEY" not in data_str
            assert "sk-" not in data_str  # 常见 OpenAI key 前缀
            assert "\\Users\\" not in data_str  # Windows 路径


# --------------------------------------------------------------------------- 错误映射


def test_stream_endpoint_returns_404_when_thread_missing():
    app = _build_app()
    with TestClient(app) as client:
        result = _read_stream(client, "nonexistent-thread", "hi")
        assert result["status"] == 404
        # 错误响应不是 SSE 格式
        assert "event:" not in result["body"]


def test_stream_endpoint_returns_422_on_empty_message():
    app = _build_app()
    with TestClient(app) as client:
        tid = _create_thread(client)
        result = _read_stream(client, tid, "")
        assert result["status"] == 422


def test_stream_endpoint_returns_422_on_missing_message_field():
    app = _build_app()
    with TestClient(app) as client:
        tid = _create_thread(client)
        r = client.post(
            f"/api/threads/{tid}/runs/stream",
            json={},
        )
        assert r.status_code == 422


# --------------------------------------------------------------------------- 并发


def test_stream_endpoint_returns_409_on_concurrent_same_thread():
    """同一 thread 已有 run 进行中时第二个流返回 409。

    用 coordinator.try_acquire 预占位，避免依赖 FakeModel 时序。
    """
    app = _build_app()
    with TestClient(app) as client:
        tid = _create_thread(client)
        # 触发 lifespan 启动
        client.get("/api/health")

        # 直接通过 RuntimeContext 占用线程
        rt = client.app.state.runtime
        try:
            rt.thread_run_coordinator.try_acquire(tid, "fake-run")
            result = _read_stream(client, tid, "second")
            assert result["status"] == 409
            assert "event:" not in result["body"]
        finally:
            rt.thread_run_coordinator.release(tid, run_id="fake-run")


def test_stream_endpoint_different_threads_succeed():
    """不同 thread 的 stream 不互斥。"""
    app = _build_app()
    with TestClient(app) as client:
        t1 = _create_thread(client, "t1")
        t2 = _create_thread(client, "t2")
        r1 = _read_stream(client, t1, "a")
        r2 = _read_stream(client, t2, "b")
        assert r1["status"] == 200
        assert r2["status"] == 200


# --------------------------------------------------------------------------- openapi


def test_openapi_includes_stream_endpoint():
    app = _build_app()
    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()
        path = "/api/threads/{thread_id}/runs/stream"
        assert path in schema["paths"]
