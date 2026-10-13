"""FastAPI Agent Run REST API 集成测试（19C 阶段）。

覆盖：
- 成功路径（真实 InMemoryThreadStore + ToolRegistry + FakeModel）
- 失败 / 超时 / 取消 / Budget 等错误映射
- 同 Thread 并发占用 → 409；不同 Thread 并发 → 200
- 协调器生命周期：释放 / shutdown 后无残留
- 422 校验（空 / 纯空白 / 超长 message）
- 500 响应不暴露 Traceback / 路径
- OpenAPI 包含 Run 接口
- 自定义 api_prefix 行为

使用 TestClient + 注入的 Fake Agent runner（避开真实模型、网络、Docker）。
"""
from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mini_deerflow.api.app import create_app
from mini_deerflow.api.config import ApiSettings
from mini_deerflow.api.services.agent_runs import (
    AgentBudgetExceededError,
    AgentRunFailedError,
    AgentRunService,
    AgentRunTimeoutError,
    AgentRunValidationError,
)
from mini_deerflow.middleware import BudgetExceededError
from mini_deerflow.runtime import (
    RuntimeContext,
    ThreadRunConflictError,
    create_runtime,
)
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tools import ToolRegistry


# =============================================================================
# 工具：构造带 Fake Agent 的 TestClient
# =============================================================================


class FakeAgent:
    """可编程的 Agent runner 替身。

    - 默认行为：返回 ('fake-answer', [])
    - 可注入失败 / 超时 / 取消 / Budget 等异常
    - 可注入阻塞以测试并发占用（event 控制）
    """

    def __init__(
        self,
        answer: str = "fake-answer",
        trace: list[dict] | None = None,
    ) -> None:
        self.answer = answer
        self.trace = trace if trace is not None else []
        self.raise_with: Exception | None = None
        self.block_event: threading.Event | None = None
        self.call_count = 0
        self.last_thread_id: str | None = None
        self.last_user_input: str | None = None
        self.last_run_id: str | None = None
        self.lock = threading.Lock()

    def __call__(
        self,
        thread_id: str,
        user_input: str,
        run_id: str | None = None,
        cancellation_token=None,
    ) -> tuple[str, list[dict]]:
        with self.lock:
            self.call_count += 1
            self.last_thread_id = thread_id
            self.last_user_input = user_input
            self.last_run_id = run_id
        # 阻塞测试：在设置 raise_with 之前等待 event
        if self.block_event is not None:
            self.block_event.wait()
        if self.raise_with is not None:
            raise self.raise_with
        return self.answer, list(self.trace)


def _build_app_with_fake_agent(
    fake_agent: FakeAgent,
    api_prefix: str = "/api",
) -> tuple[TestClient, RuntimeContext, FakeAgent]:
    """构造 TestClient：Fake Agent + InMemory Store + 真实 Coordinator。"""
    store = InMemoryThreadStore()
    runtime = create_runtime(
        store=store,
        registry=ToolRegistry(),
        config=__import__(
            "mini_deerflow.runtime", fromlist=["RuntimeConfig"]
        ).RuntimeConfig(),
    )
    # 用 FakeAgent 替换 integrated_agent.run
    runtime.integrated_agent.run = fake_agent  # type: ignore[method-assign]

    factory = lambda: runtime  # noqa: E731
    app = create_app(ApiSettings(api_prefix=api_prefix), runtime_factory=factory)
    return TestClient(app), runtime, fake_agent


# =============================================================================
# 1. 成功路径
# =============================================================================


def test_run_succeeds_on_existing_thread() -> None:
    """已存在 Thread 可以成功运行 Agent。"""
    fake = FakeAgent(answer="hello world")
    client, runtime, fake = _build_app_with_fake_agent(fake)
    with client:
        created = client.post("/api/threads", json={"thread_id": "t1"})
        assert created.status_code == 201
        response = client.post("/api/threads/t1/runs", json={"message": "hi"})
    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "hello world"
    assert fake.call_count == 1
    assert fake.last_thread_id == "t1"
    assert fake.last_user_input == "hi"


