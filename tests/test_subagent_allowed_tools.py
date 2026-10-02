"""Subagent `allowed_tools` 运行时强制（收口测试）。

覆盖两道防线：
- **暴露限制**：模型只拿到允许清单内的 `ToolSpec`
- **执行限制**：执行前再校验一次工具名，伪造也绕不过

同时覆盖三类工具（本地 / `mcp__*` / 子代理）与父 Agent 的权限隔离。
全部使用本地 stdio 测试 MCP Server 与假模型，**零网络、零额度**。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from mini_deerflow.agent import Agent
from mini_deerflow.mcp_config import MCPServerConfig
from mini_deerflow.mcp_manager import MCPClientManager
from mini_deerflow.middleware import Budget as SubagentBudget
from mini_deerflow.middleware import CancellationToken
from mini_deerflow.models import FakeModel, FinalAnswer, Message, ToolCall
from mini_deerflow.subagent_executor import SubagentRunner
from mini_deerflow.subagent_models import (
    SubagentContextPolicy,
    SubagentTask,
    TaskStatus,
)
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tools import (
    Tool,
    ToolNotAllowedError,
    ToolRegistry,
    create_default_registry,
    ensure_tool_allowed,
    filter_tool_specs,
)

SERVER_PATH = str(Path("tests/fixtures/mcp_test_server.py").resolve())


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class _ScriptedModel:
    """按脚本返回决策，并记录每轮实际拿到的 tools（用于断言暴露限制）。"""

    def __init__(self, decisions: list[Any]) -> None:
        self._decisions = list(decisions)
        self.tools_seen: list[list[str]] = []
        self.message_roles: list[list[str]] = []

    def decide(
        self, messages: list[Message], tools: list[Any] | None = None
    ) -> FinalAnswer | ToolCall:
        self.tools_seen.append([s.name for s in (tools or [])])
        self.message_roles.append([m.role for m in messages])
        return self._decisions.pop(0)


class _RecordingLocalTool:
    """本地工具：记录被调用次数与参数。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def handler(self, arguments: dict[str, object]) -> str:
        self.calls.append(dict(arguments))
        return f"local:{arguments.get('text', '')}"


def _local_tool(name: str, recorder: _RecordingLocalTool) -> Tool:
    return Tool(
        name=name,
        description=f"本地工具 {name}",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        handler=recorder.handler,
    )


def _run_subagent(
    registry: ToolRegistry,
    model: _ScriptedModel,
    allowed_tools: tuple[str, ...] | None,
    instruction: str = "执行",
) -> Any:
    """同步跑一个带指定 allowed_tools 的子任务，返回 SubagentResult。"""
    task = SubagentTask(
        task_id="sa-test",
        parent_run_id="run-0",
        parent_thread_id="main",
        instruction=instruction,
        status=TaskStatus.QUEUED,
        created_at=0.0,
        context_policy=SubagentContextPolicy(allowed_tools=allowed_tools),
    )
    runner = SubagentRunner(
        task=task,
        model=model,
        registry=registry,
        store=InMemoryThreadStore(),
        budget=SubagentBudget(max_steps=5, max_model_calls=10, max_tool_calls=10),
        cancellation_token=CancellationToken(),
    )
    return runner.run()


@pytest.fixture()
def mcp_registry() -> ToolRegistry:
    """本地 registry = calculator + local_note + 4 个真实 MCP 工具。"""
    recorder = _RecordingLocalTool()
    registry = create_default_registry()
    registry.register(_local_tool("local_note", recorder))

    config = MCPServerConfig(
        name="test",
        command=sys.executable,
        args=(SERVER_PATH,),
        startup_timeout=30.0,
        call_timeout=15.0,
    )
    manager = MCPClientManager([config])
    manager.start(registry)
    try:
        yield registry
    finally:
        manager.stop()


# ---------------------------------------------------------------------------
# 1. 暴露限制 + 执行限制的单元语义
# ---------------------------------------------------------------------------


