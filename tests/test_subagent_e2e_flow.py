"""Subagent 首版端到端链路验证（脚本模型 + 真实 HTTP/SSE）。

走**真实入口**：FastAPI → SSE → 主 Agent → subagent_submit → 子 Agent →
结果回传 → SSE 事件 → 记录落库 → API 重读。

只验证本轮相关的链路，不做全量回归。零网络、零付费模型。
"""

from __future__ import annotations

import json
import queue
from typing import Any

from fastapi.testclient import TestClient

from mini_deerflow.api.app import create_app
from mini_deerflow.api.config import ApiSettings
from mini_deerflow.models import FinalAnswer, Message, ToolCall
from mini_deerflow.runtime import RuntimeConfig, create_runtime
from mini_deerflow.subagent_executor import SubagentManager
from mini_deerflow.subagent_tools import create_subagent_tools
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tools import Tool, ToolRegistry


class _ChildModel:
    def __init__(self) -> None:
        self.calls = 0

    def decide(self, messages: list[Message], tools: list[Any] | None = None):
        self.calls += 1
        return FinalAnswer(content="子 Agent 的结论：三个方向值得查")


class _ParentModel:
    def __init__(self) -> None:
        self._step = 0

    def decide(self, messages: list[Message], tools: list[Any] | None = None):
        self._step += 1
        if self._step == 1:
            return ToolCall(
                name="subagent_submit",
                arguments={"instruction": "调研三个可能的解决方向"},
            )
        return FinalAnswer(content="已根据子任务的结论给出最终回答。")


def test_端到端_派发到结果回传到落库重读() -> None:
    store = InMemoryThreadStore()
    store.create("th-e2e")

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="noop",
            description="占位",
            parameters={"type": "object", "properties": {}},
            handler=lambda args: "{}",
        )
    )

    runtime = create_runtime(
        store=store,
        registry=registry,
        config=RuntimeConfig(subagent_enabled=True),
    )

    # 用带真实（脚本）模型的 submit 工具替换 runtime 默认的那个
    manager = runtime.manager
    assert manager is not None
    child_model = _ChildModel()
    registry.unregister("subagent_submit")
    for tool in create_subagent_tools(
        manager=manager,
        parent_run_id="e2e",
        parent_thread_id="th-e2e",
        thread_store=store,
        model=child_model,
        registry=registry,
        parent_tool_names=tuple(t.name for t in registry.list_tools()),
    ):
        if tool.name == "subagent_submit":
            registry.register(tool)

    # 主 Agent 用脚本模型
    runtime.agent._model = _ParentModel()  # type: ignore[attr-defined]

    app = create_app(ApiSettings(), runtime_factory=lambda: runtime)
    client = TestClient(app)

    with client:
        # 1) SSE 跑一轮
        resp = client.post(
            "/api/threads/th-e2e/runs/stream",
            json={"message": "帮我调研一下可能的解决方向"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.text

    # 2) SSE 里应有子任务事件
    assert "subagent.started" in body, f"应含 subagent.started 事件，实际:\n{body[:2000]}"
    assert "subagent.completed" in body, f"应含 subagent.completed 事件，实际:\n{body[:2000]}"

    # 3) 父 Agent 的最终回答里应整合了子任务结论
    assert "已根据子任务的结论给出最终回答" in body

    # 4) 子 Agent 真的用注入的模型跑过
    assert child_model.calls == 1

    # 5) 记录落库 → API 重读（刷新后恢复卡片的数据来源）
    with client:
        detail = client.get("/api/threads/th-e2e").json()
    runs = detail["subagent_runs"]
    assert len(runs) == 1, f"应有 1 条子任务记录，实际 {runs}"
    assert runs[0]["status"] == "SUCCEEDED"
    assert "三个方向值得查" in runs[0]["output"]
    assert runs[0]["instruction"] == "调研三个可能的解决方向"

    # 6) 记录里不带敏感字段（不含原始异常栈 / 完整参数）
    assert set(runs[0]) <= {
        "task_id", "instruction", "status", "output", "error_type",
        "error_message", "started_at", "finished_at", "duration_seconds",
        "parent_run_id", "tool_calls", "model_calls", "recorded_at",
    }
