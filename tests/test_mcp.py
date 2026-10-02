"""Phase 22 测试：MCP 扩展。

分层：
- 配置 / 适配层 / 事件循环桥：不启动任何子进程
- 集成：启动本地 `tests/fixtures/mcp_test_server.py`（确定性，不访问外部网络）

真实子进程只在集成用例里短暂存在，且每个用例结束都断言无残留。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import psutil
import pytest

from mini_deerflow.agent import Agent
from mini_deerflow.mcp_adapter import (
    MCPToolAdapter,
    detect_conflicts,
    qualify_tool_name,
    truncate_output,
    validate_arguments,
)
from mini_deerflow.mcp_bridge import (
    AsyncPortal,
    PortalClosedError,
    PortalTimeoutError,
)
from mini_deerflow.mcp_config import (
    MCPConfigError,
    MCPServerConfig,
    load_mcp_config,
    parse_server_config,
)
from mini_deerflow.mcp_manager import MCPClientManager, MCPStartupError, build_child_env
from mini_deerflow.model_errors import ModelProtocolError
from mini_deerflow.models import FakeModel, ToolSpec
from mini_deerflow.runtime import RuntimeConfig, create_runtime
# 源码里的类名是 SubagentContextPolicy；本文件只关心 allowed_tools 语义，
# 用局部别名 ContextPolicy 让断言读起来更短（不改公共类名）。
from mini_deerflow.subagent_models import SubagentContextPolicy as ContextPolicy
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tools import (
    ToolExecutionError,
    ToolNotFoundError,
    create_default_registry,
)

SERVER_PATH = str(Path("tests/fixtures/mcp_test_server.py").resolve())


def make_config(**overrides) -> MCPServerConfig:
    """本地测试服务器配置（不访问外部网络）。"""
    base = dict(
        name="test",
        command=sys.executable,
        args=(SERVER_PATH,),
        startup_timeout=30.0,
        call_timeout=15.0,
    )
    base.update(overrides)
    return MCPServerConfig(**base)


def child_pids() -> set[int]:
    """当前进程的直接子进程 PID 集合。"""
    try:
        return {child.pid for child in psutil.Process(os.getpid()).children(recursive=True)}
    except psutil.Error:  # pragma: no cover - 环境不支持时跳过
        pytest.skip("psutil 无法枚举子进程")


# ---------------------------------------------------------------------------
# 1. 配置
# ---------------------------------------------------------------------------


class Test配置解析:
    def test_合法配置(self) -> None:
        config = parse_server_config(
            {
                "name": "demo",
                "command": "python",
                "args": ["-m", "pkg"],
                "env": {"A": "1"},
                "enabled": True,
                "required": True,
                "startup_timeout": 10,
                "call_timeout": 5,
                "max_output_bytes": 100,
            },
            0,
        )
        assert config.name == "demo"
        assert config.command == "python"
        assert config.args == ("-m", "pkg")
        assert config.env == {"A": "1"}
        assert config.required is True
        assert config.startup_timeout == 10.0
        assert config.max_output_bytes == 100

    @pytest.mark.parametrize(
        "payload,marker",
        [
            ({"command": "python"}, "name"),
            ({"name": "x"}, "command"),
            ({"name": "x", "command": "python", "args": ["a", 1]}, "args"),
            ({"name": "x", "command": "python", "env": {"A": 1}}, "env"),
            ({"name": "x", "command": "python", "enabled": "yes"}, "enabled"),
            ({"name": "x", "command": "python", "startup_timeout": -1}, "startup_timeout"),
            ({"name": "x", "command": "python", "max_output_bytes": 0}, "max_output_bytes"),
        ],
    )
    def test_非法字段_抛配置错误(self, payload, marker) -> None:
        with pytest.raises(MCPConfigError) as exc:
            parse_server_config(payload, 0)
        assert marker in str(exc.value)

    def test_repr_不泄露_env_值(self) -> None:
        config = MCPServerConfig(name="demo", command="python",
                                 env={"SECRET_TOKEN": "s3cr3t-value"})
        text = repr(config)
        assert "s3cr3t-value" not in text
        assert "SECRET_TOKEN" in text  # 只暴露键名

    def test_未配置文件_返回空列表(self, monkeypatch) -> None:
        monkeypatch.delenv("MINI_DEERFLOW_MCP_CONFIG", raising=False)
        assert load_mcp_config() == []

    def test_禁用项被过滤(self, tmp_path) -> None:
        path = tmp_path / "mcp.json"
        path.write_text(json.dumps({"servers": [
            {"name": "on", "command": "python", "enabled": True},
            {"name": "off", "command": "python", "enabled": False},
        ]}), encoding="utf-8")
        configs = load_mcp_config(path)
        assert [c.name for c in configs] == ["on"]

    def test_文件不存在(self, tmp_path) -> None:
        with pytest.raises(MCPConfigError):
            load_mcp_config(tmp_path / "missing.json")

    def test_json_非法(self, tmp_path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(MCPConfigError):
            load_mcp_config(path)

    def test_服务器名重复(self, tmp_path) -> None:
        path = tmp_path / "dup.json"
        path.write_text(json.dumps({"servers": [
            {"name": "same", "command": "python"},
            {"name": "same", "command": "python"},
        ]}), encoding="utf-8")
        with pytest.raises(MCPConfigError) as exc:
            load_mcp_config(path)
        assert "重复" in str(exc.value)


# ---------------------------------------------------------------------------
# 2. 事件循环桥
# ---------------------------------------------------------------------------


class Test事件循环桥:
    def test_run_与超时(self) -> None:
        import asyncio

        portal = AsyncPortal()
        try:
            assert portal.run(asyncio.sleep(0.01, "ok"), timeout=5) == "ok"
            with pytest.raises(PortalTimeoutError):
                portal.run(asyncio.sleep(5), timeout=0.2)
        finally:
            portal.close()

    def test_关闭后不可提交(self) -> None:
        import asyncio

        portal = AsyncPortal()
        portal.close()
        with pytest.raises(PortalClosedError):
            portal.run(asyncio.sleep(0))

    def test_关闭幂等(self) -> None:
        portal = AsyncPortal()
        portal.close()
        portal.close()  # 第二次必须无副作用
        assert portal.closed


# ---------------------------------------------------------------------------
# 3. 适配层
# ---------------------------------------------------------------------------


def _spec(name: str = "calculator") -> ToolSpec:
    return ToolSpec(
        name=name,
        description="计算",
        input_schema={
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
        },
    )


class _FakeMCPTool:
    def __init__(self, name="echo", description="d",
                 input_schema=None) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema if input_schema is not None else {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }


class Test适配层:
    def test_命名空间(self) -> None:
        assert qualify_tool_name("srv", "echo") == "mcp__srv__echo"
        with pytest.raises(ModelProtocolError):
            qualify_tool_name("", "echo")
        with pytest.raises(ModelProtocolError):
            qualify_tool_name("srv", "bad__name")

    def test_工具名冲突_抛错(self) -> None:
        with pytest.raises(ModelProtocolError):
            detect_conflicts({"mcp__srv__echo"}, "mcp__srv__echo")
        detect_conflicts({"mcp__other__echo"}, "mcp__srv__echo")  # 不同前缀不冲突

    def test_输出按字节截断(self) -> None:
        assert truncate_output("short", 100) == "short"
        clipped = truncate_output("中文abc" * 50, 20)
        assert "已截断" in clipped
        assert len(clipped.encode("utf-8")) < 100

    def test_缺必填参数(self) -> None:
        with pytest.raises(ModelProtocolError) as exc:
            validate_arguments(_spec(), {})
        assert "expression" in str(exc.value)
        validate_arguments(_spec(), {"expression": "1+1"})  # 不抛

    def test_schema_转换(self) -> None:
        adapter = MCPToolAdapter("srv", 10.0, 1024, lambda *a: "x")
        spec = adapter.to_spec(_FakeMCPTool())
        assert spec.name == "mcp__srv__echo"
        assert spec.input_schema["required"] == ["text"]
        assert spec.description == "d"

    def test_无_schema_抛协议错误(self) -> None:
        adapter = MCPToolAdapter("srv", 10.0, 1024, lambda *a: "x")

        class Bad:
            name = "bad"
            description = "d"
            input_schema = "not-a-dict"

        with pytest.raises(ModelProtocolError):
            adapter.to_spec(Bad())

    def test_handler_会校验参数再回调(self) -> None:
        seen: list = []
        adapter = MCPToolAdapter(
            "srv", 10.0, 1024,
            lambda n, a, t: seen.append((n, a, t)) or "ok",
        )
        _, tool = adapter.to_tool(_FakeMCPTool())

        # 缺必填参数 → 校验失败，invoker 不会被调用
        with pytest.raises(ModelProtocolError):
            tool.execute({})
        assert seen == []

        assert tool.execute({"text": "hi"}) == "ok"
        assert seen == [("mcp__srv__echo", {"text": "hi"}, 10.0)]

    def test_handler_限制输出长度(self) -> None:
        adapter = MCPToolAdapter(
            "srv", 10.0, 8, lambda *a: "x" * 1000,
        )
        _, tool = adapter.to_tool(_FakeMCPTool())
        assert "已截断" in tool.execute({"text": "hi"})


# ---------------------------------------------------------------------------
# 4. 子进程环境
# ---------------------------------------------------------------------------


class Test子进程环境:
    def test_白名单被注入(self) -> None:
        env = build_child_env({"MY_TOKEN": "v"})
        assert env["MY_TOKEN"] == "v"

    def test_不继承应用密钥(self) -> None:
        env = build_child_env({})
        assert not [k for k in env if k.endswith("_API_KEY")]
        # 必须保留启动进程所需的基本变量
        assert "PATH" in env


# ---------------------------------------------------------------------------
# 5. 集成：真实本地子进程
# ---------------------------------------------------------------------------


class TestMCP集成:
    def test_启动与工具发现(self) -> None:
        registry = create_default_registry()
        manager = MCPClientManager([make_config()])
        try:
            count = manager.start(registry)
            assert count == 4
            assert manager.running_servers == ("test",)
            names = {t.name for t in registry.list_tools()}
            assert {"mcp__test__echo", "mcp__test__add",
                    "mcp__test__failing_tool", "mcp__test__wait"} <= names
        finally:
            manager.stop()

    def test_echo_add_调用(self) -> None:
        registry = create_default_registry()
        manager = MCPClientManager([make_config()])
        try:
            manager.start(registry)
            assert registry.execute("mcp__test__echo", {"text": "hi"}) == "hi"
            assert registry.execute("mcp__test__add", {"a": 17, "b": 25}) == "42"
        finally:
            manager.stop()

    def test_参数错误在发起前被拦截(self) -> None:
        """缺必填参数 → 不发起 MCP 调用，直接失败。

        ToolRegistry.execute 会把所有异常统一包成 ToolExecutionError，
        真正的原因挂在 __cause__ 上——这是注册表既有约定，不因 MCP 而改变。
        """
        registry = create_default_registry()
        manager = MCPClientManager([make_config()])
        try:
            manager.start(registry)
            with pytest.raises(ToolExecutionError) as exc:
                registry.execute("mcp__test__echo", {})  # 缺 text
            assert isinstance(exc.value.__cause__, ModelProtocolError)
            assert "text" in str(exc.value.__cause__)
        finally:
            manager.stop()

    def test_未知工具(self) -> None:
        registry = create_default_registry()
        manager = MCPClientManager([make_config()])
        try:
            manager.start(registry)
            with pytest.raises(ToolNotFoundError):
                registry.execute("mcp__test__ghost", {"text": "x"})
        finally:
            manager.stop()

    def test_工具失败_返回错误文本而非崩溃(self) -> None:
        registry = create_default_registry()
        manager = MCPClientManager([make_config()])
        try:
            manager.start(registry)
            result = registry.execute("mcp__test__failing_tool", {"reason": "x"})
            # MCP 的失败在协议层是 is_error 结果，不是异常；
            # 显式带出失败信息，模型与日志都能看见
            assert "失败" in result or "Error" in result
        finally:
            manager.stop()

    def test_调用超时(self) -> None:
        registry = create_default_registry()
        manager = MCPClientManager([make_config(call_timeout=0.5)])
        try:
            manager.start(registry)
            with pytest.raises(ToolExecutionError) as exc:
                registry.execute("mcp__test__wait", {"seconds": 5.0})
            # 原因是超时（不是工具内部错误）
            assert isinstance(exc.value.__cause__, TimeoutError)
        finally:
            manager.stop()

    def test_required_启动失败_fail_closed(self) -> None:
        registry = create_default_registry()
        manager = MCPClientManager([
            make_config(name="broken", command="definitely-not-a-real-cmd",
                        required=True, startup_timeout=10.0)
        ])
        with pytest.raises(MCPStartupError):
            manager.start(registry)
        # fail-closed 后不残留
        assert not manager.started
        assert manager.running_servers == ()
        assert manager.registered_names == ()

    def test_optional_启动失败只记录并跳过(self) -> None:
        registry = create_default_registry()
        good = make_config(name="good")
        bad = make_config(name="broken", command="definitely-not-a-real-cmd",
                          required=False, startup_timeout=10.0)
        manager = MCPClientManager([bad, good])
        try:
            count = manager.start(registry)
            # optional 失败不影响另一个服务器
            assert manager.running_servers == ("good",)
            assert count == 4
            assert not [n for n in manager.registered_names if "broken" in n]
        finally:
            manager.stop()

    def test_shutdown_无残留子进程(self) -> None:
        registry = create_default_registry()
        manager = MCPClientManager([make_config()])
        before = child_pids()
        manager.start(registry)
        during = child_pids() - before
        assert during, "应当确实拉起了子进程"
        manager.stop()
        # 给内核一点回收时间，再断言全部退出
        for _ in range(50):
            if not (child_pids() & during):
                break
            import time
            time.sleep(0.1)
        assert not (child_pids() & during), f"残留子进程: {child_pids() & during}"

    def test_stop_幂等(self) -> None:
        registry = create_default_registry()
        manager = MCPClientManager([make_config()])
        manager.start(registry)
        manager.stop()
        manager.stop()
        assert not manager.started
        assert manager.registered_names == ()

    def test_默认不启用_不启动任何子进程(self) -> None:
        registry = create_default_registry()
        before = child_pids()
        manager = MCPClientManager([])  # 等价于未配置 MINI_DEERFLOW_MCP_CONFIG
        assert manager.start(registry) == 0
        assert not manager.enabled
        assert not manager.started
        assert child_pids() == before

    def test_工具描述进入模型可见的_tool_specs(self) -> None:
        registry = create_default_registry()
        manager = MCPClientManager([make_config()])
        try:
            manager.start(registry)
            specs = registry.tool_specs()
            mcp_specs = [s for s in specs if s.name.startswith("mcp__")]
            assert len(mcp_specs) == 4
            # ToolSpec 不含 handler，模型侧拿不到可执行对象
            assert not hasattr(mcp_specs[0], "handler")
        finally:
            manager.stop()


# ---------------------------------------------------------------------------
# 6. Runtime / Agent 完整调用链
# ---------------------------------------------------------------------------


class TestRuntimeAgent调用链:
    def test_runtime_启动时连接_关闭时清理(self) -> None:
        before = child_pids()
        registry = create_default_registry()
        ctx = create_runtime(
            store=InMemoryThreadStore(),
            registry=registry,
            config=RuntimeConfig(mcp_servers=[make_config()]),
            model=FakeModel(),
        )
        try:
            assert ctx.mcp_manager.started
            assert "mcp__test__echo" in {
                t.name for t in ctx.agent._registry.list_tools()
            }
            during = child_pids() - before
            assert during
        finally:
            ctx.shutdown()

        import time
        for _ in range(50):
            if not (child_pids() & during):
                break
            time.sleep(0.1)
        assert not (child_pids() & during)

    def test_关闭幂等(self) -> None:
        registry = create_default_registry()
        ctx = create_runtime(
            store=InMemoryThreadStore(),
            registry=registry,
            config=RuntimeConfig(mcp_servers=[make_config()]),
            model=FakeModel(),
        )
        ctx.shutdown()
        ctx.shutdown()
        assert not ctx.mcp_manager.started

    def test_agent_闭环可调用_mcp_工具(self) -> None:
        """真实 Agent 循环：模型选 MCP 工具 → 执行 → 结果回传 → FinalAnswer。"""
        from mini_deerflow.models import FinalAnswer, Message, ToolCall

        store = InMemoryThreadStore()
        store.create("t1")
        registry = create_default_registry()

        class Scripted:
            def __init__(self) -> None:
                self.calls = 0

            def decide(self, messages, tools=None):
                self.calls += 1
                if self.calls == 1:
                    # 模型从 tools 里看到 MCP 工具并选择它
                    assert any(t.name == "mcp__test__add" for t in tools)
                    return ToolCall(name="mcp__test__add",
                                    arguments={"a": 17, "b": 25},
                                    id="toolu_1")
                return FinalAnswer(content=f"结果 {messages[-1].content}")

        ctx = create_runtime(
            store=store, registry=registry,
            config=RuntimeConfig(mcp_servers=[make_config()]),
            model=Scripted(),
        )
        try:
            answer, trace = ctx.agent.run("t1", "算一下")
            # MCP add(17,25)=42 回到了模型上下文
            assert answer == "结果 42"
            assert [t["action"] for t in trace] == [
                "model_decision", "tool_call", "final_answer",
            ]
        finally:
            ctx.shutdown()

    def test_未配置时_runtime_不带_mcp_工具(self) -> None:
        before = child_pids()
        ctx = create_runtime(
            store=InMemoryThreadStore(),
            registry=create_default_registry(),
            config=RuntimeConfig(),
            model=FakeModel(),
        )
        try:
            assert not ctx.mcp_manager.enabled
            assert all(
                not t.name.startswith("mcp__")
                for t in ctx.agent._registry.list_tools()
            )
            assert child_pids() == before
        finally:
            ctx.shutdown()


# ---------------------------------------------------------------------------
# 7. Subagent allowed_tools 保持可用
# ---------------------------------------------------------------------------


class TestSubagentAllowedTools:
    def test_加入_mcp_工具后_policy_往返不变(self) -> None:
        """allowed_tools 是持久化策略字段：注册 MCP 工具不得改变它的语义。

        注意：`allowed_tools` 目前**只在 SubagentContextPolicy 上做存取，
        运行时并没有据此过滤工具**。这里测的是"字段往返稳定 + 与工具集合解耦"，
        不假造一个并不存在的执行语义。
        """
        from mini_deerflow.subagent_models import SubagentContextPolicy

        registry = create_default_registry()
        manager = MCPClientManager([make_config()])
        try:
            manager.start(registry)

            policy = SubagentContextPolicy(allowed_tools=("calculator",))
            # 字段本身是不可变 dataclass，注册 MCP 工具不会写入它
            assert policy.allowed_tools == ("calculator",)
            assert "mcp__test__echo" not in policy.allowed_tools

            # 注册表里确实多了 MCP 工具（说明工具集合与策略字段彼此独立）
            assert any(t.name.startswith("mcp__") for t in registry.list_tools())
        finally:
            manager.stop()

    def test_policy_默认不限制(self) -> None:
        from mini_deerflow.subagent_models import SubagentContextPolicy

        assert SubagentContextPolicy().allowed_tools is None

    def test_policy_不可变(self) -> None:
        from mini_deerflow.subagent_models import SubagentContextPolicy

        policy = SubagentContextPolicy(allowed_tools=("calculator",))
        with pytest.raises(Exception):
            policy.allowed_tools = ("mcp__test__echo",)  # type: ignore[misc]