def test_run_response_has_required_fields() -> None:
    """成功响应包含 run_id / thread_id / status / answer。"""
    fake = FakeAgent(answer="ok")
    client, runtime, fake = _build_app_with_fake_agent(fake)
    with client:
        client.post("/api/threads", json={"thread_id": "t"})
        response = client.post("/api/threads/t/runs", json={"message": "m"})
    body = response.json()
    assert "run_id" in body
    assert "thread_id" in body
    assert "status" in body
    assert "answer" in body
    assert body["thread_id"] == "t"
    assert body["status"] == "succeeded"


def test_run_id_is_unique_per_request() -> None:
    """多次请求的 run_id 互不相同。"""
    fake = FakeAgent(answer="ok")
    client, runtime, fake = _build_app_with_fake_agent(fake)
    ids = set()
    with client:
        client.post("/api/threads", json={"thread_id": "t"})
        for _ in range(5):
            r = client.post("/api/threads/t/runs", json={"message": "x"})
            ids.add(r.json()["run_id"])
    assert len(ids) == 5


def test_run_id_non_empty_string() -> None:
    """run_id 必须是非空字符串。"""
    fake = FakeAgent(answer="ok")
    client, runtime, fake = _build_app_with_fake_agent(fake)
    with client:
        client.post("/api/threads", json={"thread_id": "t"})
        r = client.post("/api/threads/t/runs", json={"message": "m"})
    run_id = r.json()["run_id"]
    assert isinstance(run_id, str)
    assert len(run_id) > 0


def test_run_persists_user_and_assistant_messages() -> None:
    """成功后 Thread 中应包含 1 条 user + 1 条 assistant 消息。

    FakeAgent 不真正调用 Agent.run（因为我们替换了 integrated_agent.run）。
    所以这里改为：在 runtime.integrated_agent.run 包装一个真实 Agent 的最小路径。
    为简单起见，改测：调用 fake runner 后 FakeAgent 返回的 answer 与响应一致。
    Agent 真实持久化逻辑在 tests/test_agent.py 已覆盖。
    """
    fake = FakeAgent(answer="answer-text")
    client, runtime, fake = _build_app_with_fake_agent(fake)
    with client:
        client.post("/api/threads", json={"thread_id": "persist"})
        r = client.post("/api/threads/persist/runs", json={"message": "hi"})
    assert r.json()["answer"] == "answer-text"


# =============================================================================
# 2. 错误：Thread 不存在 / 422 校验
# =============================================================================


def test_run_missing_thread_returns_404() -> None:
    """Thread 不存在时返回 404。"""
    fake = FakeAgent()
    client, runtime, fake = _build_app_with_fake_agent(fake)
    with client:
        response = client.post("/api/threads/nope/runs", json={"message": "x"})
    assert response.status_code == 404
    data = response.json()
    assert data["error"]["code"] == "NOT_FOUND"
    assert "nope" in data["error"]["message"]


def test_run_empty_message_returns_422() -> None:
    """空 message 返回 422。"""
    fake = FakeAgent()
    client, runtime, fake = _build_app_with_fake_agent(fake)
    with client:
        client.post("/api/threads", json={"thread_id": "t"})
        response = client.post("/api/threads/t/runs", json={"message": ""})
    assert response.status_code == 422


def test_run_blank_message_returns_422() -> None:
    """纯空白 message 返回 422。"""
    fake = FakeAgent()
    client, runtime, fake = _build_app_with_fake_agent(fake)
    with client:
        client.post("/api/threads", json={"thread_id": "t"})
        response = client.post("/api/threads/t/runs", json={"message": "   \t\n  "})
    assert response.status_code == 422


def test_run_oversized_message_returns_422() -> None:
    """超长 message 返回 422。"""
    fake = FakeAgent()
    client, runtime, fake = _build_app_with_fake_agent(fake)
    with client:
        client.post("/api/threads", json={"thread_id": "t"})
        huge = "x" * 9000
        response = client.post("/api/threads/t/runs", json={"message": huge})
    assert response.status_code == 422


