"""MCP 管理前端 API 集成测试（TOOL-05 round 2）

- 真实 TestClient + 真实 runtime
- 断言前端 shared/api/mcp.ts 源文件含 /api 前缀
"""
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import mini_deerflow.api.dependencies as deps_module

from mini_deerflow.runtime import create_runtime, RuntimeConfig
from mini_deerflow.tools import Tool, ToolRegistry
from mini_deerflow.tool_authorization import ToolPolicy, ToolPolicyRegistry
from mini_deerflow.tool_contracts import make_mcp_tool_metadata


class _McpHarness:
    def __init__(self):
        self.temp_dir = tempfile.mkdtemp()
        # 隔离 MCP state
        from mini_deerflow.mcp_tool_state import MCPToolStateStore
        state_path = os.path.join(self.temp_dir, "mcp_state.json")
        self.runtime = create_runtime(
            store=None, registry=ToolRegistry(),
            config=RuntimeConfig(workspace_root=self.temp_dir, subagent_enabled=False, trace_enabled=False),
        )
        self.runtime.mcp_tool_state_store = MCPToolStateStore(path=state_path)
        # 注册一个工具到 policy
        self.runtime.agent._tool_policy_registry.register_metadata(make_mcp_tool_metadata(
            "mcp__echo__echo",
            declared_capabilities=frozenset({"COMPUTE"}),
        ))
        self.runtime.agent._tool_policy_registry.set_policy("mcp__echo__echo", ToolPolicy(
            enabled=False,
            allowed_principals=frozenset(),
            requires_confirmation=False,
        ))
        # mock mcp_manager
        class _McpStub:
            def __init__(self):
                self._tools = {"mcp__echo__echo": ("echo-server", "echo")}
            def list_server_states(self):
                return []  # no servers reported
        self.runtime.mcp_manager = _McpStub()
        from mini_deerflow.api.app import create_app
        from mini_deerflow.api.config import ApiSettings
        settings = ApiSettings()
        self.app = create_app(settings=settings)
        self.app.dependency_overrides[deps_module.get_runtime] = lambda: self.runtime
        self.client = TestClient(self.app)
        self.settings = settings


class TestMcpFrontendApiPaths(unittest.TestCase):
    """前端 mcp.ts 必须含 /api 前缀路径。"""

    def test_api_paths_in_source(self):
        repo = Path(__file__).resolve().parents[1]
        fe_path = repo / "frontend" / "src" / "shared" / "api" / "mcp.ts"
        src = fe_path.read_text(encoding="utf-8")
        self.assertIn("/api/mcp/servers", src)
        self.assertIn("/api/mcp/tools", src)
        # enable / grant / revoke 三个端点
        self.assertIn("/api/mcp/tools/", src)
        self.assertIn("/enable", src)
        self.assertIn("/grant", src)
        self.assertIn("/revoke", src)


class TestMcpEndpointIntegration(unittest.TestCase):
    """真实 TestClient 调 /api/mcp/* 端点。"""

    def setUp(self):
        self.h = _McpHarness()

    def test_list_servers_empty(self):
        resp = self.h.client.get(f"{self.h.settings.api_prefix}/mcp/servers")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("servers", resp.json())

    def test_list_tools(self):
        resp = self.h.client.get(f"{self.h.settings.api_prefix}/mcp/tools")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("tools", resp.json())

    def test_enable_three_principals_real_effect(self):
        """三主体 grant + revoke 真实修改 state 文件。"""
        for principal in ["MAIN_AGENT", "MINI_SUBAGENT", "HARNESS_AGENT"]:
            # grant
            resp = self.h.client.post(
                f"{self.h.settings.api_prefix}/mcp/tools/mcp__echo__echo/grant",
                json={"principal": principal},
            )
            self.assertIn(resp.status_code, (200, 204), resp.text)
            state = self.h.runtime.mcp_tool_state_store.get("mcp__echo__echo")
            self.assertIn(principal, [p.name for p in state.allowed_principals])
            # revoke
            resp2 = self.h.client.post(
                f"{self.h.settings.api_prefix}/mcp/tools/mcp__echo__echo/revoke",
                json={"principal": principal},
            )
            self.assertIn(resp2.status_code, (200, 204), resp2.text)
            state2 = self.h.runtime.mcp_tool_state_store.get("mcp__echo__echo")
            self.assertNotIn(principal, [p.name for p in state2.allowed_principals])


if __name__ == "__main__":
    unittest.main()
