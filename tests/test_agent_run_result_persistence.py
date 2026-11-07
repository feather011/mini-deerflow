"""Agent 收尾保存不得覆盖本轮的执行证据（last_run_result）。

背景（V1 实战实测）：Agent 在 FinalAnswer 分支用**运行开始时的旧快照**整份
``save``，把运行途中由 ``run_event`` 中间件写入的 ``last_run_result`` 抹掉。
结果是：成功的 run 在页面刷新后只剩答案正文，diff / verification / guard
全没了；失败的 run 因为没走到保存分支，反而把证据留着。

本文件用**真实的 SQLite 持久化**验证：
- 成功运行后，执行证据仍然能从 store 读回
- 证据是**这一轮**的，不是上一轮遗留的旧值
- 失败运行同样保留证据，不被后续的消息保存覆盖
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mini_deerflow.agent import Agent
from mini_deerflow.models import FinalAnswer, Message, ToolCall
from mini_deerflow.thread_store import SQLiteThreadStore
from mini_deerflow.tools import Tool, ToolRegistry

EVIDENCE_TOOL = "write_evidence"


def _evidence_payload(task_id: str) -> str:
    return json.dumps(
        {
            "ok": True,
            "status": "completed",
            "task_id": task_id,
            "verification": [{"command": "pytest -q", "exit_code": 0}],
            "guard": {"verdict": "verified", "label": "验证通过", "reason": "ok"},
            "changes": {"files": [{"path": "a.py", "status": "added"}]},
        },
        ensure_ascii=False,
    )


class _ScriptedModel:
    """先调用工具（写证据），再给最终回答。"""

    def __init__(self, tool_calls: int = 1) -> None:
        self._tool_calls = tool_calls
        self._seen = 0

    def decide(self, messages: list[Message], tools=None):
        self._seen += 1
        if self._seen <= self._tool_calls:
            return ToolCall(name=EVIDENCE_TOOL, arguments={"task_id": f"t{self._seen}"})
        return FinalAnswer(content="已完成。")


def _make_store(tmp_path: Path) -> SQLiteThreadStore:
    store = SQLiteThreadStore(tmp_path / "threads.db")
    store.create("th-1")
    return store


def _make_registry(store: SQLiteThreadStore) -> ToolRegistry:
    """注册一个模拟 run_event 中间件的工具：执行时把证据写进 store。"""

    def _handler(arguments: dict, context: object) -> str:
        thread_id = getattr(context, "thread_id", "")
        payload = _evidence_payload(str(arguments.get("task_id", "")))
        # 与 api/middleware/run_event.py:_persist_run_result 同样的写路径
        thread = store.get(thread_id)
        thread.last_run_result = payload
        store.save(thread)
        return json.dumps({"ok": True}, ensure_ascii=False)

    registry = ToolRegistry()
    registry.register(
        Tool(
            name=EVIDENCE_TOOL,
            description="测试用：写入本轮执行证据",
            parameters={"type": "object", "properties": {}},
            handler=_handler,
            needs_context=True,
        )
    )
    return registry


class TestAgentKeepsRunEvidence:
    def test_成功运行后证据仍能从store读回(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        agent = Agent(
            model=_ScriptedModel(),
            registry=_make_registry(store),
            store=store,
            max_steps=4,
        )
        answer, _trace = agent.run("th-1", "做一件事")

        assert answer == "已完成。"
        thread = store.get("th-1")
        # 消息落库了
        assert [m.role for m in thread.messages] == ["user", "assistant"]
        # **执行证据没有被收尾保存抹掉**
        assert thread.last_run_result is not None
        payload = json.loads(thread.last_run_result)
        assert payload["task_id"] == "t1"
        assert payload["guard"]["verdict"] == "verified"
        assert payload["changes"]["files"][0]["path"] == "a.py"

    def test_证据是这一轮的_不是上一轮遗留的旧值(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        # 上一轮遗留的旧证据
        stale = store.get("th-1")
        stale.last_run_result = json.dumps({"task_id": "OLD"})
        store.save(stale)

        agent = Agent(
            model=_ScriptedModel(),
            registry=_make_registry(store),
            store=store,
            max_steps=4,
        )
        agent.run("th-1", "做一件事")

        payload = json.loads(store.get("th-1").last_run_result)
        assert payload["task_id"] == "t1"

    def test_失败运行同样保留证据(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        # 一直调工具 → 步数耗尽 → run 失败
        agent = Agent(
            model=_ScriptedModel(tool_calls=99),
            registry=_make_registry(store),
            store=store,
            max_steps=2,
        )
        with pytest.raises(RuntimeError):
            agent.run("th-1", "做一件事")

        thread = store.get("th-1")
        assert thread.last_run_result is not None
        # max_steps=2 → 工具被调用两次 → 保留的是**最后一次**执行证据
        assert json.loads(thread.last_run_result)["task_id"] == "t2"

    def test_失败后接一次成功运行_证据不被旧值覆盖(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        agent = Agent(
            model=_ScriptedModel(tool_calls=99),
            registry=_make_registry(store),
            store=store,
            max_steps=2,
        )
        with pytest.raises(RuntimeError):
            agent.run("th-1", "第一件事")
        first = json.loads(store.get("th-1").last_run_result)["task_id"]

        agent2 = Agent(
            model=_ScriptedModel(),
            registry=_make_registry(store),
            store=store,
            max_steps=4,
        )
        agent2.run("th-1", "第二件事")
        second = json.loads(store.get("th-1").last_run_result)["task_id"]

        assert first == "t2"
        assert second == "t1"
        # 关键：第二轮成功后，证据是**第二轮**的，且没有被清空
        assert store.get("th-1").last_run_result is not None