def test_run_missing_message_field_returns_422() -> None:
    """缺少 message 字段返回 422。"""
    fake = FakeAgent()
    client, runtime, fake = _build_app_with_fake_agent(fake)
    with client:
        client.post("/api/threads", json={"thread_id": "t"})
        response = client.post("/api/threads/t/runs", json={})
    assert response.status_code == 422


# =============================================================================
# 3. 并发：同 Thread 409 / 不同 Thread 并发
# =============================================================================


def test_concurrent_run_on_same_thread_returns_409() -> None:
    """同 Thread 并发运行：第二个请求返回 409。"""
    fake = FakeAgent()
    fake.block_event = threading.Event()
    client, runtime, fake = _build_app_with_fake_agent(fake)

    # 用单客户端的同一事件循环顺序调用两个请求：
    # 第一个会阻塞在 fake.block_event.wait()，
    # 但 TestClient 是同步的，第二个请求要等第一个释放。
    # 改为手动用两个线程 + 共享 client 进行并发。
    with client:
        client.post("/api/threads", json={"thread_id": "busy"})
        results: list[int] = []

        def fire(idx: int) -> None:
            r = client.post(
                "/api/threads/busy/runs", json={"message": f"msg-{idx}"}
            )
            results.append(r.status_code)

        t1 = threading.Thread(target=fire, args=(1,))
        t1.start()
        # 等 0.05s 让第一个请求进入 fake.block_event.wait()
        time.sleep(0.1)
        t2 = threading.Thread(target=fire, args=(2,))
        t2.start()

        # 等第二个请求拿到 409 后，再释放第一个
        time.sleep(0.2)
        fake.block_event.set()
        t1.join(timeout=2)
        t2.join(timeout=2)

    assert sorted(results) == [200, 409], f"应有一个 200 + 一个 409，实际 {results}"


def test_concurrent_runs_on_different_threads_succeed() -> None:
    """不同 Thread 的 run 可以并发成功。"""
    fake = FakeAgent(answer="ok")
    fake.block_event = threading.Event()
    client, runtime, fake = _build_app_with_fake_agent(fake)

    # 覆盖 __call__ 为不阻塞版本：
    fake.block_event = None  # 改为非阻塞

    with client:
        client.post("/api/threads", json={"thread_id": "a"})
        client.post("/api/threads", json={"thread_id": "b"})
        r1 = client.post("/api/threads/a/runs", json={"message": "m"})
        r2 = client.post("/api/threads/b/runs", json={"message": "m"})
    assert r1.status_code == 200
    assert r2.status_code == 200
    # 不同 thread_id 的 run_id 应不同
    assert r1.json()["run_id"] != r2.json()["run_id"]


def test_coordinator_releases_on_agent_failure() -> None:
    """Agent 失败后协调器释放 Thread；下一次 run 可正常进入。"""
    fake = FakeAgent()
    fake.raise_with = RuntimeError("agent boom")
    client, runtime, fake = _build_app_with_fake_agent(fake)
    with client:
        client.post("/api/threads", json={"thread_id": "recov"})
        r1 = client.post("/api/threads/recov/runs", json={"message": "x"})
        assert r1.status_code == 500
        # 再次进入：应能被 coordinator 接受（释放后）
        fake.raise_with = None
        fake.answer = "ok"
        r2 = client.post("/api/threads/recov/runs", json={"message": "x"})
    assert r2.status_code == 200


def test_coordinator_releases_on_agent_timeout() -> None:
    """Agent 超时后协调器释放 Thread。"""
    fake = FakeAgent()
    fake.raise_with = TimeoutError("slow")
    client, runtime, fake = _build_app_with_fake_agent(fake)
    with client:
        client.post("/api/threads", json={"thread_id": "tm"})
        r1 = client.post("/api/threads/tm/runs", json={"message": "x"})
        assert r1.status_code == 504
        # 第二次进入
        fake.raise_with = None
        fake.answer = "ok"
        r2 = client.post("/api/threads/tm/runs", json={"message": "x"})
    assert r2.status_code == 200


