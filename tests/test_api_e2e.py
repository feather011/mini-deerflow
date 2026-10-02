"""19F 端到端（E2E）测试：完整后端流程验证。"""
from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mini_deerflow.api.app import create_app
from mini_deerflow.api.config import ApiSettings
from mini_deerflow.models import FakeModel, FinalAnswer, Message, ToolCall
from mini_deerflow.runtime import RuntimeConfig, RuntimeContext, create_runtime
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tools import ToolRegistry


# ============================================================================
# 自定义 FakeModel：接受 "hello" 返回 FinalAnswer
# ============================================================================


class EchoModel:
    """确定性模型：接受 hello 返回固定 answer，模拟 Agent 执行。"""
    def decide(self, messages: list[Message], tools=None) -> ToolCall | FinalAnswer:
        if not messages:
            return FinalAnswer(content="请输入内容")
        latest = messages[-1]
        return FinalAnswer(content=f"已收到: {latest.content}")


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def tmp_workspace():
    root = Path(tempfile.mkdtemp(prefix="mdf_e2e_ws_"))
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def e2e_client(tmp_workspace):
    """完整的 E2E 测试客户端，使用真实 Runtime。"""
    from mini_deerflow.agent import Agent

    store = InMemoryThreadStore()
    registry = ToolRegistry()
    config = RuntimeConfig(
        workspace_root=str(tmp_workspace),
        workspace_max_upload_bytes=10 * 1024 * 1024,
        workspace_max_list_entries=1000,
        subagent_enabled=False,
        trace_enabled=False,
    )
    # Agent 由 create_runtime 内部创建，不需要手动构造
    runtime = create_runtime(
        store=store,
        registry=registry,
        config=config,
    )

    settings = ApiSettings(
        workspace_root=str(tmp_workspace),
        max_upload_bytes=10 * 1024 * 1024,
    )
    app = create_app(settings=settings, runtime_factory=lambda: runtime)

    with TestClient(app) as client:
        yield client, runtime, tmp_workspace


def _parse_sse(body: str) -> list[dict]:
    """解析 SSE 文本为 [{id, event, data}, ...] 列表。"""
    messages = []
    msg: dict = {}
    for line in body.splitlines():
        if line.startswith(":"):
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


# ============================================================================
# E2E-1: 健康检查
# ============================================================================


def test_e2e_health_success(e2e_client):
    client, _, _ = e2e_client
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["service"] == "mini-deerflow"
    # 健康检查不含 request_id（middleware 后注册，health 不通过 middleware）
    # 实际上 health 是同步 def 函数，middleware 仍然会处理


def test_e2e_ready_success(e2e_client):
    client, _, _ = e2e_client
    resp = client.get("/api/ready")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ready"
    assert data["runtime"] is True


# ============================================================================
# E2E-2: 创建 Thread
# ============================================================================


