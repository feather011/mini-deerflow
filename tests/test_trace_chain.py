"""TRACE-01：一次运行的执行链（run_id / span_id / 父子关联 / 终态）。

对应验收三行：

1. **贯通 run_id、span_id、父子关联** —— 从真实 API 发起一次「模型 → 工具 → 模型」，
   能沿 `parent_id` 还原完整调用链；
2. **Trace 状态按调用隔离** —— 两个会话并发时各自记录完整、互不串线；
3. **正确结束成功 / 失败 / 取消 / 超时** —— 无悬空 running、无重复终态，
   错误保留安全原因。

不涉及真实付费模型：全部用脚本模型 + 假客户端。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mini_deerflow.api.app import create_app
from mini_deerflow.api.config import ApiSettings
from mini_deerflow.api.services.agent_runs import (
    AgentRunCancelledError,
    AgentRunService,
    AgentRunTimeoutError,
)
from mini_deerflow.middleware import (
    BudgetExceededError,
    CancellationToken,
    ExecutionContext,
    InMemoryTraceSink,
    JSONLTraceSink,
    Middleware,
    MiddlewarePipeline,
    OperationType,
    RetryAgainError,
    TraceEvent,
    TraceMiddleware,
    classify_terminal_status,
)
from mini_deerflow.model_errors import (
    ModelAuthError,
    ModelConfigError,
    ModelEmptyResponseError,
    ModelError,
    ModelProtocolError,
    ModelRateLimitError,
    ModelServerError,
    ModelTimeoutError,
)
from mini_deerflow.models import FakeModel, FinalAnswer
from mini_deerflow.runtime import RuntimeConfig, ThreadRunCoordinator, create_runtime
from mini_deerflow.subagent_executor import SubagentManager
from mini_deerflow.subagent_tools import create_subagent_tools
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tools import ToolRegistry, create_default_registry
from mini_deerflow.trace_safety import safe_trace_detail


# =============================================================================
# 公共助手
# =============================================================================


def _wait_until(predicate, timeout: float = 10.0, interval: float = 0.02) -> bool:
    """轮询等待条件成立；超时返回 False。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _terminals(events: list[TraceEvent]) -> list[TraceEvent]:
    """终态记录（status != running）。"""
    return [e for e in events if e.status != "running"]


def _spans(events: list[TraceEvent], operation: str | None = None) -> list[TraceEvent]:
    """按 operation 取终态 span（不含上下文事件）。"""
    return [
        e for e in _terminals(events)
        if e.kind == "span" and (operation is None or e.operation == operation)
    ]


def _assert_chain_sane(events: list[TraceEvent]) -> None:
    """通用扫描器：每个 event_id 恰一条终态；有 running 记录的必有终态。

    两种事件形态都要吃：
    - 中间件的 span：两条记录（running + 终态）共用一个 event_id；
    - 手工发的事件（上下文事件 / 子代理事件）：一条即终态。
    """
    grouped: dict[str, list[TraceEvent]] = {}
    for event in events:
        grouped.setdefault(event.event_id, []).append(event)
    for event_id, records in grouped.items():
        terminals = [r for r in records if r.status != "running"]
        assert len(terminals) == 1, (
            f"event_id={event_id} 的终态记录数应为 1，实际 {len(terminals)}"
            f"（状态：{[r.status for r in records]}）"
        )
        running = [r for r in records if r.status == "running"]
        assert len(running) <= 1, f"event_id={event_id} 有多条 running 记录"


def _build_runtime(model=None, *, config: RuntimeConfig | None = None, store=None):
    """构造带 Trace sink 的运行时。"""
    cfg = config or RuntimeConfig(trace_enabled=True)
    runtime = create_runtime(
        store=store or InMemoryThreadStore(),
        registry=create_default_registry(),
        config=cfg,
        model=model if model is not None else FakeModel(),
    )
    return runtime


def _api_client(runtime) -> TestClient:
    return TestClient(
        create_app(ApiSettings(), runtime_factory=lambda: runtime)
    )


# =============================================================================
# 脚本模型
# =============================================================================


class _RaisingModel:
    """每次调用都抛异常的模型。"""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def decide(self, messages, tools=None):
        raise self._exc


class _SlowStreamModel:
    """慢速流式模型：用于触发模型调用超时。"""

    def __init__(self, delay: float = 0.5) -> None:
        self._delay = delay

    def decide(self, messages, tools=None):  # pragma: no cover - 走流式路径
        return FinalAnswer(content="unused")

    def stream_decide(self, messages, tools=None):
        time.sleep(self._delay)
        yield ("decision", FinalAnswer(content="late answer"))


class _RetryOnceMiddleware(Middleware):
    """把异常转成 `RetryAgainError`，让管线重试一次。"""

    def before(self, ctx) -> None:
        pass

    def success(self, ctx, result):
        return result

    def error(self, ctx, exc):
        return RetryAgainError(exc, 1)

    def finally_(self, ctx) -> None:
        pass


# =============================================================================
# 1. 真实 API 的一次「模型 → 工具 → 模型」
# =============================================================================