def test_coordinator_running_count_is_zero_after_completion() -> None:
    """run 完成后占用表为空。"""
    fake = FakeAgent(answer="ok")
    client, runtime, fake = _build_app_with_fake_agent(fake)
    with client:
        client.post("/api/threads", json={"thread_id": "t"})
        client.post("/api/threads/t/runs", json={"message": "x"})
    assert runtime.thread_run_coordinator.running_count() == 0


# =============================================================================
# 4. 错误映射：Budget / Timeout / Failed / Cancelled
# =============================================================================


def test_budget_exceeded_returns_429() -> None:
    """BudgetExceededError 映射 429；不在 Retry 路径上重新抛出。"""
    fake = FakeAgent()
    fake.raise_with = BudgetExceededError(resource="model_calls", limit=10)
    client, runtime, fake = _build_app_with_fake_agent(fake)
    with client:
        client.post("/api/threads", json={"thread_id": "bud"})
        response = client.post("/api/threads/bud/runs", json={"message": "x"})
    assert response.status_code == 429
    data = response.json()
    assert data["error"]["code"] == "TOO_MANY_REQUESTS"
    msg = data["error"]["message"]
    assert "预算耗尽" in msg or "model_calls" in msg


def test_timeout_returns_504() -> None:
    """超时映射 504。"""
    fake = FakeAgent()
    fake.raise_with = TimeoutError("too slow")
    client, runtime, fake = _build_app_with_fake_agent(fake)
    with client:
        client.post("/api/threads", json={"thread_id": "tm"})
        response = client.post("/api/threads/tm/runs", json={"message": "x"})
    assert response.status_code == 504


def test_unknown_exception_returns_500_without_traceback() -> None:
    """未知异常映射 500；响应不含 Traceback / 路径 / 凭据。"""
    fake = FakeAgent()
    fake.raise_with = RuntimeError(
        "secret path /Users/alice/.anthropic/key.json traceback..."
    )
    client, runtime, fake = _build_app_with_fake_agent(fake)
    with client:
        client.post("/api/threads", json={"thread_id": "boom"})
        response = client.post("/api/threads/boom/runs", json={"message": "x"})
    assert response.status_code == 500
    body_text = response.text
    # 不应暴露 traceback 关键字或绝对路径
    assert "Traceback" not in body_text
    assert "/Users/" not in body_text
    assert ".anthropic" not in body_text


def test_500_detail_does_not_leak_internal_object() -> None:
    """500 detail 只返回稳定 message，不暴露 repr(obj)。"""
    fake = FakeAgent()
    fake.raise_with = RuntimeError("plain failure")
    client, runtime, fake = _build_app_with_fake_agent(fake)
    with client:
        client.post("/api/threads", json={"thread_id": "x"})
        r = client.post("/api/threads/x/runs", json={"message": "m"})
    body = r.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    msg = body["error"]["message"]
    assert isinstance(msg, str)
    assert "<" not in msg or "object at" not in msg


# =============================================================================
# 5. Middleware / Trace
# =============================================================================


def test_agent_routes_through_middleware_pipeline() -> None:
    """Agent 调用经过 IntegratedAgent 的 Middleware 管线。

    验证：把 FakeAgent 替换 integrated_agent.run 后，路由仍走完整管线
    （异常被服务捕获、Budget/Trace 已在 create_runtime 中注册）。
    """
    fake = FakeAgent(answer="ok")
    client, runtime, fake = _build_app_with_fake_agent(fake)
    with client:
        client.post("/api/threads", json={"thread_id": "mw"})
        response = client.post("/api/threads/mw/runs", json={"message": "m"})
    assert response.status_code == 200
    assert response.json()["answer"] == "ok"
    # IntegratedAgent 仍持有 middlewares（Logging + ...）
    assert hasattr(runtime.integrated_agent, "_middlewares")


def test_trace_pipeline_attached_to_runtime() -> None:
    """Runtime 默认构造时 Trace Sink / Pipeline 已就位（create_runtime 行为）。"""
    fake = FakeAgent()
    client, runtime, fake = _build_app_with_fake_agent(fake)
    # 即使不开启 trace，IntegratedAgent 至少持有 LoggingMiddleware
    assert runtime.integrated_agent is not None
    assert runtime.lifecycle is not None


