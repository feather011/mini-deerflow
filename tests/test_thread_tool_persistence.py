"""Thread 持久化收口：工具往返不进 ThreadState。

核心不变式：

    Thread 只保存  user → 最终 assistant
    assistant(tool_use) + tool_result 只存在于
        本轮 working_messages / Trace / SSE 事件

原因：单独落一条不带 `call_id` 的 tool 消息，下一轮 working_messages 里
没有与之配对的 tool_use，会形成**孤儿 tool_result**——供应商会拒收该轮请求。

全部使用 FakeModel / 假模型与内存或本地 SQLite，**零网络、零额度**。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from mini_deerflow.agent import Agent
from mini_deerflow.anthropic_compat_model import provider_messages_from
from mini_deerflow.api.app import create_app
from mini_deerflow.api.config import ApiSettings
from mini_deerflow.models import FakeModel, FinalAnswer, Message, ToolCall
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tools import ToolNotFoundError, create_default_registry

_TERMINAL = {
    "run.completed",
    "run.failed",
    "run.timed_out",
    "run.cancelled",
}


class _ScriptedModel:
    """按脚本返回决策，并记录每轮 decide 收到的完整消息。"""

    def __init__(self, decisions: list[Any]) -> None:
        self._decisions = list(decisions)
        self.calls: list[list[Message]] = []

    def decide(
        self, messages: list[Message], tools: list[Any] | None = None
    ) -> FinalAnswer | ToolCall:
        self.calls.append(list(messages))
        return self._decisions.pop(0)


def _agent(model: Any, store: Any = None) -> tuple[Agent, Any]:
    store = store or InMemoryThreadStore()
    if not store.exists("t1"):
        store.create("t1")
    return (
        Agent(model=model, registry=create_default_registry(), store=store),
        store,
    )


def _roles(store: Any) -> list[str]:
    return [m.role for m in store.get("t1").snapshot()]


def _has_tool_result_block(messages: list[Message]) -> bool:
    """转成供应商格式后，是否出现 tool_result 块。

    注意 `provider_messages_from` 只为**配对成功**的 tool 消息生成 tool_result，
    孤儿 tool 会退回普通文本。因此"第二轮完全没有 tool_result"才是真正要守的
    不变式：上一轮的工具往返根本不该传到下一轮。
    """
    for item in provider_messages_from(messages):
        content = item.get("content")
        if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        ):
            return True
    return False


def _has_orphan_tool(messages: list[Message]) -> bool:
    """是否存在没有配对 tool_use 的 tool 消息（孤儿）。"""
    for index, message in enumerate(messages):
        if message.role != "tool":
            continue
        previous = messages[index - 1] if index > 0 else None
        if (
            previous is None
            or previous.role != "assistant"
            or previous.tool_call_id != message.tool_call_id
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# 1/2. 落库形状与失败不变
# ---------------------------------------------------------------------------


def test_工具调用成功后_thread_只有_user_与_assistant() -> None:
    model = _ScriptedModel([
        ToolCall(name="calculator", arguments={"expression": "1+1"}, id="t1"),
        FinalAnswer(content="计算结果: 2"),
    ])
    agent, store = _agent(model)
    answer, trace = agent.run("t1", "计算 1+1")

    assert answer == "计算结果: 2"
    assert _roles(store) == ["user", "assistant"]
    # 工具结果没丢，它在 Trace 里
    tool_entries = [t for t in trace if t.get("action") == "tool_call"]
    assert [t["detail"]["result"] for t in tool_entries] == ["2"]


def test_工具调用失败后_thread_完全不变() -> None:
    model = _ScriptedModel([
        ToolCall(name="no_such_tool", arguments={}, id="t1"),
    ])
    agent, store = _agent(model)

    try:
        agent.run("t1", "随便")
    except ToolNotFoundError:
        pass
    else:  # pragma: no cover - 必须抛
        raise AssertionError("未知工具应当抛出 ToolNotFoundError")

    assert _roles(store) == []


def test_工具执行异常时_thread_完全不变() -> None:
    from mini_deerflow.tools import Tool, ToolRegistry

    registry = ToolRegistry()
    registry.register(Tool(
        name="boom",
        description="总是失败",
        parameters={"type": "object"},
        handler=lambda args: (_ for _ in ()).throw(ValueError("boom")),
    ))
    store = InMemoryThreadStore()
    store.create("t1")
    model = _ScriptedModel([ToolCall(name="boom", arguments={}, id="t1")])
    agent = Agent(model=model, registry=registry, store=store)

    try:
        agent.run("t1", "触发")
    except Exception:
        pass
    else:  # pragma: no cover
        raise AssertionError("工具异常应当向外传播")

    assert _roles(store) == []


# ---------------------------------------------------------------------------
# 3. 跨 run：第二轮不得出现孤立 tool_result
# ---------------------------------------------------------------------------


def test_第二轮模型请求_不存在孤立_tool_result() -> None:
    store = InMemoryThreadStore()
    store.create("t1")

    # 第一轮：走一次工具闭环
    first = _ScriptedModel([
        ToolCall(name="calculator", arguments={"expression": "1+1"}, id="t1"),
        FinalAnswer(content="计算结果: 2"),
    ])
    Agent(model=first, registry=create_default_registry(), store=store).run(
        "t1", "计算 1+1"
    )
    assert _roles(store) == ["user", "assistant"]

    # 第二轮：普通消息，直接收敛
    second = _ScriptedModel([FinalAnswer(content="好的")])
    answer, _ = Agent(
        model=second, registry=create_default_registry(), store=store
    ).run("t1", "再来一次")

    assert answer == "好的"
    second_messages = second.calls[0]

    # 2) 第二轮模型请求里没有任何 tool 消息
    assert [m.role for m in second_messages] == ["user", "assistant", "user"]
    # 3) 转成供应商格式后，也不存在 tool_result 块
    assert not _has_tool_result_block(second_messages)
    # 顺带确认没有任何孤儿 tool 消息
    assert not _has_orphan_tool(second_messages)

    # 4) 第二轮正常得到 FinalAnswer（已在上面断言）
    assert _roles(store) == ["user", "assistant", "user", "assistant"]


# ---------------------------------------------------------------------------
# 4. 同一 run 内连续两次工具调用
# ---------------------------------------------------------------------------


def test_同一_run_连续两次工具调用均成功() -> None:
    model = _ScriptedModel([
        ToolCall(name="calculator", arguments={"expression": "1+1"}, id="t1"),
        ToolCall(name="calculator", arguments={"expression": "2+2"}, id="t2"),
        FinalAnswer(content="完成"),
    ])
    agent, store = _agent(model)
    _, trace = agent.run("t1", "连续算两次")

    assert _roles(store) == ["user", "assistant"]
    assert [t["action"] for t in trace] == [
        "model_decision",
        "tool_call",
        "model_decision",
        "tool_call",
        "final_answer",
    ]
    assert [t["detail"]["result"] for t in trace if t["action"] == "tool_call"] == [
        "2",
        "4",
    ]

    # 第三次 decide 的消息里，两组 tool_use / tool_result 必须各自配对
    third = model.calls[2]
    assert not _has_orphan_tool(third)
    assert _has_tool_result_block(third)  # 配对的 tool_result 该在
    tool_use_ids = [
        m.tool_call_id for m in third if m.role == "assistant" and m.tool_call_id
    ]
    tool_result_ids = [m.tool_call_id for m in third if m.role == "tool"]
    assert tool_use_ids == ["t1", "t2"]
    assert tool_result_ids == ["t1", "t2"]


# ---------------------------------------------------------------------------
# 5/6. call_id 在当前 run 内配对
# ---------------------------------------------------------------------------


def test_call_id_在当前_run_内配对() -> None:
    model = _ScriptedModel([
        ToolCall(name="calculator", arguments={"expression": "3+4"}, id="toolu_abc"),
        FinalAnswer(content="7"),
    ])
    agent, _ = _agent(model)
    agent.run("t1", "算一下")

    # 第二次 decide 收到的应当是 ... assistant(toolu_abc) → tool(toolu_abc)
    second = model.calls[1]
    assert second[-2].role == "assistant"
    assert second[-1].role == "tool"
    assert second[-2].tool_call_id == "toolu_abc"
    assert second[-1].tool_call_id == "toolu_abc"
    assert second[-1].content == "7"


def test_trace_保留工具往返() -> None:
    """落库虽不含 tool 消息，Trace 仍完整记录两步。"""
    model = _ScriptedModel([
        ToolCall(name="calculator", arguments={"expression": "5+5"}, id="t1"),
        FinalAnswer(content="10"),
    ])
    agent, _ = _agent(model)
    _, trace = agent.run("t1", "算")

    actions = [t["action"] for t in trace]
    assert "model_decision" in actions
    assert "tool_call" in actions
    assert "final_answer" in actions


# ---------------------------------------------------------------------------
# 7. SSE 事件仍包含 tool.started / tool.completed
# ---------------------------------------------------------------------------


def _parse_sse(body: str) -> list[dict[str, Any]]:
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
        if line.startswith("event:"):
            msg["event"] = line[6:].strip()
        elif line.startswith("id:"):
            msg["id"] = int(line[3:].strip())
        elif line.startswith("data:"):
            raw = line[5:].strip()
            try:
                msg["data"] = json.loads(raw)
            except json.JSONDecodeError:
                msg["data_raw"] = raw
    if msg:
        messages.append(msg)
    return messages


def test_sse_仍包含_tool_started_与_tool_completed() -> None:
    def factory() -> Any:
        from mini_deerflow.runtime import RuntimeConfig, create_runtime

        return create_runtime(
            store=InMemoryThreadStore(),
            registry=create_default_registry(),
            config=RuntimeConfig(),
        )

    app = create_app(runtime_factory=factory)
    with TestClient(app) as client:
        tid = client.post("/api/threads", json={"title": "sse"}).json()["thread_id"]
        with client.stream(
            "POST",
            f"/api/threads/{tid}/runs/stream",
            json={"message": "计算 (17 + 25) * 3"},
        ) as resp:
            events = _parse_sse("".join(resp.iter_text()))

        names = [e["event"] for e in events]
        assert "tool.started" in names
        assert "tool.completed" in names
        assert "tool.failed" not in names
        assert [n for n in names if n in _TERMINAL] == ["run.completed"]

        # SSE 有工具往返，但 Thread 落库仍然只有两段
        detail = client.get(f"/api/threads/{tid}").json()["messages"]
        assert [m["role"] for m in detail] == ["user", "assistant"]
        assert "126" in detail[1]["content"]


# ---------------------------------------------------------------------------
# 8. SQLite 重启恢复后可继续下一轮
# ---------------------------------------------------------------------------


def test_sqlite_重启恢复后可继续对话(tmp_path: Path) -> None:
    db = tmp_path / "data" / "app.db"
    workspace = tmp_path / "ws"

    def app() -> Any:
        return create_app(
            ApiSettings(db_path=str(db), workspace_root=str(workspace))
        )

    with TestClient(app()) as first:
        tid = first.post("/api/threads", json={"title": "p"}).json()["thread_id"]
        run = first.post(
            f"/api/threads/{tid}/runs", json={"message": "计算 1+1"}
        )
        assert run.status_code == 200
        assert run.json()["answer"] == "计算结果: 2"
        before = first.get(f"/api/threads/{tid}").json()["messages"]
        assert [m["role"] for m in before] == ["user", "assistant"]

    # 模拟重启：新的 lifespan、同一个 SQLite 文件
    with TestClient(app()) as second:
        after = second.get(f"/api/threads/{tid}").json()["messages"]
        assert [m["role"] for m in after] == ["user", "assistant"]
        assert not any(m["role"] == "tool" for m in after)

        # 可以继续下一轮（普通消息，直接收敛）
        run2 = second.post(
            f"/api/threads/{tid}/runs", json={"message": "再来一次"}
        )
        assert run2.status_code == 200
        assert run2.json()["answer"]

        final = second.get(f"/api/threads/{tid}").json()["messages"]
        assert [m["role"] for m in final] == ["user", "assistant", "user", "assistant"]


def test_fake_model_回归_落库两段() -> None:
    store = InMemoryThreadStore()
    store.create("t1")
    agent = Agent(
        model=FakeModel(), registry=create_default_registry(), store=store
    )
    answer, _ = agent.run("t1", "计算 1+1")
    assert answer == "计算结果: 2"
    assert _roles(store) == ["user", "assistant"]