def test_真实API_模型工具模型_能还原调用链() -> None:
    """从真实 API 发起一次运行，能沿 parent_id 还原完整调用链。"""
    runtime = _build_runtime()
    client = _api_client(runtime)
    with client:
        created = client.post("/api/threads", json={"thread_id": "t-trace"})
        assert created.status_code == 201
        response = client.post(
            "/api/threads/t-trace/runs", json={"message": "计算 1+1"}
        )
    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]

    events = runtime.trace_sink.get_events()

    # ① run_id 贯通：这次运行的**所有**事件都挂在同一个 run_id 下
    #    （过去模型调用事件的 run_id 是空串，按 run_id 查不到）。
    assert {e.run_id for e in events} == {run_id}, (
        f"事件分属多个 run_id：{sorted({e.run_id for e in events})}"
    )

    # ② 父子关联：模型 → 工具 → 模型
    agent_runs = _spans(events, "AGENT_RUN")
    models = _spans(events, "MODEL_CALL")
    tools = _spans(events, "TOOL_CALL")
    assert len(agent_runs) == 1, f"AGENT_RUN span 数 = {len(agent_runs)}"
    assert len(models) == 2, f"MODEL_CALL span 数 = {len(models)}"
    assert len(tools) == 1, f"TOOL_CALL span 数 = {len(tools)}"

    agent_run = agent_runs[0]
    tool_span = tools[0]
    # 终态记录按产生顺序：M1 → T1 → M2
    first_model, second_model = models

    assert first_model.parent_id == agent_run.event_id
    assert tool_span.parent_id == first_model.event_id
    assert second_model.parent_id == tool_span.event_id

    # ③ 终态正确、无悬空 running、无重复终态
    assert agent_run.status == "success"
    assert first_model.status == "success"
    assert tool_span.status == "success"
    assert second_model.status == "success"
    _assert_chain_sane(events)


# =============================================================================
# 2. 两个会话并发：各自完整、互不串线
# =============================================================================


def test_并发两会话_各自事件完整不串线() -> None:
    """两个 thread 并发运行，事件不串线、无悬空 running、无重复终态。"""
    runtime = _build_runtime()
    client = _api_client(runtime)
    with client:
        for thread_id in ("t-a", "t-b"):
            assert client.post(
                "/api/threads", json={"thread_id": thread_id}
            ).status_code == 201

        def _run(thread_id: str):
            return client.post(
                f"/api/threads/{thread_id}/runs", json={"message": "计算 1+1"}
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(_run, ["t-a", "t-b"]))

    assert [r.status_code for r in responses] == [200, 200]
    run_ids = {r.json()["run_id"] for r in responses}
    assert len(run_ids) == 2, "两次运行应产生不同的 run_id"

    events = runtime.trace_sink.get_events()

    # 每个 run 的事件集合都是完整的（各有自己的 AGENT_RUN / MODEL_CALL / TOOL_CALL）
    for run_id in run_ids:
        run_events = [e for e in events if e.run_id == run_id]
        assert run_events, f"run {run_id} 没有任何事件"
        assert len(_spans(run_events, "AGENT_RUN")) == 1
        assert len(_spans(run_events, "MODEL_CALL")) == 2
        assert len(_spans(run_events, "TOOL_CALL")) == 1
        _assert_chain_sane(run_events)

    # 不串线：每个 run 的 span 树各自闭合（父节点都在本 run 内）
    for run_id in run_ids:
        run_events = [e for e in events if e.run_id == run_id]
        event_ids = {e.event_id for e in run_events}
        for event in _spans(run_events):
            if event.parent_id is not None:
                assert event.parent_id in event_ids, (
                    f"run {run_id} 的 span {event.event_id} 的父节点跑到别的 run 里了"
                )

    _assert_chain_sane(events)


# =============================================================================
# 2b. 交错的两次调用（确定性复现「实例共享状态」缺陷）
# =============================================================================


def test_交错两次调用_各自收口不串线() -> None:
    """同一个 TraceMiddleware 实例上交错两次调用，各自独立收口。

    这是「实例共享单槽位」缺陷的**确定性**复现：真实并发下两次运行未必
    真的交错，这里把交错固定下来 —— 若 span 状态存在实例上，
    第二次 before 会顶掉第一次的，后者的终态就会写进前一个事件里。
    """
    sink = InMemoryTraceSink()
    middleware = TraceMiddleware(sink)

    ctx_a = ExecutionContext(
        run_id="run-a",
        thread_id="t-a",
        operation=OperationType.MODEL_CALL,
        operation_name="model_decide",
    )
    ctx_b = ExecutionContext(
        run_id="run-b",
        thread_id="t-b",
        operation=OperationType.MODEL_CALL,
        operation_name="model_decide",
    )

    middleware.before(ctx_a)
    middleware.before(ctx_b)      # 交错：b 顶掉 a
    middleware.success(ctx_a, "a")  # a 收口
    middleware.success(ctx_b, "b")  # b 收口

    events = sink.get_events()
    a_events = [e for e in events if e.run_id == "run-a"]
    b_events = [e for e in events if e.run_id == "run-b"]
    assert len(a_events) == 2, f"run-a 的事件数 = {len(a_events)}"
    assert len(b_events) == 2, f"run-b 的事件数 = {len(b_events)}"
    _assert_chain_sane(a_events)
    _assert_chain_sane(b_events)
    assert _terminals(a_events)[0].status == "success"
    assert _terminals(b_events)[0].status == "success"


# =============================================================================
# 3. 失败：终态为 error，且错误保留**安全**原因
# =============================================================================