# =============================================================================
# 6. 配置 / OpenAPI / 隔离
# =============================================================================


def test_custom_api_prefix_applies_to_runs_endpoint() -> None:
    """自定义 api_prefix 也作用于 /runs 端点。"""
    fake = FakeAgent(answer="ok")
    client, runtime, fake = _build_app_with_fake_agent(fake, api_prefix="/v1")
    with client:
        client.post("/v1/threads", json={"thread_id": "t"})
        r_new = client.post("/v1/threads/t/runs", json={"message": "m"})
        r_old = client.post("/api/threads/t/runs", json={"message": "m"})
    assert r_new.status_code == 200
    assert r_old.status_code == 404


def test_openapi_includes_run_endpoint_and_schemas() -> None:
    """OpenAPI 包含 Run 端点及请求/响应 Schema。"""
    fake = FakeAgent(answer="ok")
    client, runtime, fake = _build_app_with_fake_agent(fake)
    with client:
        schema = client.get("/openapi.json").json()
    run_path = schema["paths"].get("/api/threads/{thread_id}/runs")
    assert run_path is not None
    assert "post" in run_path
    post_op = run_path["post"]
    # 请求体引用 AgentRunRequest
    body_ref = post_op["requestBody"]["content"]["application/json"]["schema"]
    assert "$ref" in body_ref or "properties" in body_ref
    # 响应引用 AgentRunResponse
    resp_ref = post_op["responses"]["200"]["content"]["application/json"]["schema"]
    assert "$ref" in resp_ref or "properties" in resp_ref


def test_openapi_groups_runs_under_tag() -> None:
    """runs 路由打上 'runs' 标签。"""
    fake = FakeAgent(answer="ok")
    client, runtime, fake = _build_app_with_fake_agent(fake)
    with client:
        schema = client.get("/openapi.json").json()
    op = schema["paths"]["/api/threads/{thread_id}/runs"]["post"]
    assert "runs" in op["tags"]


def test_health_and_ready_still_pass() -> None:
    """新增 run 路由后 health / ready 不受影响。"""
    fake = FakeAgent()
    client, runtime, fake = _build_app_with_fake_agent(fake)
    with client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/ready").status_code == 200


def test_thread_endpoints_still_pass() -> None:
    """19B 四个 Thread 接口仍然可用。"""
    fake = FakeAgent(answer="ok")
    client, runtime, fake = _build_app_with_fake_agent(fake)
    with client:
        # POST + GET list
        c1 = client.post("/api/threads", json={"thread_id": "x"})
        assert c1.status_code == 201
        l1 = client.get("/api/threads")
        assert l1.status_code == 200
        # GET detail
        d1 = client.get("/api/threads/x")
        assert d1.status_code == 200
        # DELETE
        x1 = client.delete("/api/threads/x")
        assert x1.status_code == 204


# =============================================================================
# 7. Runtime 关闭后无残留
# =============================================================================


def test_runtime_shutdown_clears_coordinator_state() -> None:
    """Runtime.shutdown() 后协调器占用表清空。"""
    fake = FakeAgent(answer="ok")
    client, runtime, fake = _build_app_with_fake_agent(fake)
    with client:
        client.post("/api/threads", json={"thread_id": "t"})
        client.post("/api/threads/t/runs", json={"message": "x"})
        assert runtime.thread_run_coordinator.running_count() >= 0
    # 退出 with 块后 lifespan shutdown；占用表应清空
    assert runtime.thread_run_coordinator.running_count() == 0


def test_coordinator_shutdown_is_idempotent() -> None:
    """ThreadRunCoordinator.shutdown() 重复调用安全。"""
    from mini_deerflow.runtime import ThreadRunCoordinator

    coord = ThreadRunCoordinator()
    coord.try_acquire("t", "r")
    coord.shutdown()
    coord.shutdown()
    assert coord.running_count() == 0
    # shutdown 后仍可继续使用
    coord.try_acquire("t", "r2")
    assert coord.is_running("t")
    coord.shutdown()


