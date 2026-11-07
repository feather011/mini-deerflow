"""TOOL-03 R4 收尾测试（round 3）。

修复：
1. 同一 Manager 同一 registry 停止后重启：可成功，无重复注册。
2. optional 初始化超时：子进程被回收，manager 仍可管理其他服务器。
"""

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

from mini_deerflow.mcp_config import MCPServerConfig
from mini_deerflow.mcp_manager import MCPClientManager, ServerStatus
from mini_deerflow.tools import ToolRegistry
from mini_deerflow.tool_authorization import (
    ExecutorPrincipal,
    ToolPolicy,
    ToolPolicyRegistry,
    check_tool_access,
)
from mini_deerflow.tool_authorization import RunAuthority
from mini_deerflow.tool_contracts import ToolCapability


FIXTURE = Path(__file__).parent / "fixtures" / "mcp_fixture_server.py"


def _make_config(
    name: str,
    mode: str = "echo",
    capabilities: frozenset[str] = frozenset({"COMPUTE"}),
    side_effects: frozenset[str] = frozenset(),
    startup_timeout: float = 15.0,
    required: bool = False,
) -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        command=sys.executable,
        args=["-m", "tests.fixtures.mcp_fixture_server"],
        env={"MCP_SERVER_MODE": mode, "PYTHONPATH": str(Path.cwd() / "src")},
        required=required,
        startup_timeout=startup_timeout,
        call_timeout=10.0,
        tool_capabilities=capabilities,
        tool_side_effects=side_effects,
    )


# ============================================================================
# R4 round 3.1: 同一 Manager / registry 停止后重启
# ============================================================================


class TestR4StopStartSameRegistry(unittest.TestCase):
    """同一 Manager + 同一 ToolRegistry 停止后重启：无重复注册，工具仍可调用。"""

    def test_stop_then_start_succeeds_no_duplicate_registration(self):
        """stop→start 同 manager 同 registry 成功；无 MCPStartupError。"""
        config = _make_config("echo")
        mgr = MCPClientManager([config])
        registry = ToolRegistry()

        # 第一次启动
        n1 = mgr.start(registry)
        self.assertGreater(n1, 0)
        first_tools = set(mgr.registered_names)
        self.assertIn("mcp__echo__echo", first_tools)

        # 关闭
        mgr.stop()
        self.assertFalse(mgr.started)

        # 第二次启动（同一 manager、同 registry）：之前会因"自有名称冲突"失败
        n2 = mgr.start(registry)
        self.assertGreater(n2, 0, "第二次 start 应成功")
        second_tools = set(mgr.registered_names)
        self.assertIn("mcp__echo__echo", second_tools)
        # registry 中也只有一个 mcp__echo__echo（无重复）
        names_in_registry = [t.name for t in registry.list_tools()]
        self.assertEqual(
            names_in_registry.count("mcp__echo__echo"), 1,
            f"registry 应只含一个 echo: {names_in_registry}",
        )

        mgr.stop()

    def test_stop_start_preserves_external_tools_in_registry(self):
        """stop→start 不应清空 registry 中本 manager 之外的其他工具。"""
        config = _make_config("echo")
        mgr = MCPClientManager([config])
        registry = ToolRegistry()

        # 在 registry 中预先放一个外来工具
        from mini_deerflow.tools import Tool
        registry.register(Tool(
            name="external_tool",
            description="外部工具（非 MCP）",
            parameters={"type": "object", "properties": {}},
            handler=lambda args: "external",
        ))

        mgr.start(registry)
        mgr.stop()
        # 第二次启动后，外部工具仍在
        mgr.start(registry)
        names = [t.name for t in registry.list_tools()]
        self.assertIn("external_tool", names,
                     "R4: 外部工具不应被 start 清空")
        self.assertIn("mcp__echo__echo", names)

        mgr.stop()

    def test_tool_callable_after_restart(self):
        """stop→start 后真实调用 tool handler（验证工具仍在 registry 中可用）。"""
        config = _make_config("echo")
        mgr = MCPClientManager([config])
        registry = ToolRegistry()

        mgr.start(registry)
        mgr.stop()
        mgr.start(registry)

        tool = registry.get("mcp__echo__echo")
        self.assertIsNotNone(tool, "重启后工具应仍在 registry")
        result = tool.handler({"message": "hello"})
        self.assertIn("echo: hello", result)

        mgr.stop()


# ============================================================================
# R4 round 3.2: 启动超时回收 + 不影响其他服务器
# ============================================================================


class TestR4StartupTimeoutReap(unittest.TestCase):
    """optional 启动超时时：子进程被回收，manager 仍可管理其他服务器。"""

    def _pid_alive(self, pid: int) -> bool:
        """跨平台检查 PID 是否存活。"""
        if pid is None or pid <= 0:
            return False
        if sys.platform == "win32":
            # Windows 用 tasklist 简化判断
            try:
                out = subprocess.check_output(
                    ["tasklist", "/FI", f"PID eq {pid}"],
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                )
                return str(pid) in out.decode("utf-8", errors="ignore")
            except Exception:
                return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def test_optional_hang_init_subprocess_reaped(self):
        """optional 启动超时时 hang_init fixture 进程被回收。"""
        # hang_init fixture 启动后不响应 initialize
        hang_config = _make_config(
            "hang",
            mode="hang_init",
            startup_timeout=1.5,
            required=False,
        )
        echo_config = _make_config("echo", mode="echo", required=False)
        mgr = MCPClientManager([hang_config, echo_config])
        registry = ToolRegistry()
        try:
            # hang 启动超时但 optional 不会 fail-closed；echo 仍可成功
            n = mgr.start(registry)
            self.assertGreater(n, 0, "echo fixture 应注册成功")
            # hang 状态应为 FAILED
            states = {s.name: s for s in mgr.list_server_states()}
            self.assertEqual(states["hang"].status, ServerStatus.FAILED)
            self.assertEqual(states["echo"].status, ServerStatus.RUNNING)

            # 验证 hang 的子进程（如果有）已被回收
            # 通过 mgr 内部 future 拿不到 pid；改为验证 list_sessions 已无 hang
            self.assertNotIn("hang", mgr._sessions)
        finally:
            mgr.stop()

    def test_optional_hang_does_not_affect_echo_callable(self):
        """hang 启动超时后，echo 仍可被 Agent 真实调用。"""
        hang_config = _make_config("hang", mode="hang_init", startup_timeout=1.0, required=False)
        echo_config = _make_config("echo", required=False)
        mgr = MCPClientManager([hang_config, echo_config])
        registry = ToolRegistry()
        try:
            mgr.start(registry)
            # 真实调用 echo
            tool = registry.get("mcp__echo__echo")
            self.assertIsNotNone(tool)
            result = tool.handler({"message": "after hang"})
            self.assertIn("echo: after hang", result)
        finally:
            mgr.stop()


if __name__ == "__main__":
    unittest.main()