def test_失败_终态为error且保留安全原因() -> None:
    """模型失败 → span 终态是 error；原因受控，原始异常文本不泄漏。

    TRACE-01 收口：`ModelError` 家族**按具体子类给固定文案**，不再放行
    `str(exc)` —— 异常类型不构成安全保证（上游响应正文可能带敏感串）。
    异常**类名**由 `error_type` 承载，原始消息只留在服务端日志里。
    """
    # 3a. ModelError：固定文案；原始消息不落盘
    runtime = _build_runtime(_RaisingModel(ModelServerError("供应商返回 500")))
    store = runtime.store
    store.create("t-fail")
    with pytest.raises(ModelServerError):
        runtime.integrated_agent.run("t-fail", "hi")

    events = runtime.trace_sink.get_events()
    agent_runs = _spans(events, "AGENT_RUN")
    assert len(agent_runs) == 1
    failed = agent_runs[0]
    assert failed.status == "error"
    assert failed.error_type == "ModelServerError"
    assert failed.error_detail == "模型服务端错误"
    assert "供应商返回 500" not in json.dumps(
        [e.to_dict() for e in events], ensure_ascii=False
    ), "原始异常消息不应进入 Trace 事件"
    _assert_chain_sane(events)

    # 3b. 未知异常：原始 message 绝不放行
    secret = "SECRET_TOKEN_abc123 /etc/passwd"
    runtime2 = _build_runtime(_RaisingModel(RuntimeError(secret)))
    runtime2.store.create("t-fail2")
    with pytest.raises(RuntimeError):
        runtime2.integrated_agent.run("t-fail2", "hi")

    events2 = runtime2.trace_sink.get_events()
    failed2 = _spans(events2, "AGENT_RUN")[0]
    assert failed2.status == "error"
    assert failed2.error_type == "RuntimeError"
    assert not failed2.error_detail, "未知异常不应给出失败原因"
    dumped = json.dumps([e.to_dict() for e in events2], ensure_ascii=False)
    assert secret not in dumped, "原始异常文本泄漏到 Trace 事件里"


# =============================================================================
# 4. 取消：终态为 cancelled
# =============================================================================


def test_取消_终态为cancelled() -> None:
    """取消令牌已置位 → span 终态是 cancelled（不是 error）。"""
    runtime = _build_runtime()
    runtime.store.create("t-cancel")
    token = CancellationToken()
    token.cancel()

    with pytest.raises(BudgetExceededError):
        runtime.integrated_agent.run("t-cancel", "hi", cancellation_token=token)

    events = runtime.trace_sink.get_events()
    cancelled = _spans(events, "AGENT_RUN")[0]
    assert cancelled.status == "cancelled"
    _assert_chain_sane(events)


# =============================================================================
# 5. 超时：终态为 timeout
# =============================================================================


def test_超时_终态为timeout() -> None:
    """模型调用超时 → span 终态是 timeout（不是 error / cancelled）。"""
    runtime = _build_runtime(
        _SlowStreamModel(delay=0.5),
        config=RuntimeConfig(trace_enabled=True, model_timeout_seconds=0.1),
    )
    runtime.store.create("t-timeout")
    token = CancellationToken()

    with pytest.raises(BudgetExceededError):
        runtime.integrated_agent.run("t-timeout", "hi", cancellation_token=token)

    events = runtime.trace_sink.get_events()
    timed_out = _spans(events, "AGENT_RUN")[0]
    assert timed_out.status == "timeout"
    assert timed_out.error_type == "BudgetExceededError"
    _assert_chain_sane(events)


# =============================================================================
# 6. 重试：每次 attempt 是独立 span，各只有一个终态
# =============================================================================


def test_重试_每次attempt独立span且只有一个终态() -> None:
    """重试产生新 span（挂在上一 attempt 之下），每个 event_id 恰一条终态。"""
    sink = InMemoryTraceSink()
    pipeline = MiddlewarePipeline([TraceMiddleware(sink), _RetryOnceMiddleware()])
    calls = {"n": 0}

    def action() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("第一次失败")
        return "ok"

    ctx = ExecutionContext(
        run_id="run-retry",
        thread_id="t-retry",
        operation=OperationType.MODEL_CALL,
        operation_name="model_decide",
    )
    assert pipeline.execute_with_retry(ctx, action) == "ok"

    events = sink.get_events()
    terminals = _terminals(events)
    assert len(terminals) == 2, f"终态记录数 = {len(terminals)}"
    first, second = terminals
    assert first.status == "error"
    assert second.status == "success"
    assert first.event_id != second.event_id, "重试必须换新的 span"
    assert second.parent_id == first.event_id, "重试链：attempt2 挂在 attempt1 之下"
    assert ctx.attempt == 2
    _assert_chain_sane(events)


# =============================================================================
# 7. 子代理：用父运行的 run_id，挂在父侧工具 span 之下
# =============================================================================