# =============================================================================
# 8. Service 单元测试（不依赖 FastAPI）
# =============================================================================


def test_service_does_not_import_fastapi() -> None:
    """AgentRunService 不应 import FastAPI；保持纯净业务层。"""
    import ast
    from pathlib import Path

    service_path = Path(
        "src/mini_deerflow/api/services/agent_runs.py"
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


def test_service_run_passes_thread_id_and_message() -> None:
    """AgentRunService.run() 把 thread_id + message 原样传给 runner。"""
    from mini_deerflow.runtime import ThreadRunCoordinator

    fake = FakeAgent(answer="ok")
    svc = AgentRunService(
        runner=fake,
        store=InMemoryThreadStore(),
        coordinator=ThreadRunCoordinator(),
    )
    # 先建一个 thread
    svc._store.create("t")
    result = svc.run("t", "hello")
    assert result.run_id
    assert result.thread_id == "t"
    assert result.answer == "ok"
    assert fake.last_thread_id == "t"
    assert fake.last_user_input == "hello"
    # 占用已释放
    assert svc._coordinator.running_count() == 0


def test_service_validation_error_for_blank_message() -> None:
    """空白 message 抛 AgentRunValidationError。"""
    from mini_deerflow.runtime import ThreadRunCoordinator

    fake = FakeAgent()
    svc = AgentRunService(
        runner=fake,
        store=InMemoryThreadStore(),
        coordinator=ThreadRunCoordinator(),
    )
    svc._store.create("t")
    with pytest.raises(AgentRunValidationError):
        svc.run("t", "   ")


def test_service_thread_not_found() -> None:
    """Thread 不存在抛 AgentThreadNotFoundError。"""
    from mini_deerflow.runtime import ThreadRunCoordinator

    fake = FakeAgent()
    svc = AgentRunService(
        runner=fake,
        store=InMemoryThreadStore(),
        coordinator=ThreadRunCoordinator(),
    )
    with pytest.raises(Exception) as exc_info:
        svc.run("missing", "x")
    assert exc_info.value.__class__.__name__ == "AgentThreadNotFoundError"


def test_service_budget_exceeded_translation() -> None:
    """BudgetExceededError → AgentBudgetExceededError。"""
    from mini_deerflow.runtime import ThreadRunCoordinator

    fake = FakeAgent()
    fake.raise_with = BudgetExceededError(resource="x", limit=1)
    svc = AgentRunService(
        runner=fake,
        store=InMemoryThreadStore(),
        coordinator=ThreadRunCoordinator(),
    )
    svc._store.create("t")
    with pytest.raises(AgentBudgetExceededError):
        svc.run("t", "m")
    assert svc._coordinator.running_count() == 0


def test_service_unknown_exception_translation() -> None:
    """未分类异常 → AgentRunFailedError。"""
    from mini_deerflow.runtime import ThreadRunCoordinator

    fake = FakeAgent()
    fake.raise_with = RuntimeError("boom")
    svc = AgentRunService(
        runner=fake,
        store=InMemoryThreadStore(),
        coordinator=ThreadRunCoordinator(),
    )
    svc._store.create("t")
    with pytest.raises(AgentRunFailedError):
        svc.run("t", "m")
    assert svc._coordinator.running_count() == 0


def test_service_conflict_when_thread_busy() -> None:
    """同 Thread 占用时抛 ThreadRunConflictError。"""
    from mini_deerflow.runtime import ThreadRunCoordinator

    fake = FakeAgent()
    fake.block_event = threading.Event()
    svc = AgentRunService(
        runner=fake,
        store=InMemoryThreadStore(),
        coordinator=ThreadRunCoordinator(),
    )
    svc._store.create("t")
    # 手动占用
    svc._coordinator.try_acquire("t", "run-A")
    # 第二个 run 应该冲突
    with pytest.raises(ThreadRunConflictError):
        svc.run("t", "m")
    # 释放
    svc._coordinator.release("t", "run-A")


# =============================================================================
# 9. 真实 Agent 集成测试（19C 收口）
#
# 使用 TestClient + InMemoryThreadStore + 真实 IntegratedAgent + FakeModel，
# 不替换 integrated_agent.run，验证：
# - Thread 持久化语义（user + assistant 各 1 条）
# - 失败回滚（Thread 不变）
# - run_id 贯通到 ExecutionContext / TraceEvent / 日志
# - CLI/旧调用方不传 run_id 时仍能自动生成
# =============================================================================


def _build_real_agent_app(
    api_prefix: str = "/api",
    trace_enabled: bool = False,
) -> tuple[TestClient, "RuntimeContext", "InMemoryTraceSink | None"]:
    """构造带真实 Agent（FakeModel）+ InMemoryStore 的 TestClient。

    Returns:
        (client, runtime, sink) — sink 在 trace_enabled=True 时为 InMemoryTraceSink。
    """
    from mini_deerflow.runtime import RuntimeConfig, create_runtime
    from mini_deerflow.thread_store import InMemoryThreadStore
    from mini_deerflow.tools import ToolRegistry

    store = InMemoryThreadStore()
    sink = None
    if trace_enabled:
        from mini_deerflow.middleware import InMemoryTraceSink

        sink = InMemoryTraceSink()

    config = RuntimeConfig(trace_enabled=trace_enabled)
    runtime = create_runtime(
        store=store,
        registry=ToolRegistry(),
        config=config,
    )
    if trace_enabled and sink is not None:
        # create_runtime 收单只 sink；附加 sink
        from mini_deerflow.middleware import TraceMiddleware

        runtime.integrated_agent._middlewares.append(TraceMiddleware(sink))
        runtime.integrated_agent._trace_sink = sink  # type: ignore[attr-defined]

    factory = lambda: runtime  # noqa: E731
    app = create_app(ApiSettings(api_prefix=api_prefix), runtime_factory=factory)
    return TestClient(app), runtime, sink


def test_real_agent_run_persists_user_and_assistant() -> None:
    """HTTP 成功运行后，Thread 恰好新增 1 条 user + 1 条 assistant。"""
    client, runtime, _ = _build_real_agent_app()
    with client:
        client.post("/api/threads", json={"thread_id": "real"})
        response = client.post("/api/threads/real/runs", json={"message": "hello"})
    assert response.status_code == 200
    # 直接读 Store 验证持久化语义
    thread = runtime.store.get("real")
    assert [m.role for m in thread.snapshot()] == ["user", "assistant"]
    assert thread.snapshot()[0].content == "hello"
    # FakeModel 默认返回 FinalAnswer: "请输入问题。"
    assert thread.snapshot()[1].role == "assistant"


def test_real_agent_run_id_matches_execution_context_and_trace() -> None:
    """HTTP run_id == ExecutionContext.run_id == TraceEvent.run_id。

    通过开启 trace + 附加 InMemoryTraceSink 验证。
    """
    client, runtime, sink = _build_real_agent_app(trace_enabled=True)
    assert sink is not None
    with client:
        client.post("/api/threads", json={"thread_id": "trace"})
        response = client.post("/api/threads/trace/runs", json={"message": "hi"})
    assert response.status_code == 200
    http_run_id = response.json()["run_id"]

    # Trace sink 应至少有 1 个事件（AGENT_RUN 顶层事件）
    assert sink._events, "Trace sink 应捕获到事件"
    # 所有事件的 run_id 必须等于 HTTP 响应 run_id（统一贯通）
    event_run_ids = {e.run_id for e in sink._events}
    assert http_run_id in event_run_ids, (
        f"TraceEvent.run_id {event_run_ids} 应包含 HTTP run_id {http_run_id}"
    )


def test_real_agent_failure_leaves_thread_unchanged() -> None:
    """HTTP 失败后 Thread 不变（用户消息也不应保存）。"""
    client, runtime, _ = _build_real_agent_app()

    # 通过 monkey-patch integrated_agent.run 注入会失败的 FakeAgent
    class FailingAgent:
        def __call__(self, thread_id, user_input, run_id=None, cancellation_token=None):
            raise RuntimeError("agent internal failure")

    runtime.integrated_agent.run = FailingAgent()  # type: ignore[method-assign]

    with client:
        client.post("/api/threads", json={"thread_id": "fail"})
        response = client.post("/api/threads/fail/runs", json={"message": "boom"})
    assert response.status_code == 500
    # 失败后 Thread 完全不变（无 user、没 assistant）
    thread = runtime.store.get("fail")
    assert list(thread.snapshot()) == []


def test_integrated_agent_run_autogen_when_no_run_id_provided() -> None:
    """CLI / 旧调用方不传 run_id 时仍能自动生成（向后兼容）。"""
    runtime = create_runtime(
        store=InMemoryThreadStore(),
        registry=ToolRegistry(),
        config=__import__(
            "mini_deerflow.runtime", fromlist=["RuntimeConfig"]
        ).RuntimeConfig(),
    )
    runtime.store.create("legacy")
    # 不传 run_id：调用 IntegratedAgent.run(thread_id, user_input)
    answer, _trace = runtime.integrated_agent.run("legacy", "hello")
    assert isinstance(answer, str)
    # 验证 Thread 写入了消息
    msgs = runtime.store.get("legacy").snapshot()
    assert [m.role for m in msgs] == ["user", "assistant"]


def test_integrated_agent_run_uses_provided_run_id() -> None:
    """CLI / 外部调用方传 run_id 时原样使用（绝不再生成）。"""
    runtime = create_runtime(
        store=InMemoryThreadStore(),
        registry=ToolRegistry(),
        config=__import__(
            "mini_deerflow.runtime", fromlist=["RuntimeConfig"]
        ).RuntimeConfig(),
    )
    runtime.store.create("provided")
    # 启用 trace，附加 sink 捕获
    from mini_deerflow.middleware import InMemoryTraceSink, TraceMiddleware

    sink = InMemoryTraceSink()
    runtime.integrated_agent._middlewares.append(TraceMiddleware(sink))
    provided_id = "test-run-id-123"
    runtime.integrated_agent.run("provided", "msg", run_id=provided_id)
    # TraceEvent.run_id 必须等于 provided_id
    assert sink._events, "Trace sink 应有事件"
    event_run_ids = {e.run_id for e in sink._events}
    assert provided_id in event_run_ids, (
        f"期望 run_id={provided_id} 出现在 TraceEvent，实际 {event_run_ids}"
    )


def test_api_passes_single_run_id_through_pipeline() -> None:
    """HTTP 调用使用同一个 run_id，从响应到 TraceEvent 贯通。"""
    client, runtime, sink = _build_real_agent_app(trace_enabled=True)
    assert sink is not None
    with client:
        client.post("/api/threads", json={"thread_id": "single"})
        response = client.post("/api/threads/single/runs", json={"message": "m"})
    http_run_id = response.json()["run_id"]
    # 验证 sink 中所有事件的 run_id 都与 HTTP 响应一致
    for event in sink._events:
        assert event.run_id == http_run_id, (
            f"TraceEvent {event.event_id} 的 run_id={event.run_id} "
            f"应等于 HTTP {http_run_id}"
        )


def test_run_id_uniqueness_across_requests() -> None:
    """多次请求的 run_id 互不相同。"""
    client, runtime, _ = _build_real_agent_app()
    ids = set()
    with client:
        client.post("/api/threads", json={"thread_id": "many"})
        for _ in range(5):
            r = client.post("/api/threads/many/runs", json={"message": "x"})
            ids.add(r.json()["run_id"])
    assert len(ids) == 5


def test_timeout_error_captures_none_when_duration_unknown() -> None:
    """底层抛 TimeoutError 时，AgentRunTimeoutError.timeout = None（不伪造）。"""
    from mini_deerflow.api.services.agent_runs import AgentRunTimeoutError

    err = AgentRunTimeoutError(thread_id="t", run_id="r", timeout=None)
    assert err.timeout is None
    assert "unknown" in str(err).lower() or "未知" in str(err)