class Test纵深防御语义:
    def test_none_表示不设限制(self) -> None:
        specs = create_default_registry().tool_specs()
        assert filter_tool_specs(specs, None) == specs
        ensure_tool_allowed("任意工具", None)  # 不抛

    def test_空白名单不暴露任何工具(self) -> None:
        specs = create_default_registry().tool_specs()
        assert filter_tool_specs(specs, frozenset()) == []

    def test_空白名单执行任何工具都被拒(self) -> None:
        with pytest.raises(ToolNotAllowedError):
            ensure_tool_allowed("calculator", frozenset())

    def test_白名单只放行命中的名字(self) -> None:
        allowed = frozenset({"calculator"})
        ensure_tool_allowed("calculator", allowed)
        with pytest.raises(ToolNotAllowedError) as exc:
            ensure_tool_allowed("local_note", allowed)
        assert "local_note" in str(exc.value)


# ---------------------------------------------------------------------------
# 2. 本地工具
# ---------------------------------------------------------------------------


class Test本地工具限制:
    def test_允许的本地工具可以执行(self) -> None:
        registry = create_default_registry()
        model = _ScriptedModel([
            ToolCall(name="calculator", arguments={"expression": "1+1"}, id="t1"),
            FinalAnswer(content="done"),
        ])
        result = _run_subagent(registry, model, allowed_tools=("calculator",))

        assert result.error_type is None
        assert model.tools_seen[0] == ["calculator"]
        assert model.tools_seen[1] == ["calculator"]

    def test_未允许的本地工具被拒绝(self) -> None:
        """registry 里有 local_note，但白名单没放行 → 执行限制必须拦下。"""
        recorder = _RecordingLocalTool()
        registry = create_default_registry()
        registry.register(_local_tool("local_note", recorder))

        model = _ScriptedModel([
            ToolCall(name="local_note", arguments={"text": "x"}, id="t1"),
        ])
        result = _run_subagent(registry, model, allowed_tools=("calculator",))

        assert result.error_type == ToolNotAllowedError.__name__
        assert "local_note" in (result.error_message or "")
        # 暴露限制也生效：模型根本没看到 local_note
        assert "local_note" not in model.tools_seen[0]
        # 真的没执行过
        assert recorder.calls == []


# ---------------------------------------------------------------------------
# 3. MCP 工具
# ---------------------------------------------------------------------------


class TestMCP工具限制:
    def test_允许的_mcp_工具可以执行(self, mcp_registry: ToolRegistry) -> None:
        model = _ScriptedModel([
            ToolCall(name="mcp__test__echo", arguments={"text": "hi"}, id="t1"),
            FinalAnswer(content="done"),
        ])
        result = _run_subagent(
            mcp_registry, model, allowed_tools=("mcp__test__echo",)
        )

        assert result.error_type is None
        # 暴露限制：只看到被允许的那一个 MCP 工具
        assert model.tools_seen[0] == ["mcp__test__echo"]

    def test_未允许的_mcp_工具被拒绝(self, mcp_registry: ToolRegistry) -> None:
        model = _ScriptedModel([
            ToolCall(name="mcp__test__add", arguments={"a": 1, "b": 2}, id="t1"),
        ])
        result = _run_subagent(
            mcp_registry, model, allowed_tools=("mcp__test__echo",)
        )

        assert result.error_type == ToolNotAllowedError.__name__
        assert "mcp__test__add" in (result.error_message or "")
        assert "mcp__test__add" not in model.tools_seen[0]

    def test_白名单可同时放行本地与_mcp_工具(
        self, mcp_registry: ToolRegistry
    ) -> None:
        model = _ScriptedModel([
            ToolCall(name="calculator", arguments={"expression": "2+3"}, id="t1"),
            ToolCall(name="mcp__test__echo", arguments={"text": "ok"}, id="t2"),
            FinalAnswer(content="done"),
        ])
        result = _run_subagent(
            mcp_registry,
            model,
            allowed_tools=("calculator", "mcp__test__echo"),
        )

        assert result.error_type is None
        assert model.tools_seen[0] == ["calculator", "mcp__test__echo"]


# ---------------------------------------------------------------------------
# 4. 空白名单
# ---------------------------------------------------------------------------


class Test空白名单:
    def test_不暴露任何工具(self, mcp_registry: ToolRegistry) -> None:
        model = _ScriptedModel([FinalAnswer(content="无工具可用")])
        result = _run_subagent(mcp_registry, model, allowed_tools=())

        assert result.error_type is None
        assert model.tools_seen == [[]]

    def test_执行任何工具都被拒绝(self, mcp_registry: ToolRegistry) -> None:
        for name, args in (
            ("calculator", {"expression": "1+1"}),
            ("local_note", {"text": "x"}),
            ("mcp__test__echo", {"text": "x"}),
        ):
            model = _ScriptedModel([ToolCall(name=name, arguments=args, id="t1")])
            result = _run_subagent(mcp_registry, model, allowed_tools=())
            assert result.error_type == ToolNotAllowedError.__name__, name
            assert model.tools_seen == [[]]


