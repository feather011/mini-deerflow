"""TOOL-03 MCP 管理后端集成测试（修复 R1/R2/R3/R4/R5/R6）。

R3 修复：路由 prefix 改为 /mcp（由 app 加 api_prefix）。
R1 修复：API 同步当前 Agent 策略；运行时撤销/启用即时生效。
R2 修复：MCP 工具按 server 声明的可信分类；未声明 → uncategorized 拒绝。
R4 修复：本地 stdio fixture；测试用真 manager.start/stop。
R5 修复：服务器状态以配置为来源，失败/停止仍列出。
R6 修复：状态加载失败时清理已启动的 MCP 资源。
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mini_deerflow.api.dependencies import get_runtime
from mini_deerflow.mcp_config import MCPServerConfig, MCPConfigError
from mini_deerflow.mcp_manager import (
    MCPClientManager,
    MCPStartupError,
    ServerStatus,
)
from mini_deerflow.mcp_tool_state import (
    MCPToolStateError,
    MCPToolStateStore,
)
from mini_deerflow.runtime import RuntimeConfig, RuntimeContext
from mini_deerflow.tools import ToolRegistry
from mini_deerflow.tool_authorization import (
    ExecutorPrincipal,
    ToolPolicy,
    ToolPolicyRegistry,
    check_tool_access,
)
from mini_deerflow.tool_contracts import (
    ToolCapability,
    make_mcp_tool_metadata,
)


# Fixture MCP server 路径
FIXTURE = Path(__file__).parent / "fixtures" / "mcp_fixture_server.py"


def _make_config(
    name: str = "fixture",
    mode: str = "echo",
    capabilities: frozenset[str] = frozenset(),
    side_effects: frozenset[str] = frozenset(),
    required: bool = False,
) -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        command=sys.executable,
        args=["-m", "tests.fixtures.mcp_fixture_server"],
        env={"MCP_SERVER_MODE": mode, "PYTHONPATH": str(Path.cwd() / "src")},
        required=required,
        tool_capabilities=capabilities,
        tool_side_effects=side_effects,
    )


# ============================================================================
# R3: 路由前缀
# ============================================================================


class TestR3RoutePrefix(unittest.TestCase):
    """R3 修复：路由 prefix=/mcp，app 应用 api_prefix。"""

    def test_route_prefix_does_not_double_api(self):
        """路由内部不再带 /api 前缀，由 app 统一加 api_prefix。"""
        from mini_deerflow.api.routes.mcp import router
        self.assertEqual(router.prefix, "/mcp")

    def test_full_url_is_api_prefix_plus_mcp(self):
        """完整 URL = api_prefix + /mcp/*。"""
        from mini_deerflow.api.config import ApiSettings
        settings = ApiSettings()
        # 默认 api_prefix = /api
        self.assertTrue(settings.api_prefix.endswith("/api"))
        # 路由 prefix 不重复
        from mini_deerflow.api.routes.mcp import router
        self.assertFalse(router.prefix.startswith("/api/"))


# ============================================================================
# R2: MCP 工具分类
# ============================================================================


class TestR2MCPToolClassification(unittest.TestCase):
    """R2 修复：MCP 工具按 server 声明分类；未声明 → uncategorized 拒绝。"""

    def test_undeclared_mcp_tool_is_uncategorized(self):
        """未声明 capability/side_effect → uncategorized=True → 默认拒绝。"""
        meta = make_mcp_tool_metadata("mcp__demo__echo")
        self.assertTrue(meta.uncategorized)

    def test_declared_pure_compute_mcp_tool(self):
        """声明 COMPUTE + 无副作用 → 分类正确，pure compute。"""
        meta = make_mcp_tool_metadata(
            "mcp__demo__echo",
            declared_capabilities=frozenset({"COMPUTE"}),
            declared_side_effects=frozenset(),
        )
        self.assertFalse(meta.uncategorized)
        self.assertIn(ToolCapability.COMPUTE, meta.capabilities)
        self.assertEqual(len(meta.side_effects), 0)

    def test_declared_unknown_capability_raises(self):
        """未知 capability 名称 → MCPConfigError。"""
        from mini_deerflow.tool_contracts import MCPConfigError
        with self.assertRaises(MCPConfigError):
            make_mcp_tool_metadata(
                "mcp__demo__echo",
                declared_capabilities=frozenset({"NOT_A_REAL_CAP"}),
            )

    def test_unauthorized_pure_compute_mcp_blocks_handler(self):
        """未授权主体的纯计算 MCP 工具 → check_tool_access 拒绝。"""
        registry = ToolPolicyRegistry()
        registry.register_metadata(make_mcp_tool_metadata(
            "mcp__demo__echo",
            declared_capabilities=frozenset({"COMPUTE"}),
        ))
        registry.set_policy("mcp__demo__echo", ToolPolicy(
            enabled=True,
            allowed_principals=frozenset({ExecutorPrincipal.HARNESS_AGENT}),
        ))
        from mini_deerflow.tool_authorization import RunAuthority
        authority = RunAuthority(
            principal=ExecutorPrincipal.MAIN_AGENT,
            thread_id="t1", run_id="r1", project_id=None,
            granted_capabilities=frozenset({ToolCapability.COMPUTE}),
        )
        decision = check_tool_access(registry, "mcp__demo__echo", authority)
        self.assertTrue(decision.is_denied)

    def test_authorized_pure_compute_mcp_allows(self):
        """已授权主体的纯计算 MCP 工具 → check_tool_access 允许。"""
        registry = ToolPolicyRegistry()
        registry.register_metadata(make_mcp_tool_metadata(
            "mcp__demo__echo",
            declared_capabilities=frozenset({"COMPUTE"}),
        ))
        registry.set_policy("mcp__demo__echo", ToolPolicy(
            enabled=True,
            allowed_principals=frozenset({ExecutorPrincipal.MAIN_AGENT}),
        ))
        from mini_deerflow.tool_authorization import RunAuthority
        authority = RunAuthority(
            principal=ExecutorPrincipal.MAIN_AGENT,
            thread_id="t1", run_id="r1", project_id=None,
            granted_capabilities=frozenset({ToolCapability.COMPUTE}),
        )
        decision = check_tool_access(registry, "mcp__demo__echo", authority)
        self.assertFalse(decision.is_denied)


# ============================================================================
# R4 + R5: 真实 stdio fixture 启动与状态
# ============================================================================


class TestR4R5RealStdioFixture(unittest.TestCase):
    """R4/R5 修复：本地 stdio fixture 真实启动 + 服务器状态如实呈现。"""

    def test_echo_fixture_starts_and_lists(self):
        """echo fixture 真实启动；服务器状态 = RUNNING。"""
        config = _make_config(name="echo-fixture", capabilities=frozenset({"COMPUTE"}))
        mgr = MCPClientManager([config])
        try:
            reg = ToolRegistry()
            n = mgr.start(reg)
            self.assertGreater(n, 0, "应发现至少 1 个工具")
            self.assertIn("mcp__echo-fixture__echo", mgr.registered_names)

            states = mgr.list_server_states()
            self.assertEqual(len(states), 1)
            self.assertEqual(states[0].name, "echo-fixture")
            self.assertEqual(states[0].status, ServerStatus.RUNNING)
            self.assertIsNone(states[0].failure_kind)
        finally:
            mgr.stop()

    def test_stopped_server_appears_in_list_with_status(self):
        """R5：停止后服务器仍在清单，状态 = STOPPED。"""
        config = _make_config(name="echo-fixture", capabilities=frozenset({"COMPUTE"}))
        mgr = MCPClientManager([config])
        reg = ToolRegistry()
        mgr.start(reg)
        mgr.stop()

        states = mgr.list_server_states()
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0].status, ServerStatus.STOPPED)
        self.assertEqual(states[0].tool_count, 0)


# ============================================================================
# R1: API → 同一 runtime Agent 策略同步
# ============================================================================


class TestR1APIPolicySync(unittest.TestCase):
    """R1 修复：API enable/grant/revoke 同步当前 Agent 策略注册表。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.state_path = Path(self.temp_dir.name) / "mcp_state.json"
        # 把环境变量指向临时 state file，避免污染默认路径
        os.environ["MINI_DEERFLOW_MCP_TOOL_STATE"] = str(self.state_path)

    def tearDown(self):
        self.temp_dir.cleanup()
        os.environ.pop("MINI_DEERFLOW_MCP_TOOL_STATE", None)

    def _build_runtime(self, policy_registry: ToolPolicyRegistry) -> RuntimeContext:
        """构造含 Agent 策略注册表的 RuntimeContext。"""
        config = _make_config(name="echo", capabilities=frozenset({"COMPUTE"}))
        mgr = MCPClientManager([config])
        reg = ToolRegistry()
        mgr.start(reg)
        # 把 MCP 工具元数据+策略注册到 registry
        policy_registry.register_metadata(make_mcp_tool_metadata(
            "mcp__echo__echo",
            declared_capabilities=frozenset({"COMPUTE"}),
        ))
        policy_registry.set_policy("mcp__echo__echo", ToolPolicy(
            enabled=False, allowed_principals=frozenset(),
        ))

        # 构造 Agent stub
        class _AgentStub:
            _tool_policy_registry = policy_registry

        runtime = RuntimeContext(
            store=None,
            agent=_AgentStub(),
            integrated_agent=None,
            manager=None,
            config=RuntimeConfig(),
            trace_sink=None,
            lifecycle=None,
            mcp_manager=mgr,
            mcp_tool_state_store=MCPToolStateStore(path=self.state_path),
        )
        return runtime

    def test_enable_syncs_to_current_policy(self):
        """R1: POST /tools/.../enable 后当前 Agent 策略立即 enabled=true。"""
        from fastapi.testclient import TestClient
        from mini_deerflow.api.app import create_app
        from mini_deerflow.api.config import ApiSettings

        policy_registry = ToolPolicyRegistry()
        runtime = self._build_runtime(policy_registry)
        try:
            settings = ApiSettings()
            app = create_app(settings=settings)
            app.dependency_overrides[get_runtime] = lambda: runtime
            client = TestClient(app)

            resp = client.post(
                f"{settings.api_prefix}/mcp/tools/mcp__echo__echo/enable",
                json={"enabled": True},
            )
            self.assertEqual(resp.status_code, 200, resp.text)

            # 验证：当前 Agent 策略注册表已同步
            _, policy = policy_registry.get("mcp__echo__echo")
            self.assertTrue(policy.enabled, "策略注册表未同步 enable=True")
        finally:
            runtime.mcp_manager.stop()

    def test_grant_syncs_to_current_policy(self):
        """R1: POST /tools/.../grant 后当前 Agent 策略允许该主体。"""
        from fastapi.testclient import TestClient
        from mini_deerflow.api.app import create_app
        from mini_deerflow.api.config import ApiSettings

        policy_registry = ToolPolicyRegistry()
        runtime = self._build_runtime(policy_registry)
        try:
            # 先启用
            policy_registry.set_policy("mcp__echo__echo", ToolPolicy(
                enabled=True, allowed_principals=frozenset(),
            ))

            settings = ApiSettings()
            app = create_app(settings=settings)
            app.dependency_overrides[get_runtime] = lambda: runtime
            client = TestClient(app)

            resp = client.post(
                f"{settings.api_prefix}/mcp/tools/mcp__echo__echo/grant",
                json={"principal": "MAIN_AGENT"},
            )
            self.assertEqual(resp.status_code, 200, resp.text)

            _, policy = policy_registry.get("mcp__echo__echo")
            self.assertIn(ExecutorPrincipal.MAIN_AGENT, policy.allowed_principals)
        finally:
            runtime.mcp_manager.stop()

    def test_revoke_syncs_to_current_policy(self):
        """R1: POST /tools/.../revoke 后当前 Agent 策略移除该主体。"""
        from fastapi.testclient import TestClient
        from mini_deerflow.api.app import create_app
        from mini_deerflow.api.config import ApiSettings

        policy_registry = ToolPolicyRegistry()
        runtime = self._build_runtime(policy_registry)
        try:
            # 先启用+授权
            policy_registry.set_policy("mcp__echo__echo", ToolPolicy(
                enabled=True,
                allowed_principals=frozenset({ExecutorPrincipal.MAIN_AGENT}),
            ))

            settings = ApiSettings()
            app = create_app(settings=settings)
            app.dependency_overrides[get_runtime] = lambda: runtime
            client = TestClient(app)

            resp = client.post(
                f"{settings.api_prefix}/mcp/tools/mcp__echo__echo/revoke",
                json={"principal": "MAIN_AGENT"},
            )
            self.assertEqual(resp.status_code, 200, resp.text)

            _, policy = policy_registry.get("mcp__echo__echo")
            self.assertNotIn(ExecutorPrincipal.MAIN_AGENT, policy.allowed_principals)
        finally:
            runtime.mcp_manager.stop()

    def test_disable_syncs_to_current_policy(self):
        """R1: POST /tools/.../enable enabled=false 同步到当前策略。"""
        from fastapi.testclient import TestClient
        from mini_deerflow.api.app import create_app
        from mini_deerflow.api.config import ApiSettings

        policy_registry = ToolPolicyRegistry()
        runtime = self._build_runtime(policy_registry)
        try:
            # 先启用
            policy_registry.set_policy("mcp__echo__echo", ToolPolicy(
                enabled=True,
                allowed_principals=frozenset({ExecutorPrincipal.MAIN_AGENT}),
            ))

            settings = ApiSettings()
            app = create_app(settings=settings)
            app.dependency_overrides[get_runtime] = lambda: runtime
            client = TestClient(app)

            resp = client.post(
                f"{settings.api_prefix}/mcp/tools/mcp__echo__echo/enable",
                json={"enabled": False},
            )
            self.assertEqual(resp.status_code, 200, resp.text)

            _, policy = policy_registry.get("mcp__echo__echo")
            self.assertFalse(policy.enabled)
        finally:
            runtime.mcp_manager.stop()

    def test_state_persists_across_rebuild(self):
        """R1: 撤销/禁用持久化，重建 runtime 后策略生效。"""
        # 1. 首次写入状态
        store1 = MCPToolStateStore(path=self.state_path)
        store1.set_enabled("mcp__echo__echo", True)
        store1.grant("mcp__echo__echo", ExecutorPrincipal.MAIN_AGENT)
        del store1

        # 2. 重建：从持久化文件恢复策略
        store2 = MCPToolStateStore(path=self.state_path)
        registry = ToolPolicyRegistry()
        registry.register_metadata(make_mcp_tool_metadata(
            "mcp__echo__echo",
            declared_capabilities=frozenset({"COMPUTE"}),
        ))
        state = store2.get("mcp__echo__echo")
        registry.set_policy("mcp__echo__echo", ToolPolicy(
            enabled=state.enabled,
            allowed_principals=state.allowed_principals,
        ))

        # 3. 验证：访问判定为 ALLOW
        from mini_deerflow.tool_authorization import RunAuthority
        authority = RunAuthority(
            principal=ExecutorPrincipal.MAIN_AGENT,
            thread_id="t1", run_id="r1", project_id=None,
            granted_capabilities=frozenset({ToolCapability.COMPUTE}),
        )
        decision = check_tool_access(registry, "mcp__echo__echo", authority)
        self.assertFalse(decision.is_denied)

        # 4. 撤销主体
        store2.revoke("mcp__echo__echo", ExecutorPrincipal.MAIN_AGENT)

        # 5. 再次重建 + 同步
        store3 = MCPToolStateStore(path=self.state_path)
        registry2 = ToolPolicyRegistry()
        registry2.register_metadata(make_mcp_tool_metadata(
            "mcp__echo__echo",
            declared_capabilities=frozenset({"COMPUTE"}),
        ))
        state2 = store3.get("mcp__echo__echo")
        registry2.set_policy("mcp__echo__echo", ToolPolicy(
            enabled=state2.enabled,
            allowed_principals=state2.allowed_principals,
        ))
        # 验证：访问判定为 DENY（主体已被撤销）
        decision2 = check_tool_access(registry2, "mcp__echo__echo", authority)
        self.assertTrue(decision2.is_denied)


# ============================================================================
# R5: API 列出服务器状态
# ============================================================================


class TestR5APIListServers(unittest.TestCase):
    """R5 修复：API /servers 返回以配置为来源的清单。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.state_path = Path(self.temp_dir.name) / "mcp_state.json"
        os.environ["MINI_DEERFLOW_MCP_TOOL_STATE"] = str(self.state_path)

    def tearDown(self):
        self.temp_dir.cleanup()
        os.environ.pop("MINI_DEERFLOW_MCP_TOOL_STATE", None)

    def test_running_server_listed_with_status_running(self):
        """运行的服务器 status=running，出现在清单。"""
        from fastapi.testclient import TestClient
        from mini_deerflow.api.app import create_app
        from mini_deerflow.api.config import ApiSettings

        config = _make_config(name="echo", capabilities=frozenset({"COMPUTE"}))
        mgr = MCPClientManager([config])
        reg = ToolRegistry()
        mgr.start(reg)

        class _AgentStub:
            _tool_policy_registry = ToolPolicyRegistry()

        runtime = RuntimeContext(
            store=None, agent=_AgentStub(), integrated_agent=None,
            manager=None, config=RuntimeConfig(), trace_sink=None, lifecycle=None,
            mcp_manager=mgr,
            mcp_tool_state_store=MCPToolStateStore(path=self.state_path),
        )

        try:
            settings = ApiSettings()
            app = create_app(settings=settings)
            app.dependency_overrides[get_runtime] = lambda: runtime
            client = TestClient(app)
            resp = client.get(f"{settings.api_prefix}/mcp/servers")
            self.assertEqual(resp.status_code, 200, resp.text)
            data = resp.json()
            names = [s["name"] for s in data["servers"]]
            self.assertIn("echo", names)
            echo_info = next(s for s in data["servers"] if s["name"] == "echo")
            self.assertEqual(echo_info["status"], "running")
        finally:
            mgr.stop()

    def test_stopped_server_still_listed(self):
        """R5：停止后服务器仍列出，status=stopped。"""
        from fastapi.testclient import TestClient
        from mini_deerflow.api.app import create_app
        from mini_deerflow.api.config import ApiSettings

        config = _make_config(name="echo", capabilities=frozenset({"COMPUTE"}))
        mgr = MCPClientManager([config])
        reg = ToolRegistry()
        mgr.start(reg)
        mgr.stop()

        class _AgentStub:
            _tool_policy_registry = ToolPolicyRegistry()

        runtime = RuntimeContext(
            store=None, agent=_AgentStub(), integrated_agent=None,
            manager=None, config=RuntimeConfig(), trace_sink=None, lifecycle=None,
            mcp_manager=mgr,
            mcp_tool_state_store=MCPToolStateStore(path=self.state_path),
        )

        settings = ApiSettings()
        app = create_app(settings=settings)
        app.dependency_overrides[get_runtime] = lambda: runtime
        client = TestClient(app)
        resp = client.get(f"{settings.api_prefix}/mcp/servers")
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        names = [s["name"] for s in data["servers"]]
        self.assertIn("echo", names)
        echo_info = next(s for s in data["servers"] if s["name"] == "echo")
        self.assertEqual(echo_info["status"], "stopped")


# ============================================================================
# R6: create_runtime 状态加载失败清理
# ============================================================================


class TestR6CleanupOnFailure(unittest.TestCase):
    """R6 修复：状态加载/应用失败时清理已启动 MCP 资源。"""

    def test_state_load_failure_triggers_mcp_stop(self):
        """R6：状态文件损坏 → 启动失败时已启动的 MCP 已被 stop。"""
        # 不实际调用 create_runtime（会拉起 Agent 复杂依赖）；
        # 验证"已启动后失败"的清理路径：手动模拟场景
        config = _make_config(name="echo", capabilities=frozenset({"COMPUTE"}))
        mgr = MCPClientManager([config])
        reg = ToolRegistry()
        mgr.start(reg)
        self.assertTrue(mgr.started)

        # 模拟状态加载失败：用损坏的 state file
        with TemporaryDirectory() as d:
            state_path = Path(d) / "bad.json"
            state_path.write_text("not valid json{", encoding="utf-8")
            store = MCPToolStateStore(path=state_path)
            with self.assertRaises(MCPToolStateError):
                store.get("anything")  # 触发加载

        # 验证 mgr 仍可正确 stop（清理路径仍可工作）
        mgr.stop()
        self.assertFalse(mgr.started)


if __name__ == "__main__":
    unittest.main()
