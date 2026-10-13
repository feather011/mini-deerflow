"""GET /api/threads/{thread_id}/context/usage 路由测试。

覆盖：
- 200 + snapshot=null：Thread 存在但未发送过消息
- 200 + snapshot=null：Thread 不存在（也走 null，不返回 404）
- 200 + snapshot=完整结构：run 之后能读到快照
- 切换 Thread 不串数据
- 压缩后 snapshot 携带 tokens_before
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mini_deerflow.agent import Agent
from mini_deerflow.api.app import create_app
from mini_deerflow.context_assembler import ContextAssembler
from mini_deerflow.context_compactor import ContextCompactor
from mini_deerflow.context_state import ContextPolicy
from mini_deerflow.context_store import InMemoryContextStore
from mini_deerflow.middleware import Budget as MwBudget
from mini_deerflow.models import FinalAnswer
from mini_deerflow.runtime import IntegratedAgent, RuntimeConfig, create_runtime
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.token_counter import HeuristicTokenCounter
from mini_deerflow.tools import ToolRegistry


THREAD = "thread-1"


class _ScriptedModel:
    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls = 0

    def decide(self, messages, tools=None):
        self.calls += 1
        if not self._responses:
            return FinalAnswer(content="ok")
        resp = self._responses.pop(0)
        if isinstance(resp, BaseException):
            raise resp
        return resp


def _summary_json_response() -> FinalAnswer:
    """构造一条合法的「摘要 JSON」FinalAnswer，供 ContextCompactor 解析。"""
    import json
    payload = {
        "narrative": "早期讨论已压缩",
        "facts": ["用户提了问题"],
        "constraints": [],
        "decisions": [],
        "failed_attempts": [],
        "open_items": [],
    }
    return FinalAnswer(content=json.dumps(payload, ensure_ascii=False))


def _make_runtime_without_assembler():
    """标准 create_runtime（context_enabled=False 时 assembler 为 None）。"""
    store = InMemoryThreadStore()
    store.create(THREAD)
    runtime = create_runtime(
        store=store,
        registry=ToolRegistry(),
        config=RuntimeConfig(context_enabled=False),
    )
    return runtime


def _make_runtime_with_assembler(*, with_compactor: bool = False):
    """构造一个带 ContextAssembler 的 IntegratedAgent 与 RuntimeContext。"""
    store = InMemoryThreadStore()
    store.create(THREAD)
    policy = ContextPolicy(
        max_context_tokens=8000,
        reserved_output_tokens=1000,
        safety_margin_tokens=500,
        compaction_threshold=0.8,
    )
    counter = HeuristicTokenCounter()
    compactor = None
    if with_compactor:
        compactor = ContextCompactor(
            model=_ScriptedModel([FinalAnswer(content="压缩完成")]),
            counter=counter,
            policy=policy,
            budget=MwBudget(max_summary_calls=5),
        )
    assembler = ContextAssembler(
        policy=policy,
        counter=counter,
        context_store=InMemoryContextStore(),
        compactor=compactor,
    )

    inner = Agent(
        model=_ScriptedModel([FinalAnswer(content="ok")]),
        registry=ToolRegistry(),
        store=store,
        context_assembler=assembler,
    )
    integrated = IntegratedAgent(agent=inner, config=RuntimeConfig())

    # 复用标准 create_runtime 拿到完整的 RuntimeContext 装配（broker / middleware 等），
    # 但**只替换 integrated_agent**，store / 其他组件保持不变。
    runtime = create_runtime(
        store=store,
        registry=ToolRegistry(),
        config=RuntimeConfig(context_enabled=False),
    )
    runtime.integrated_agent = integrated  # type: ignore[attr-defined]
    return runtime, integrated


# ---------------------------------------------------------------------------
# 200 + snapshot=null
# ---------------------------------------------------------------------------


def test_returns_null_snapshot_when_no_run_yet() -> None:
    runtime = _make_runtime_without_assembler()
    with TestClient(create_app(runtime_factory=lambda: runtime)) as client:
        resp = client.get(f"/api/threads/{THREAD}/context/usage")
    assert resp.status_code == 200
    assert resp.json() == {"snapshot": None}


def test_returns_null_snapshot_for_unknown_thread() -> None:
    runtime = _make_runtime_without_assembler()
    with TestClient(create_app(runtime_factory=lambda: runtime)) as client:
        resp = client.get("/api/threads/never-existed/context/usage")
    assert resp.status_code == 200
    assert resp.json() == {"snapshot": None}


# ---------------------------------------------------------------------------
# 200 + 完整 snapshot
# ---------------------------------------------------------------------------


def test_returns_snapshot_after_run() -> None:
    runtime, integrated = _make_runtime_with_assembler()
    # 触发一次 run，让 IntegratedAgent 捕获快照
    integrated.run(THREAD, "hello")

    with TestClient(create_app(runtime_factory=lambda: runtime)) as client:
        resp = client.get(f"/api/threads/{THREAD}/context/usage")
    assert resp.status_code == 200
    body = resp.json()
    snap = body["snapshot"]
    assert snap is not None
    # 字段完整
    for key in (
        "thread_id",
        "budget",
        "total",
        "breakdown",
        "compacted",
        "tokens_before",
        "soft_limit",
        "captured_at",
    ):
        assert key in snap, f"missing field {key}"
    assert snap["thread_id"] == THREAD
    assert snap["budget"] > 0
    assert snap["total"] >= 0
    # 9 分区键齐全
    for key in (
        "system",
        "current_input",
        "active_tool_cycle",
        "recent_messages",
        "summary",
        "task_notes",
        "memory",
        "skills",
        "tool_schemas",
    ):
        assert key in snap["breakdown"]
    # 分区之和 == total
    assert sum(snap["breakdown"].values()) == snap["total"]


def test_switch_thread_does_not_leak_usage() -> None:
    """两个 Thread 都跑过 run 后，usage endpoint 各自返回自己的快照。"""
    runtime, integrated = _make_runtime_with_assembler()
    other = "thread-2"
    runtime.store.create(other)  # type: ignore[attr-defined]

    integrated.run(THREAD, "alpha")
    integrated.run(other, "beta")

    with TestClient(create_app(runtime_factory=lambda: runtime)) as client:
        a_snap = client.get(f"/api/threads/{THREAD}/context/usage").json()["snapshot"]
        b_snap = client.get(f"/api/threads/{other}/context/usage").json()["snapshot"]
    assert a_snap["thread_id"] == THREAD
    assert b_snap["thread_id"] == other
    assert a_snap["thread_id"] != b_snap["thread_id"]


def test_compaction_reflected_in_snapshot() -> None:
    """压缩后的快照应包含 tokens_before，且压缩前后数值来自真实组装。

    不允许通过放宽断言或伪造快照让测试通过——必须真实触发压缩路径。
    """
    from mini_deerflow.models import Message

    # 预填足量历史消息，让 `_compact()` 有 recent 可压缩
    store = InMemoryThreadStore()
    store.create(THREAD)
    thread = store.get(THREAD)
    history_text = "历史讨论 " * 50  # 足够长以撑爆预算
    for i in range(6):
        thread.extend([
            Message(role="user", content=f"{history_text} #{i}"),
            Message(role="assistant", content=f"历史回复 #{i}: 已收到。"),
        ])
    store.save(thread)

    # 极小 budget + 低软阈值：让新消息 + 已有历史必然触发压缩
    policy = ContextPolicy(
        max_context_tokens=200,
        reserved_output_tokens=0,
        safety_margin_tokens=0,
        compaction_threshold=0.1,  # soft = 20
    )
    counter = HeuristicTokenCounter()
    assembler = ContextAssembler(
        policy=policy,
        counter=counter,
        context_store=InMemoryContextStore(),
        compactor=ContextCompactor(
            model=_ScriptedModel([_summary_json_response()]),
            counter=counter,
            policy=policy,
            budget=MwBudget(max_summary_calls=5),
        ),
    )
    inner = Agent(
        model=_ScriptedModel([FinalAnswer(content="ok")]),
        registry=ToolRegistry(),
        store=store,
        context_assembler=assembler,
    )
    integrated = IntegratedAgent(agent=inner, config=RuntimeConfig())
    integrated.run(THREAD, "本轮新消息")

    snap = integrated.get_last_usage(THREAD)
    assert snap is not None
    # 真实触发了压缩
    assert snap.compacted is True
    # tokens_before 是压缩前真实 breakdown.total
    assert snap.tokens_before is not None
    assert snap.tokens_before >= snap.soft_limit
    # 压缩后总用量必然 <= 压缩前
    assert snap.total <= snap.tokens_before
    # 九分区之和仍然等于 total（无重复计数）
    assert sum(snap.breakdown.values()) == snap.total
    # 压缩后的 total 也应满足硬上限（否则不应返回快照）
    assert snap.total <= snap.budget