def test_子代理_用父run的run_id并挂在工具span下() -> None:
    """子代理的 SUBAGENT_TASK span 属于父 run，且挂在父侧工具 span 之下。"""
    sink = InMemoryTraceSink()
    manager = SubagentManager(max_concurrency=1, trace_sink=sink)
    registry = ToolRegistry()
    registry.register_many(
        create_subagent_tools(
            manager=manager, parent_run_id="run-parent", parent_thread_id="t-parent"
        )
    )

    # 父侧工具 span（真实走一遍 TraceMiddleware，拿到真实的 event_id）
    tool_ctx = ExecutionContext(
        run_id="run-parent",
        thread_id="t-parent",
        operation=OperationType.TOOL_CALL,
        operation_name="subagent_submit",
    )
    middleware = TraceMiddleware(sink)
    middleware.before(tool_ctx)
    payload = json.loads(
        registry.execute(
            "subagent_submit", {"instruction": "计算 1+1"}, context=tool_ctx
        )
    )
    middleware.success(tool_ctx, payload)
    assert payload.get("ok") is True, payload

    # 等子任务跑完
    manager.collect(payload["task_id"], timeout=15)
    manager.shutdown()

    events = sink.get_events()
    task_spans = _spans(events, "SUBAGENT_TASK")
    assert len(task_spans) == 1, f"SUBAGENT_TASK span 数 = {len(task_spans)}"
    task_span = task_spans[0]

    # run_id 用**父运行**的（不是子代理自己的 uuid）
    assert task_span.run_id == "run-parent"
    # 父节点 = 父侧工具 span
    assert task_span.parent_id == tool_ctx.span_id

    # 子代理的模型调用挂在 SUBAGENT_TASK 之下，同属父 run
    sub_models = [
        e for e in _spans(events, "MODEL_CALL")
        if e.parent_id == task_span.event_id
    ]
    assert sub_models, "子代理的模型调用应挂在 SUBAGENT_TASK span 之下"
    for event in sub_models:
        assert event.run_id == "run-parent"

    _assert_chain_sane(events)


# =============================================================================
# 8. 扫描器自身
# =============================================================================


def test_扫描器_能识别悬空running与重复终态() -> None:
    """扫描器必须真的能抓出问题（否则前面的用例等于没断言）。"""
    sink = InMemoryTraceSink()
    mw = TraceMiddleware(sink)
    ctx = ExecutionContext(
        run_id="run-x",
        thread_id="t",
        operation=OperationType.MODEL_CALL,
        operation_name="decide",
    )
    mw.before(ctx)
    # 只发 running、没有终态 → 悬空
    with pytest.raises(AssertionError, match="终态记录数"):
        _assert_chain_sane(sink.get_events())

    # 补一个终态 → 不再悬空
    mw.success(ctx, "ok")
    _assert_chain_sane(sink.get_events())


# =============================================================================
# 9. 停止 vs 超时：两个真实入口的对照
# =============================================================================
#
# 返修点：用户主动停止时，Agent 层抛的也是
# `BudgetExceededError("model_call_timeout")`（模型超时定时器置位的同一个
# 取消令牌），过去 Trace 因此把「用户停止」记成 timeout —— 与 API 的
# cancelled 口径相反，会直接误导排障。
#
# 现在靠「首次取消的原因」区分：
#   - 用户停止 → `token.cancel()` → reason="cancelled" → Trace 记 cancelled
#   - 真正超时 → `token.cancel(reason="model_timeout")` → Trace 记 timeout


def test_模型调用期间用户停止_终态为cancelled() -> None:
    """真实入口：模型调用进行中点「停止」→ Trace 终态是 cancelled。

    走真实 API：`POST /api/threads/{id}/runs/stream` 起一次运行，
    模型还在调用中时 `POST /api/threads/{id}/runs/stop` 停它。
    """
    runtime = _build_runtime(_SlowStreamModel(delay=2.0))
    client = _api_client(runtime)
    with client:
        assert client.post(
            "/api/threads", json={"thread_id": "t-stop"}
        ).status_code == 201

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                client.post,
                "/api/threads/t-stop/runs/stream",
                json={"message": "做一个耗时的任务"},
            )
            # 等模型调用真的开始（Trace 里出现 running 的 MODEL_CALL）
            started = _wait_until(
                lambda: any(
                    e.operation == "MODEL_CALL" and e.status == "running"
                    for e in runtime.trace_sink.get_events()
                )
            )
            assert started, "模型调用没有开始（Trace 里没有 running 的 MODEL_CALL）"

            stopped = client.post("/api/threads/t-stop/runs/stop")
            assert stopped.status_code == 200, stopped.text
            response = future.result(timeout=30)

    assert response.status_code == 200, response.text
    # API 侧口径：用户停止 → run.cancelled
    assert "run.cancelled" in response.text, response.text

    events = runtime.trace_sink.get_events()
    agent_runs = _spans(events, "AGENT_RUN")
    assert len(agent_runs) == 1, f"AGENT_RUN span 数 = {len(agent_runs)}"
    assert agent_runs[0].status == "cancelled", (
        f"用户停止应记 cancelled，实际 {agent_runs[0].status}"
    )
    _assert_chain_sane(events)


def test_模型调用自然超时_终态为timeout() -> None:
    """真实入口：模型调用自然超时 → Trace 终态是 timeout。

    与上面的「用户停止」对照：同一个慢模型、同一个取消令牌，
    只是触发原因不同 —— 定时器到期（reason=model_timeout）记 timeout，
    用户停止（reason=cancelled）记 cancelled。
    """
    runtime = _build_runtime(
        _SlowStreamModel(delay=1.0),
        config=RuntimeConfig(trace_enabled=True, model_timeout_seconds=0.1),
    )
    runtime.store.create("t-real-timeout")
    token = CancellationToken()

    with pytest.raises(BudgetExceededError):
        runtime.integrated_agent.run("t-real-timeout", "hi", cancellation_token=token)

    # 超时定时器置位了令牌，但原因被标记为 model_timeout
    assert token.is_cancelled()
    assert token.cancel_reason() == "model_timeout"

    events = runtime.trace_sink.get_events()
    timed_out = _spans(events, "AGENT_RUN")[0]
    assert timed_out.status == "timeout", (
        f"真正超时应记 timeout，实际 {timed_out.status}"
    )
    _assert_chain_sane(events)