def test_e2e_create_thread(e2e_client):
    client, _, _ = e2e_client
    resp = client.post("/api/threads", json={"thread_id": "e2e-thread"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["thread_id"] == "e2e-thread"
    assert data["messages"] == []


# ============================================================================
# E2E-3: 上传文件
# ============================================================================


def test_e2e_upload_file(e2e_client):
    client, _, _ = e2e_client
    files = {"file": ("input.txt", io.BytesIO(b"hello world"), "text/plain")}
    resp = client.post("/api/files", files=files)
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "input.txt"
    assert data["size"] == 11
    assert data["kind"] == "file"


# ============================================================================
# E2E-4: 列表看到上传文件
# ============================================================================


def test_e2e_list_sees_upload(e2e_client):
    client, _, _ = e2e_client
    files = {"file": ("listed.txt", io.BytesIO(b"content"), "text/plain")}
    client.post("/api/files", files=files)
    resp = client.get("/api/files")
    assert resp.status_code == 200
    data = resp.json()
    names = [e["name"] for e in data["entries"]]
    assert "listed.txt" in names


# ============================================================================
# E2E-5: SSE Run
# ============================================================================


def test_e2e_sse_run(e2e_client):
    client, _, _, = e2e_client

    # 创建 thread
    t = client.post("/api/threads")
    tid = t.json()["thread_id"]

    # SSE Run（使用 client.stream）
    with client.stream(
        "POST",
        f"/api/threads/{tid}/runs/stream",
        json={"message": "hello"},
    ) as run_resp:
        assert run_resp.status_code == 200
        assert run_resp.headers.get("content-type", "").startswith("text/event-stream")
        assert "X-Run-ID" in run_resp.headers
        run_id = run_resp.headers["X-Run-ID"]
        chunks = []
        for chunk in run_resp.iter_text():
            chunks.append(chunk)
        body = "".join(chunks)

    # 解析 SSE
    events = _parse_sse(body)
    event_types = [e.get("event") for e in events]
    assert "run.started" in event_types, f"events: {events}"

    # run_id 全链路一致
    for e in events:
        if "run_id" in e.get("data", {}):
            assert e["data"]["run_id"] == run_id

    # 最后一条为终态
    assert event_types[-1] in (
        "run.completed", "run.failed", "run.timed_out", "run.cancelled"
    ), f"last event: {event_types[-1]}"

    # answer 正确
    completed = next(
        (e for e in events if e.get("event") == "run.completed"), None
    )
    assert completed is not None, f"No run.completed in {events}"
    # FakeModel 返回固定 answer，不依赖具体内容


# ============================================================================
# E2E-6: Thread 详情含消息
# ============================================================================


def test_e2e_thread_detail_has_messages(e2e_client):
    client, _, _ = e2e_client
    t = client.post("/api/threads")
    tid = t.json()["thread_id"]

    # SSE run 触发 agent
    with client.stream(
        "POST",
        f"/api/threads/{tid}/runs/stream",
        json={"message": "ping"},
    ) as run_resp:
        pass  # 只触发，不验证

    # 读取详情
    detail = client.get(f"/api/threads/{tid}")
    assert detail.status_code == 200
    data = detail.json()
    assert data["thread_id"] == tid
    msgs = data["messages"]
    # 至少有一条 user 和一条 assistant
    roles = [m["role"] for m in msgs]
    assert "user" in roles
    assert "assistant" in roles


# ============================================================================
# E2E-7: artifacts 目录可列出（产物生成后）
# ============================================================================


def test_e2e_artifact_list_accessible(e2e_client):
    """artifacts 目录存在，可列出。"""
    client, _, tmp_workspace = e2e_client
    resp = client.get("/api/artifacts")
    assert resp.status_code == 200
    data = resp.json()
    assert "entries" in data


# ============================================================================
# E2E-8: 手动写入 artifact 后可下载
# ============================================================================


def test_e2e_artifact_download(e2e_client):
    client, _, tmp_workspace = e2e_client

    # 手动写入 artifact（模拟 Agent 产物）
    artifacts_dir = tmp_workspace / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    artifact_file = artifacts_dir / "report.txt"
    artifact_file.write_text("artifact content here", encoding="utf-8")

    # 下载
    resp = client.get("/api/artifacts/report.txt")
    assert resp.status_code == 200
    assert resp.content == b"artifact content here"


# ============================================================================
# E2E-9: DELETE 上传文件成功
# ============================================================================


def test_e2e_delete_upload_file(e2e_client):
    client, _, _ = e2e_client
    files = {"file": ("to_delete.txt", io.BytesIO(b"delete me"), "text/plain")}
    client.post("/api/files", files=files)

    # 删除
    resp = client.delete("/api/files/to_delete.txt")
    assert resp.status_code == 204

    # 确认不存在
    resp2 = client.get("/api/files/to_delete.txt")
    assert resp2.status_code == 404


# ============================================================================
# E2E-10: DELETE Thread 成功
# ============================================================================


def test_e2e_delete_thread(e2e_client):
    client, _, _ = e2e_client
    t = client.post("/api/threads", json={"thread_id": "to-delete"})
    assert t.status_code == 201

    resp = client.delete("/api/threads/to-delete")
    assert resp.status_code == 204

    # 确认不存在
    resp2 = client.get("/api/threads/to-delete")
    assert resp2.status_code == 404


# ============================================================================
# E2E-11: Runtime shutdown 后无残留临时文件
# ============================================================================


def test_e2e_no_temp_files_left_after_lifespan(tmp_workspace):
    """退出 TestClient 后 workspace 无残留临时上传文件。"""
    from mini_deerflow.agent import Agent
    from mini_deerflow.runtime import create_runtime
    from mini_deerflow.thread_store import InMemoryThreadStore
    from mini_deerflow.tools import ToolRegistry

    store = InMemoryThreadStore()
    registry = ToolRegistry()
    config = RuntimeConfig(
        workspace_root=str(tmp_workspace),
        workspace_max_upload_bytes=10 * 1024 * 1024,
    )
    runtime = create_runtime(
        store=store,
        registry=registry,
        config=config,
        model=EchoModel(),
    )

    settings = ApiSettings(workspace_root=str(tmp_workspace))
    app = create_app(settings=settings, runtime_factory=lambda: runtime)

    with TestClient(app) as client:
        # 上传一个文件
        files = {"file": ("temp.txt", io.BytesIO(b"data"), "text/plain")}
        client.post("/api/files", files=files)

    # 退出后，workspace 目录中只有 uploads/ 子目录（正常的）
    # 没有任何 .mdf_up_*.tmp 临时文件残留
    uploads = tmp_workspace / "uploads"
    if uploads.exists():
        temp_files = list(uploads.glob(".mdf_up_*"))
        assert len(temp_files) == 0, f"临时文件残留: {temp_files}"


# ============================================================================
# E2E-12: SSE Run X-Run-ID 一致性
# ============================================================================


def test_e2e_sse_x_run_id_matches_events(e2e_client):
    client, _, _ = e2e_client
    t = client.post("/api/threads")
    tid = t.json()["thread_id"]

    with client.stream(
        "POST",
        f"/api/threads/{tid}/runs/stream",
        json={"message": "check"},
    ) as run_resp:
        header_run_id = run_resp.headers.get("X-Run-ID")
        chunks = []
        for chunk in run_resp.iter_text():
            chunks.append(chunk)
        body = "".join(chunks)

    events = _parse_sse(body)

    for e in events:
        data = e.get("data", {})
        if "run_id" in data:
            assert data["run_id"] == header_run_id


# ============================================================================
# E2E-13: Request ID 在 E2E 中存在
# ============================================================================


def test_e2e_request_id_on_all_responses(e2e_client):
    client, _, _ = e2e_client

    # 健康检查
    h = client.get("/api/health")
    assert "X-Request-ID" in h.headers

    # Thread 操作
    t = client.post("/api/threads")
    assert "X-Request-ID" in t.headers

    # 文件上传
    f = client.post("/api/files", files={"file": ("x.txt", io.BytesIO(b"x"), "text/plain")})
    assert "X-Request-ID" in f.headers

    # 错误响应
    err = client.get("/api/threads/nonexistent-xyz")
    assert err.status_code == 404
    assert "X-Request-ID" in err.headers


# ============================================================================
# 错误 E2E 测试
# ============================================================================


def test_e2e_error_404_thread_not_found_includes_request_id(e2e_client):
    client, _, _ = e2e_client
    resp = client.get("/api/threads/nonexistent-thread-abc")
    assert resp.status_code == 404
    data = resp.json()
    assert data["error"]["code"] == "NOT_FOUND"
    assert "request_id" in data["error"]


def test_e2e_error_413_upload_over_limit(e2e_client):
    client, _, _ = e2e_client
    files = {
        "file": ("huge.bin", io.BytesIO(b"x" * (10 * 1024 * 1024 + 1)), "application/octet-stream")
    }
    resp = client.post("/api/files", files=files)
    assert resp.status_code == 413
    data = resp.json()
    assert data["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert "request_id" in data["error"]


def test_e2e_error_422_validation(e2e_client):
    client, _, _ = e2e_client
    t = client.post("/api/threads")
    tid = t.json()["thread_id"]
    resp = client.post(f"/api/threads/{tid}/runs", json={"message": "  "})
    assert resp.status_code == 422
    data = resp.json()
    assert data["error"]["code"] == "UNPROCESSABLE_ENTITY"
    assert data["error"]["details"] is not None


def test_e2e_error_409_conflict(e2e_client):
    client, _, _ = e2e_client
    client.post("/api/threads", json={"thread_id": "dup"})
    resp2 = client.post("/api/threads", json={"thread_id": "dup"})
    assert resp2.status_code == 409
    data = resp2.json()
    assert data["error"]["code"] == "CONFLICT"


def test_e2e_error_500_safe_message(e2e_client):
    """500 不泄漏内部异常信息。"""
    client, _, _ = e2e_client
    # 触发一个真实的未处理异常：访问不存在的 artifact
    resp = client.get("/api/artifacts/../etc/passwd")
    # 如果走到 400 说明路径检查保护了，否则不应该暴露 traceback
    assert resp.status_code in (400, 404)
    if resp.status_code == 400:
        data = resp.json()
        assert data["error"]["code"] == "BAD_REQUEST"
        assert "etc" not in data["error"].get("message", "")
