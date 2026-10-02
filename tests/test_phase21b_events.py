"""Phase 21B 测试：CLI / API / SSE 上的 ToolCall 闭环事件。

全部使用 FakeModel / 假模型 + 内存 Runtime，**不访问真实模型、不消耗额度**。

覆盖：
- SSE：model.started/completed、tool.started/completed、同一 run_id、唯一终态
- SSE：工具失败 → tool.failed + run.failed（唯一终态，无 500 文案泄漏）
- API 同步 run：calculator 真实执行 + Thread 只落 user/assistant 两段
- API 同步 run：工具失败 → 500 且 Thread 不半提交
- CLI：--trace 能看到「选工具 → 执行 → 收敛」的两轮闭环
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from mini_deerflow.api.app import create_app
from mini_deerflow.cli import main
from mini_deerflow.models import FinalAnswer, Message, ToolCall
from mini_deerflow.runtime import RuntimeConfig, create_runtime
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tools import Tool, ToolRegistry, create_default_registry

# 唯一终态集合（与 api/schemas/streaming.py 保持一致）
_TERMINAL_EVENTS = {
    "run.completed",
    "run.failed",
    "run.timed_out",
    "run.cancelled",
}


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class _ScriptedModel:
    """按预定序列返回决策的模型；记录每次收到的 tools 以验证透传。"""

    def __init__(self, decisions: list[Any]) -> None:
        self._decisions = list(decisions)
        self.tools_seen: list[list[Any]] = []

    def decide(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
    ) -> FinalAnswer | ToolCall:
        self.tools_seen.append(list(tools or []))
        return self._decisions.pop(0)


def _failing_registry() -> ToolRegistry:
    """只注册一个必然抛异常的工具。"""
    registry = ToolRegistry()
    registry.register(Tool(
        name="boom",
        description="总是失败的工具",
        parameters={"type": "object"},
        handler=lambda args: (_ for _ in ()).throw(ValueError("boom")),
    ))
    return registry


def _build_app(model: Any = None, registry: Any = None) -> Any:
    """构造注入指定 Model / ToolRegistry 的 FastAPI 应用。

    `RuntimeConfig()` 默认 `provider=fake`，整条链路零网络、零额度。
    """

    def factory() -> Any:
        return create_runtime(
            store=InMemoryThreadStore(),
            registry=registry if registry is not None else create_default_registry(),
            config=RuntimeConfig(),
            model=model,
        )

    return create_app(runtime_factory=factory)


def _parse_sse(body: str) -> list[dict[str, Any]]:
    """SSE 文本 → [{id, event, data}] 列表。"""
    messages: list[dict[str, Any]] = []
    msg: dict[str, Any] = {}
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
            raw = line[5:].strip()
            try:
                msg["data"] = json.loads(raw)
            except json.JSONDecodeError:
                msg["data_raw"] = raw
    if msg:
        messages.append(msg)
    return messages


def _read_stream(client: Any, thread_id: str, message: str) -> dict[str, Any]:
    """发一次流式 run 并读完整 SSE body。"""
    with client.stream(
        "POST",
        f"/api/threads/{thread_id}/runs/stream",
        json={"message": message},
    ) as resp:
        body = "".join(resp.iter_text())
        return {
            "status": resp.status_code,
            "headers": dict(resp.headers),
            "messages": _parse_sse(body),
        }


def _create_thread(client: Any, title: str = "t") -> str:
    resp = client.post("/api/threads", json={"title": title})
    assert resp.status_code == 201
    return resp.json()["thread_id"]


# ---------------------------------------------------------------------------
# SSE 事件
# ---------------------------------------------------------------------------


class TestSSE事件:
    def test_工具闭环事件序列_同一_run_id_唯一终态(self) -> None:
        app = _build_app()
        with TestClient(app) as client:
            tid = _create_thread(client)
            result = _read_stream(client, tid, "计算 (17 + 25) * 3")

        assert result["status"] == 200
        events = result["messages"]
        names = [m["event"] for m in events]

        # 两轮闭环：第一轮选工具，第二轮拿结果给出最终答案
        assert names[0] == "run.started"
        assert names.count("model.started") == 2
        assert names.count("model.completed") == 2
        assert names.count("tool.started") == 1
        assert names.count("tool.completed") == 1
        # tool 事件必须夹在两次 model 调用之间
        assert names.index("tool.started") < names.index("tool.completed")
        assert names.index("model.completed") < names.index("tool.started")
        # tool 完成之后必须还有第二次 model 调用（拿工具结果收敛）
        assert "model.started" in names[names.index("tool.completed") + 1:]

        # 同一 run_id 贯通全部事件，且与响应头一致
        x_run_id = result["headers"]["x-run-id"]
        assert {m["data"]["run_id"] for m in events} == {x_run_id}

        # 序列号严格自增、连续、不跳号
        seqs = [m["id"] for m in events]
        assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))

        # 唯一终态，且必须是最后一个事件
        terminals = [n for n in names if n in _TERMINAL_EVENTS]
        assert terminals == ["run.completed"]
        assert names[-1] == "run.completed"

        # 工具名来自真实 Registry
        started = [m for m in events if m["event"] == "tool.started"]
        assert started[0]["data"]["tool_name"] == "calculator"

        # 最终答案含 calculator 的真实计算结果
        completed = [m for m in events if m["event"] == "run.completed"]
        assert "126" in completed[0]["data"]["answer"]

    def test_工具失败_发_tool_failed_且唯一终态为_run_failed(self) -> None:
        model = _ScriptedModel([ToolCall(name="boom", arguments={}, id="t1")])
        app = _build_app(model=model, registry=_failing_registry())

        with TestClient(app) as client:
            tid = _create_thread(client)
            result = _read_stream(client, tid, "触发失败")

        names = [m["event"] for m in result["messages"]]
        assert "tool.started" in names
        assert "tool.failed" in names
        assert "tool.completed" not in names

        terminals = [n for n in names if n in _TERMINAL_EVENTS]
        assert terminals == ["run.failed"]
        assert names[-1] == "run.failed"

        # 模型确实看到了工具描述（Registry → ToolSpec → Model 的透传）
        assert [spec.name for spec in model.tools_seen[0]] == ["boom"]

    def test_未知工具_终态唯一且文案安全(self) -> None:
        model = _ScriptedModel([ToolCall(name="nope", arguments={}, id="t1")])
        app = _build_app(model=model, registry=ToolRegistry())

        with TestClient(app) as client:
            tid = _create_thread(client)
            result = _read_stream(client, tid, "hi")

        names = [m["event"] for m in result["messages"]]
        assert [n for n in names if n in _TERMINAL_EVENTS] == ["run.failed"]

        blob = json.dumps(
            [m.get("data", {}) for m in result["messages"]], ensure_ascii=False
        )
        assert "Traceback" not in blob
        assert "API_KEY" not in blob
        assert "sk-" not in blob


# ---------------------------------------------------------------------------
# API 同步 run
# ---------------------------------------------------------------------------


class TestAPI同步Run:
    def test_同步_run_工具闭环并持久化(self) -> None:
        app = _build_app()
        with TestClient(app) as client:
            tid = _create_thread(client)
            resp = client.post(
                f"/api/threads/{tid}/runs",
                json={"message": "计算 (17 + 25) * 3"},
            )
            assert resp.status_code == 200
            assert "126" in resp.json()["answer"]

            detail = client.get(f"/api/threads/{tid}")
            assert detail.status_code == 200
            messages = detail.json()["messages"]
            # Thread 只落 user 与最终 assistant；工具往返不进 ThreadState
            assert [m["role"] for m in messages] == ["user", "assistant"]
            assert messages[0]["content"] == "计算 (17 + 25) * 3"
            assert "126" in messages[1]["content"]

    def test_工具失败时同步_run_返回500_且_thread_不半提交(self) -> None:
        model = _ScriptedModel([ToolCall(name="boom", arguments={}, id="t1")])
        app = _build_app(model=model, registry=_failing_registry())

        with TestClient(app) as client:
            tid = _create_thread(client)
            resp = client.post(f"/api/threads/{tid}/runs", json={"message": "hi"})
            assert resp.status_code == 500

            messages = client.get(f"/api/threads/{tid}").json()["messages"]
            assert messages == []

    def test_同步_run_把_tool_spec_透传给模型(self) -> None:
        """Agent 每轮把 Registry 的纯数据描述交给 Model，而不是注册表本身。"""
        model = _ScriptedModel([FinalAnswer(content="ok")])
        app = _build_app(model=model)

        with TestClient(app) as client:
            tid = _create_thread(client)
            resp = client.post(f"/api/threads/{tid}/runs", json={"message": "hi"})
            assert resp.status_code == 200

        assert len(model.tools_seen) == 1
        names = [spec.name for spec in model.tools_seen[0]]
        assert "calculator" in names
        # 只给纯数据描述，不允许出现可执行 handler
        for spec in model.tools_seen[0]:
            assert not hasattr(spec, "handler")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCLI闭环:
    def test_cli_trace_显示两轮工具闭环(self) -> None:
        """CLI 走同一 Model Factory 与 Registry：trace 可见选工具 → 执行 → 收敛。"""
        with tempfile.TemporaryDirectory() as tmp:
            argv = ["mini-deerflow", "--db", str(Path(tmp) / "t.db"), "--trace"]
            with patch(
                "builtins.input",
                side_effect=["计算 (17 + 25) * 3", "exit"],
            ), patch("builtins.print") as mock_print, patch("sys.argv", argv):
                main()
            out = "\n".join(str(c) for c in mock_print.call_args_list)

        assert "执行轨迹" in out
        assert "模型决策" in out
        assert "工具调用: calculator" in out
        assert "最终回答" in out
        assert "计算结果: 126" in out