def test_取消原因_首次胜出不被后来的超时覆盖() -> None:
    """用户先停止、随后模型超时定时器才到期 → 原因保持「用户停止」。"""
    token = CancellationToken()
    token.cancel()
    token.cancel(reason="model_timeout")
    assert token.cancel_reason() == "cancelled"
    assert token.is_cancelled()


# =============================================================================
# 10. 「日志和 Trace 在哪看」：配置入口、落盘位置、查询方式、失败表现
# =============================================================================


def test_Web后端_Trace默认关闭_开启后落盘且可按run_id查询(tmp_path, monkeypatch) -> None:
    """Web 后端的 Trace 配置入口 → JSONL 落盘 → 按 run_id 查询。"""
    trace_path = tmp_path / "traces" / "trace.jsonl"

    # ① 默认关闭：不设置环境变量时不启用 Trace
    monkeypatch.delenv("MINI_DEERFLOW_TRACE_ENABLED", raising=False)
    monkeypatch.delenv("MINI_DEERFLOW_TRACE_PATH", raising=False)
    default_settings = ApiSettings()
    assert default_settings.trace_enabled is False
    assert default_settings.trace_path is None

    # ② 配置入口：环境变量 → ApiSettings → RuntimeConfig
    monkeypatch.setenv("MINI_DEERFLOW_TRACE_ENABLED", "1")
    monkeypatch.setenv("MINI_DEERFLOW_TRACE_PATH", str(trace_path))
    monkeypatch.setenv("MINI_DEERFLOW_MODEL_PROVIDER", "fake")
    settings = ApiSettings()
    assert settings.trace_enabled is True
    assert settings.trace_path == str(trace_path)

    # ③ 实际启动：真实 Web 应用跑一次，JSONL 文件被写出来
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.post(
            "/api/threads", json={"thread_id": "t-trace-file"}
        ).status_code == 201
        response = client.post(
            "/api/threads/t-trace-file/runs", json={"message": "你好"}
        )
    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]

    assert trace_path.exists(), f"Trace 文件没有生成：{trace_path}"
    lines = [
        line for line in trace_path.read_text(encoding="utf-8").splitlines() if line
    ]
    records = [json.loads(line) for line in lines]
    assert records, "Trace 文件是空的"
    assert {r["run_id"] for r in records} == {run_id}, (
        f"落盘记录的 run_id 应全等于本次运行：{sorted({r['run_id'] for r in records})}"
    )
    # 落盘的是「两段式」span：running + 终态
    statuses = [r["status"] for r in records]
    assert "running" in statuses and "success" in statuses

    # ④ 按 run_id 查询：一条可运行的命令
    script = Path(__file__).resolve().parents[1] / "scripts" / "trace_query.py"
    result = subprocess.run(
        [sys.executable, str(script), str(trace_path), "--run-id", run_id],
        capture_output=True,
        text=True,
        encoding="utf-8",
        # Windows 默认按本地编码写 stdout → 强制 UTF-8，避免解码失败
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert result.returncode == 0, result.stderr
    assert run_id in result.stdout
    assert "AGENT_RUN" in result.stdout, result.stdout
    # 健康数据不应产生任何警告（重复终态 / 成环的诊断不能误报）
    assert not result.stderr.strip(), f"健康 Trace 不应有警告：{result.stderr}"

    # ⑤ 安全输出：落盘记录不含凭据 / 提示词正文 / 工具参数
    dumped = json.dumps(records, ensure_ascii=False)
    for secret in ("sk-", "Bearer ", "Authorization", "password="):
        assert secret not in dumped, f"落盘记录疑似包含凭据片段：{secret}"
    assert "你好" not in dumped, "落盘记录不应包含用户提示词正文"


def test_Trace落盘路径不可写_明确报错而不是静默丢弃(tmp_path) -> None:
    """路径不可写 → 启动阶段就明确报错（不静默丢事件）。"""
    target = tmp_path / "trace-is-a-directory"
    target.mkdir()

    with pytest.raises(RuntimeError, match="Trace 文件不可写"):
        JSONLTraceSink(str(target))

    # Web 后端装配路径同样如此：create_runtime 直接失败，不带病启动
    with pytest.raises(RuntimeError, match="Trace 文件不可写"):
        create_runtime(
            store=InMemoryThreadStore(),
            registry=create_default_registry(),
            config=RuntimeConfig(trace_enabled=True, trace_path=str(target)),
            model=FakeModel(),
        )


# =============================================================================
# 11. 统一底层事实：API 与 Trace 读同一个「取消原因」
# =============================================================================
#
# 复审意见：API 与 Trace 各自判断「是不是用户停止」，同一次运行在两个观测
# 渠道出现不同终态。收口方式不是改 SSE 契约，而是把「取消原因」变成
# **唯一的底层事实**，两边各自映射到自己的公开状态：
#
#   内部事实 model_timeout → Trace 记 timeout；SSE 本轮保持兼容记 run.cancelled
#   内部事实 cancelled     → 两边都记 cancelled
#
# SSE 的兼容映射只影响**表示层**：清理 / 恢复 / 持久化由取消令牌驱动
# （agent_runs.py 的 finally → unregister_run(cancelled=令牌是否置位)），
# 与终态事件名无关。


class _SpyToken:
    """记录各类读取次数的取消令牌替身。"""

    def __init__(self, cancelled: bool, reason: str | None) -> None:
        self._cancelled = cancelled
        self._reason = reason
        self.reads = {"is_cancelled": 0, "cancel_reason": 0, "cancel_state": 0}

    def is_cancelled(self) -> bool:
        self.reads["is_cancelled"] += 1
        return self._cancelled

    def cancel_reason(self) -> str | None:
        self.reads["cancel_reason"] += 1
        return self._reason

    def cancel_state(self) -> tuple[bool, str | None]:
        self.reads["cancel_state"] += 1
        return self._cancelled, self._reason


def _cancelling_runner(reason: str | None, resource: str):
    """runner 替身：置位取消令牌（可带原因）后抛 BudgetExceededError。"""

    def runner(thread_id, user_input, run_id=None, cancellation_token=None):
        if cancellation_token is not None:
            if reason is None:
                cancellation_token.cancel()
            else:
                cancellation_token.cancel(reason=reason)
        raise BudgetExceededError(resource, None)

    return runner


def test_终态分类_读一次一致快照而不是分两次读() -> None:
    """终态判定必须一次拿到「是否取消 + 原因」。

    分两次读（is_cancelled() 与 cancel_reason() 各加一次锁）之间可能插入
    一次 cancel()，读到的是不自洽的组合（没取消 + 有原因），
    「用户停止」会被误判成 error。
    """
    token = _SpyToken(True, "cancelled")
    ctx = ExecutionContext(cancellation_token=token)  # type: ignore[arg-type]

    status = classify_terminal_status(ctx, BudgetExceededError("model_call_timeout", None))
    assert status == "cancelled", f"用户停止应记 cancelled，实际 {status}"

    assert token.reads["cancel_state"] == 1, "应读一次一致快照"
    assert token.reads["is_cancelled"] == 0, "不应单独读 is_cancelled()"
    assert token.reads["cancel_reason"] == 0, "不应单独读 cancel_reason()"


def test_取消状态快照_未取消与已取消都自洽() -> None:
    token = CancellationToken()
    assert token.cancel_state() == (False, None)
    token.cancel(reason="model_timeout")
    assert token.cancel_state() == (True, "model_timeout")


def test_取消状态快照_并发取消下仍然自洽() -> None:
    """并发取消时，快照里的「是否取消」与「原因」永远同时成立。"""
    token = CancellationToken()
    stop = threading.Event()

    def _worker() -> None:
        while not stop.is_set():
            token.cancel(reason="model_timeout")

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()
    try:
        for _ in range(2000):
            cancelled, reason = token.cancel_state()
            assert cancelled == (reason is not None), (
                f"快照不自洽：cancelled={cancelled} reason={reason}"
            )
    finally:
        stop.set()
        worker.join(timeout=2)


def test_API层_模型超时_内部事实为model_timeout() -> None:
    """模型超时 → API 层识别出的底层事实是 model_timeout（不是"用户停止"）。"""
    svc = AgentRunService(
        runner=_cancelling_runner("model_timeout", "model_call_timeout"),
        store=InMemoryThreadStore(),
        coordinator=ThreadRunCoordinator(),
    )
    svc._store.create("t")
    token = CancellationToken()

    with pytest.raises(AgentRunCancelledError) as exc_info:
        svc.run("t", "m", cancellation_token=token)

    assert exc_info.value.reason == "model_timeout", (
        f"内部事实应为 model_timeout，实际 {exc_info.value.reason}"
    )


def test_API层_用户停止_内部事实为cancelled() -> None:
    """用户停止 → API 层识别出的底层事实是 cancelled。"""
    svc = AgentRunService(
        runner=_cancelling_runner("cancelled", "model_call_timeout"),
        store=InMemoryThreadStore(),
        coordinator=ThreadRunCoordinator(),
    )
    svc._store.create("t")
    token = CancellationToken()

    with pytest.raises(AgentRunCancelledError) as exc_info:
        svc.run("t", "m", cancellation_token=token)

    assert exc_info.value.reason == "cancelled", (
        f"内部事实应为 cancelled，实际 {exc_info.value.reason}"
    )


def test_API层_没有取消令牌时的模型超时_仍报超时() -> None:
    """运行未带取消令牌时，模型超时走原有的超时分支（既有行为不变）。"""
    svc = AgentRunService(
        runner=_cancelling_runner(None, "model_call_timeout"),
        store=InMemoryThreadStore(),
        coordinator=ThreadRunCoordinator(),
    )
    svc._store.create("t")

    with pytest.raises(AgentRunTimeoutError):
        svc.run("t", "m")


def test_模型超时_同一底层事实_Trace记timeout而SSE保持兼容run_cancelled() -> None:
    """端到端：同一个「模型超时」事实，两个观测渠道各自映射。

    - Trace → `timeout`
    - SSE   → `run.cancelled`（本轮不改公开事件名；契约保持兼容）

    两者读的是同一个底层事实，不再各判各的。
    """
    runtime = _build_runtime(
        _SlowStreamModel(delay=1.0),
        config=RuntimeConfig(trace_enabled=True, model_timeout_seconds=0.1),
    )
    client = _api_client(runtime)
    with client:
        assert client.post(
            "/api/threads", json={"thread_id": "t-timeout-sse"}
        ).status_code == 201
        response = client.post(
            "/api/threads/t-timeout-sse/runs/stream", json={"message": "慢一点"}
        )

    assert response.status_code == 200, response.text
    # 公开事件名保持兼容（本轮不改 SSE 契约）
    assert "run.cancelled" in response.text, response.text
    assert "run.timed_out" not in response.text, response.text

    # 同一底层事实 → Trace 侧记 timeout
    events = runtime.trace_sink.get_events()
    agent_runs = _spans(events, "AGENT_RUN")
    assert len(agent_runs) == 1, f"AGENT_RUN span 数 = {len(agent_runs)}"
    assert agent_runs[0].status == "timeout", (
        f"模型超时应记 timeout，实际 {agent_runs[0].status}"
    )
    _assert_chain_sane(events)


# =============================================================================
# 12. 失败原因的安全边界：不再依赖「异常消息本身安全」这一约定
# =============================================================================
#
# 复审意见：`safe_trace_detail()` 里 `str(ModelError)` 的安全假设站不住 ——
# 异常类型不构成安全保证，上游响应正文可能带敏感串；截断只能限长。
# 同样，`BudgetExceededError.resource` 只是**约定**为固定枚举
# （`lead_agent/budget_ledger.py` 实际会传 `lease:<id>:<resource>` 这类动态串）。
#
# 收口：
#   - ModelError 家族 → 按**具体子类**给固定文案，不放行 str(exc)；
#   - 预算 resource  → **允许列表**，不在表内整条不放行；
#   - 未识别异常     → 空串（不放行原始 message）。


def test_失败原因_未知ModelError子类不放行原始消息() -> None:
    """未识别的 ModelError 子类 → 通用固定文案，原始消息不落盘。"""
    exc = ModelError("供应商返回 sk-live-ABCDEFGH1234567890，body=echo")
    detail = safe_trace_detail(exc)
    assert detail == "模型调用失败"
    assert "sk-live" not in detail
    assert "echo" not in detail


def test_失败原因_ModelError按子类给固定文案() -> None:
    """已知子类 → 各自的固定文案（不含原始消息里的任何内容）。"""
    assert safe_trace_detail(ModelRateLimitError("429 too many requests")) == "模型调用被限流"
    assert safe_trace_detail(ModelAuthError("401 unauthorized, key=sk-zzz")) == "模型鉴权失败"
    assert safe_trace_detail(ModelServerError("500 boom")) == "模型服务端错误"
    assert safe_trace_detail(ModelTimeoutError("模型超时")) == "模型调用超时"
    assert safe_trace_detail(ModelConfigError("缺少 API Key")) == "模型配置错误"
    assert safe_trace_detail(ModelEmptyResponseError("空响应")) == "模型返回内容为空"
    assert safe_trace_detail(ModelProtocolError("tool_use 缺 id")) == "模型响应不符合约定"


def test_失败原因_预算resource不在允许列表则不放行() -> None:
    """动态 resource（lead_agent 的 `lease:<id>:<resource>`）→ 整条不放行。"""
    detail = safe_trace_detail(BudgetExceededError("lease:abc123:model_calls", 5))
    assert detail == "", f"不在允许列表的 resource 不应放行，实际 {detail!r}"
    assert "lease" not in detail


def test_失败原因_允许列表内的预算resource仍然可见() -> None:
    """白名单内的预算资源照常可见（排障需要知道是哪一项超了）。"""
    assert safe_trace_detail(BudgetExceededError("model_calls", 5)) == "预算耗尽：model_calls（上限: 5）"
    assert safe_trace_detail(BudgetExceededError("steps", 12)) == "预算耗尽：steps（上限: 12）"
    assert safe_trace_detail(BudgetExceededError("model_call_timeout", None)) == "预算耗尽：model_call_timeout"


def test_失败原因_未知异常一律空串() -> None:
    assert safe_trace_detail(RuntimeError("SECRET_TOKEN /etc/passwd")) == ""
    assert safe_trace_detail(None) == ""


# =============================================================================
# 13. Trace 写入失败：两级失败策略
# =============================================================================
#
# 复审意见：构造时检测路径可写是对的，但**运行期**写入失败（磁盘满、
# 文件系统故障）仍可能中断正常 Agent 执行 —— 需要明确这是 fail-fast
# 还是「观测故障不伤主流程」。
#
# 收口后的策略：
#   启动期（构造时路径不可写）→ 直接报错（还没产出工作，明确失败最省事）；
#   运行期（写入出错）→ **不中断 Agent 运行**，但记 ERROR 并标记 degraded，
#   后续事件跳过（不反复失败、不刷日志）。
#
# 依据：Trace 是显式开启的观测能力（默认关），不是业务事实源；
# 业务事实走 ThreadStore / ConfirmationStore。


class _FailingFile:
    """写入即失败的假文件（模拟磁盘满）。"""

    def __init__(self) -> None:
        self.write_calls = 0

    def write(self, *_args) -> None:
        self.write_calls += 1
        raise OSError(28, "No space left on device")

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


def _sinks_of(runtime) -> list:
    return list(runtime.trace_sink._sinks)


def test_Trace写入失败_不中断Agent运行_但记ERROR且不再重试(tmp_path, caplog) -> None:
    """运行期写入失败：Agent 运行照常完成，但失败必须留下 ERROR 记录。"""
    runtime = _build_runtime(
        config=RuntimeConfig(
            trace_enabled=True,
            trace_path=str(tmp_path / "trace.jsonl"),
        ),
    )
    jsonl_sink = next(s for s in _sinks_of(runtime) if isinstance(s, JSONLTraceSink))
    memory_sink = next(s for s in _sinks_of(runtime) if isinstance(s, InMemoryTraceSink))
    failing = _FailingFile()
    jsonl_sink._file = failing  # 模拟磁盘满

    runtime.store.create("t-write-fail")
    with caplog.at_level(logging.ERROR, logger="mini_deerflow.middleware"):
        answer, _trace = runtime.integrated_agent.run("t-write-fail", "你好")

    # ① 主流程没有被观测故障打断
    assert answer is not None, "Trace 写入失败不应中断 Agent 运行"

    # ② 但失败不是静默的
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "Trace 写入失败必须留下 ERROR 记录"
    assert any("Trace 写入失败" in r.getMessage() for r in errors), [
        r.getMessage() for r in errors
    ]

    # ③ 后续事件被跳过，不反复失败、不刷日志
    assert failing.write_calls == 1, (
        f"写入失败后不应反复重试，实际尝试 {failing.write_calls} 次"
    )
    assert len([r for r in caplog.records if "Trace 写入失败" in r.getMessage()]) == 1

    # ④ 内存 sink 仍然收得到事件（观测没有整体失效）
    assert memory_sink.get_events(), "内存 sink 仍应收到事件"


def test_Trace写入失败_关闭时不抛异常(tmp_path) -> None:
    """关闭失败也不抛 —— 不打断运行时的关闭流程。"""
    sink = JSONLTraceSink(str(tmp_path / "trace.jsonl"))
    sink._file = _FailingFile()
    sink.emit(
        TraceEvent(
            event_id="e1",
            parent_id=None,
            run_id="r1",
            thread_id="t1",
            task_id=None,
            operation="AGENT_RUN",
            name="agent_run",
            start=0.0,
            status="running",
        )
    )
    sink.flush()
    sink.close()  # 不抛


def test_Trace路径不可写_构造阶段仍然明确报错(tmp_path) -> None:
    """启动期策略不变：路径不可写直接报错，不静默降级。"""
    target = tmp_path / "trace-is-a-dir"
    target.mkdir()
    with pytest.raises(RuntimeError, match="Trace 文件不可写"):
        JSONLTraceSink(str(target))


# =============================================================================
# 14. 查询工具的异常数据诊断：重复终态 / 父子引用成环
# =============================================================================
#
# 复审意见：查询工具若静默处理异常数据，会把「同一 span 同时被记成 success
# 和 error」这类真实缺陷藏起来 —— 那正是 TRACE-01 要防的东西。
#
# 收口方式：**只诊断不修复** —— 保留原始记录，在 stderr 给出警告，
# 原始文件内容一字不改。


def _span(
    event_id: str,
    *,
    parent_id: str | None = None,
    status: str = "success",
    run_id: str = "run-diag",
) -> dict:
    return {
        "event_id": event_id,
        "parent_id": parent_id,
        "run_id": run_id,
        "thread_id": "t-diag",
        "operation": "MODEL_CALL",
        "name": "model_decide",
        "start": 0.0,
        "status": status,
        "kind": "span",
    }


def _write_jsonl(path: Path, records: list[dict]) -> str:
    text = "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"
    path.write_text(text, encoding="utf-8")
    return text


def _run_query(*args: str) -> subprocess.CompletedProcess:
    script = Path(__file__).resolve().parents[1] / "scripts" / "trace_query.py"
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


def test_查询工具_重复终态给出警告_但不修改原始文件(tmp_path) -> None:
    """同一 span 有两条终态 → stderr 警告；文件内容不改；span 仍然可见。"""
    path = tmp_path / "trace.jsonl"
    original = _write_jsonl(path, [
        _span("e1", status="running"),
        _span("e1", status="success"),
        _span("e1", status="error"),
    ])

    result = _run_query(str(path), "--run-id", "run-diag")

    assert result.returncode == 0, result.stderr
    # ① 重复终态被警告出来（而不是静默取第一条）
    assert "终态" in result.stderr, result.stderr
    assert "e1" in result.stderr, result.stderr
    # ② 原始文件内容一字未改
    assert path.read_text(encoding="utf-8") == original, "查询工具不应修改原始文件"
    # ③ 该 span 仍然显示（保留第一条终态）
    assert "id=e1" in result.stdout, result.stdout


def test_查询工具_父子引用成环给出警告_且环上span不被丢弃(tmp_path) -> None:
    """parent_id 成环 → stderr 警告；环上的 span 仍被显示，不静默消失。"""
    path = tmp_path / "trace.jsonl"
    original = _write_jsonl(path, [
        _span("a", parent_id="b"),
        _span("b", parent_id="a"),
    ])

    result = _run_query(str(path), "--run-id", "run-diag")

    assert result.returncode == 0, result.stderr
    # ① 成环被警告出来
    assert "成环" in result.stderr, result.stderr
    # ② 原始文件内容一字未改
    assert path.read_text(encoding="utf-8") == original, "查询工具不应修改原始文件"
    # ③ 环上的 span 都被显示（成环时它们不在任何根链上，容易被静默丢弃）
    assert "id=a" in result.stdout, result.stdout
    assert "id=b" in result.stdout, result.stdout