# ---------------------------------------------------------------------------
# 5. 模型伪造工具名
# ---------------------------------------------------------------------------


class Test伪造无法绕过:
    def test_伪造_registry_里存在但未授权的名字(self) -> None:
        """暴露限制挡不住"模型凭记忆编名字"，必须由执行限制兜底。"""
        recorder = _RecordingLocalTool()
        registry = create_default_registry()
        registry.register(_local_tool("local_note", recorder))

        model = _ScriptedModel([
            ToolCall(name="local_note", arguments={"text": "偷跑"}, id="t1"),
        ])
        result = _run_subagent(registry, model, allowed_tools=("calculator",))

        assert result.error_type == ToolNotAllowedError.__name__
        assert recorder.calls == []

    def test_伪造_不存在_的名字同样被拦(self) -> None:
        """执行限制先于 registry 查找，未知名字也走同一条稳定异常。"""
        registry = create_default_registry()
        model = _ScriptedModel([
            ToolCall(name="mcp__test__echo", arguments={"text": "x"}, id="t1"),
        ])
        # 白名单只给 calculator，registry 里甚至没有 mcp 工具
        result = _run_subagent(registry, model, allowed_tools=("calculator",))

        assert result.error_type == ToolNotAllowedError.__name__


# ---------------------------------------------------------------------------
# 6. 父 Agent 与 Subagent 权限互不污染
# ---------------------------------------------------------------------------


class Test父子权限隔离:
    def test_父_agent_不受子代理白名单影响(
        self, mcp_registry: ToolRegistry
    ) -> None:
        # 1) 子代理只允许 calculator
        sub_model = _ScriptedModel([
            ToolCall(name="local_note", arguments={"text": "x"}, id="t1"),
        ])
        sub_result = _run_subagent(
            mcp_registry, sub_model, allowed_tools=("calculator",)
        )
        assert sub_result.error_type == ToolNotAllowedError.__name__

        # 2) 父 Agent 用同一个 registry，仍可调用子代理被拒的工具
        parent_model = _ScriptedModel([
            ToolCall(name="local_note", arguments={"text": "父级"}, id="p1"),
            FinalAnswer(content="父级完成"),
        ])
        store = InMemoryThreadStore()
        store.create("t1")
        agent = Agent(model=parent_model, registry=mcp_registry, store=store)
        answer, _ = agent.run("t1", "执行")

        assert answer == "父级完成"
        # 父 Agent 看到的是**完整**工具集（白名单只作用于子代理）
        assert "local_note" in parent_model.tools_seen[0]
        assert "mcp__test__echo" in parent_model.tools_seen[0]

        # 3) 反过来：父级没被子代理的限制"污染"，也没把自己的限制带给子代理
        assert mcp_registry.get("local_note") is not None
        assert mcp_registry.get("mcp__test__echo") is not None

    def test_子代理白名单不修改注册表本身(self) -> None:
        recorder = _RecordingLocalTool()
        registry = create_default_registry()
        registry.register(_local_tool("local_note", recorder))

        model = _ScriptedModel([FinalAnswer(content="ok")])
        _run_subagent(registry, model, allowed_tools=())

        # 过滤只发生在子代理内部，注册表保持完整
        assert {t.name for t in registry.list_tools()} == {
            "calculator",
            "local_note",
        }


# ---------------------------------------------------------------------------
# 7. None 沿用默认策略
# ---------------------------------------------------------------------------


class Test默认策略不变:
    def test_none_时不做过滤(self) -> None:
        recorder = _RecordingLocalTool()
        registry = create_default_registry()
        registry.register(_local_tool("local_note", recorder))

        model = _ScriptedModel([
            ToolCall(name="local_note", arguments={"text": "默认"}, id="t1"),
            FinalAnswer(content="ok"),
        ])
        result = _run_subagent(registry, model, allowed_tools=None)

        assert result.error_type is None
        assert model.tools_seen[0] == ["calculator", "local_note"]
        assert recorder.calls == [{"text": "默认"}]
