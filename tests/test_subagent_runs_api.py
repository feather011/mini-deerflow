"""G6 收口：子任务记录保存后能通过 API 重读，浏览器刷新后可恢复卡片。

G6 的收口标准（本轮明确要求）：
    **保存后能通过 API 重读，浏览器刷新后恢复卡片**。
仅增加字段不算解决。

本文件覆盖后端那一半：
- 子任务记录写进父线程的 ThreadState
- ``GET /api/threads/{id}`` 能读出这些记录（前端刷新时靠它恢复卡片）
- 父 Agent 的 FinalAnswer 收尾保存**不覆盖**它们

前端恢复那一半由 ``frontend/src/features/subagents/useSubagentCards.test.ts``
覆盖（卡片能从 API 返回的记录重建）。

使用 TestClient + 真实内存 Store，零网络。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mini_deerflow.agent import Agent
from mini_deerflow.api.app import create_app
from mini_deerflow.api.config import ApiSettings
from mini_deerflow.models import FinalAnswer, Message, ToolCall
from mini_deerflow.runtime import RuntimeConfig, create_runtime
from mini_deerflow.subagent_executor import SubagentManager
from mini_deerflow.subagent_tools import create_subagent_tools
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tool_authorization import ToolPolicyRegistry
from mini_deerflow.tools import Tool, ToolRegistry


class _ChildModel:
    """子 Agent 用的脚本模型。"""

    def __init__(self, content: str = "子任务产出") -> None:
        self._content = content
        self.decide_calls = 0

    def decide(self, messages: list[Message], tools: list[Any] | None = None):
        self.decide_calls += 1
        return FinalAnswer(content=self._content)


class _ParentModel:
    """主 Agent：先派发子任务，再给最终回答。"""

    def __init__(self) -> None:
        self._step = 0

    def decide(self, messages: list[Message], tools: list[Any] | None = None):
        self._step += 1
        if self._step == 1:
            return ToolCall(
                name="subagent_submit",
                arguments={"instruction": "调研某个只读主题"},
            )
        return FinalAnswer(content="主 Agent 已整合子任务结果。")


def _build_runtime(store: InMemoryThreadStore):
    """构造一个带子 Agent 能力的 runtime（等价于 API 开启开关后的形态）。"""
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="noop",
            description="占位工具",
            parameters={"type": "object", "properties": {}},
            handler=lambda args: "{}",
        )
    )
    config = RuntimeConfig(subagent_enabled=True)
    runtime = create_runtime(
        store=store,
        registry=registry,
        config=config,
    )
    return runtime, registry


def _make_client(runtime) -> TestClient:
    app = create_app(ApiSettings(), runtime_factory=lambda: runtime)
    return TestClient(app)


def _replace_submit_tool(
    registry: ToolRegistry,
    manager: SubagentManager,
    store: InMemoryThreadStore,
    run_id: str,
    thread_id: str,
    model: Any,
) -> None:
    """把 registry 里的 subagent_submit 换成带指定模型的版本。

    其余三个子代理工具（status/collect/cancel）保持 runtime 注册的实例。
    """
    registry.unregister("subagent_submit")
    for tool in create_subagent_tools(
        manager=manager,
        parent_run_id=run_id,
        parent_thread_id=thread_id,
        thread_store=store,
        model=model,
        registry=registry,
        parent_tool_names=tuple(t.name for t in registry.list_tools()),
    ):
        if tool.name == "subagent_submit":
            registry.register(tool)


class TestSubagentRunsReadableThroughApi:
    def test_派发后API能读出子任务记录(self) -> None:
        """保存后能通过 API 重读 —— G6 的后端一半。"""
        store = InMemoryThreadStore()
        store.create("th-g6")
        runtime, registry = _build_runtime(store)

        child_model = _ChildModel("子任务产出内容")
        # runtime 已注册 subagent 工具；给子任务注入真实模型
        manager = runtime.manager
        assert manager is not None, "subagent_enabled=True 时应有 Manager"
        # 换成带**真实模型**的 submit 工具（runtime 里注入的是默认模型）
        _replace_submit_tool(registry, manager, store, "run-g6", "th-g6", child_model)

        agent = Agent(
            model=_ParentModel(),
            registry=registry,
            store=store,
            max_steps=4,
            tool_policy_registry=runtime.agent._tool_policy_registry,
        )
        # 父任务完整跑完（含 FinalAnswer 收尾保存）
        agent.run("th-g6", "帮我调研一下")

        # API 重读
        client = _make_client(runtime)
        with client:
            resp = client.get("/api/threads/th-g6")
        assert resp.status_code == 200
        body = resp.json()
        runs = body["subagent_runs"]

        assert len(runs) == 1, f"应有 1 条子任务记录，实际 {runs}"
        record = runs[0]
        assert record["status"] == "SUCCEEDED"
        assert record["instruction"] == "调研某个只读主题"
        assert "子任务产出内容" in record["output"]
        assert record["task_id"].startswith("sa-")

    def test_父Agent收尾保存不覆盖API可读的记录(self) -> None:
        """**关键回归**：主 Agent 收尾保存后，记录仍在且 API 读得到。"""
        store = InMemoryThreadStore()
        store.create("th-g6b")
        runtime, registry = _build_runtime(store)

        child_model = _ChildModel("产出")
        manager = runtime.manager
        assert manager is not None
        _replace_submit_tool(registry, manager, store, "run-g6b", "th-g6b", child_model)

        agent = Agent(
            model=_ParentModel(),
            registry=registry,
            store=store,
            max_steps=4,
            tool_policy_registry=runtime.agent._tool_policy_registry,
        )
        agent.run("th-g6b", "帮我调研一下")

        # 父任务已最终完成 —— 此时重读，记录必须还在
        client = _make_client(runtime)
        with client:
            resp = client.get("/api/threads/th-g6b")
        body = resp.json()
        assert len(body["subagent_runs"]) == 1, (
            "主 Agent 收尾保存覆盖了子任务记录（G6 未解决）"
        )
        # 消息也在（说明确实是最终状态，不是中途快照）
        assert [m["role"] for m in body["messages"]] == ["user", "assistant"]

    def test_没有派发子任务的线程返回空列表(self) -> None:
        store = InMemoryThreadStore()
        store.create("th-empty")
        runtime, _registry = _build_runtime(store)
        client = _make_client(runtime)
        with client:
            resp = client.get("/api/threads/th-empty")
        assert resp.status_code == 200
        assert resp.json()["subagent_runs"] == []
