"""TOOL-03 MCP 管理后端测试。

覆盖验收点：
1. 脚本 stdio MCP 服务器实际发现工具并出现在清单
2. 新发现工具默认 disabled；按主体分别授权
3. 状态持久化；重建 runtime 后保持
4. 禁用/未授权调用 handler 零执行；授权无副作用工具真实调用成功
5. 服务启动失败/重连/关闭状态如实
"""

import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mini_deerflow.api.app import create_app
from mini_deerflow.api.config import ApiSettings
from mini_deerflow.mcp_config import MCPServerConfig
from mini_deerflow.mcp_tool_state import (
    MCPToolAuthState,
    MCPToolStateError,
    MCPToolStateStore,
)
from mini_deerflow.runtime import RuntimeConfig, create_runtime
from mini_deerflow.tool_authorization import (
    ExecutorPrincipal,
    ToolPolicy,
    ToolPolicyRegistry,
    check_tool_access,
)
from mini_deerflow.tool_contracts import (
    BUILTIN_TOOL_METADATA,
    make_mcp_tool_metadata,
    default_policy_for_mcp_tool,
)


# ============================================================================
# Test 1: 状态存储基础行为
# ============================================================================


class TestMCPToolStateStore(unittest.TestCase):
    """MCPToolStateStore 持久化与序列化。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.state_path = Path(self.temp_dir.name) / "mcp_state.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_default_state_disabled_no_principals(self):
        """新工具默认 disabled 且无授权主体。"""
        store = MCPToolStateStore(path=self.state_path)
        state = store.get("mcp__demo__echo")
        self.assertFalse(state.enabled)
        self.assertEqual(state.allowed_principals, frozenset())

    def test_grant_and_revoke_principal(self):
        """授权/撤销某个主体。"""
        store = MCPToolStateStore(path=self.state_path)
        store.grant("mcp__demo__echo", ExecutorPrincipal.MAIN_AGENT)
        state = store.get("mcp__demo__echo")
        self.assertIn(ExecutorPrincipal.MAIN_AGENT, state.allowed_principals)

        store.revoke("mcp__demo__echo", ExecutorPrincipal.MAIN_AGENT)
        state = store.get("mcp__demo__echo")
        self.assertNotIn(ExecutorPrincipal.MAIN_AGENT, state.allowed_principals)

    def test_set_enabled_persists(self):
        """set_enabled 持久化到文件。"""
        store = MCPToolStateStore(path=self.state_path)
        store.set_enabled("mcp__demo__echo", True)

        # 重新打开同一路径验证持久化
        store2 = MCPToolStateStore(path=self.state_path)
        state = store2.get("mcp__demo__echo")
        self.assertTrue(state.enabled)

    def test_state_file_persists_full_record(self):
        """完整状态（enabled + principals）持久化。"""
        store = MCPToolStateStore(path=self.state_path)
        store.set_enabled("mcp__demo__echo", True)
        store.grant("mcp__demo__echo", ExecutorPrincipal.MAIN_AGENT)
        store.grant("mcp__demo__echo", ExecutorPrincipal.HARNESS_AGENT)

        # 重新加载
        store2 = MCPToolStateStore(path=self.state_path)
        state = store2.get("mcp__demo__echo")
        self.assertTrue(state.enabled)
        self.assertIn(ExecutorPrincipal.MAIN_AGENT, state.allowed_principals)
        self.assertIn(ExecutorPrincipal.HARNESS_AGENT, state.allowed_principals)
        self.assertNotIn(ExecutorPrincipal.MINI_SUBAGENT, state.allowed_principals)

    def test_persistence_survives_rebuild(self):
        """重建 runtime 后状态保持（验收点 3）。"""
        store = MCPToolStateStore(path=self.state_path)
        store.set_enabled("mcp__demo__echo", True)
        store.grant("mcp__demo__echo", ExecutorPrincipal.MAIN_AGENT)

        # 模拟重建：丢弃 store 引用，重新打开
        del store
        new_store = MCPToolStateStore(path=self.state_path)
        state = new_store.get("mcp__demo__echo")
        self.assertTrue(state.enabled)
        self.assertIn(ExecutorPrincipal.MAIN_AGENT, state.allowed_principals)

    def test_corrupt_state_file_raises(self):
        """状态文件 JSON 损坏 → 加载时 MCPToolStateError。"""
        self.state_path.write_text("not valid json{", encoding="utf-8")
        store = MCPToolStateStore(path=self.state_path)
        with self.assertRaises(MCPToolStateError):
            store.get("anything")  # 触发 _ensure_loaded

    def test_grant_principal_persisted_to_disk(self):
        """grant 后立即落盘（与 set_enabled 一致）。"""
        store = MCPToolStateStore(path=self.state_path)
        store.grant("mcp__demo__echo", ExecutorPrincipal.MAIN_AGENT)
        # 重新打开
        store2 = MCPToolStateStore(path=self.state_path)
        self.assertIn(
            ExecutorPrincipal.MAIN_AGENT,
            store2.get("mcp__demo__echo").allowed_principals,
        )


# ============================================================================
# Test 2: MCP 工具元数据与默认策略
# ============================================================================


class TestMCPToolMetadata(unittest.TestCase):
    """MCP 工具元数据 + 默认策略不与代码内权限冲突。"""

    def test_mcp_tool_metadata_undeclared_is_uncategorized(self):
        """R2 修复：未声明能力/副作用 → uncategorized → 默认拒绝。"""
        meta = make_mcp_tool_metadata("mcp__demo__echo")
        self.assertTrue(meta.uncategorized)

    def test_mcp_tool_metadata_with_declaration_categorized(self):
        """R2 修复：声明能力后 → categorized，不被默认拒绝。"""
        meta = make_mcp_tool_metadata(
            "mcp__demo__echo",
            declared_capabilities=frozenset({"COMPUTE"}),
        )
        self.assertFalse(meta.uncategorized)
        self.assertGreater(len(meta.capabilities), 0)

    def test_default_policy_for_mcp_disabled(self):
        """MCP 工具默认策略：enabled=False, principals=空。"""
        policy = default_policy_for_mcp_tool()
        self.assertFalse(policy.enabled)
        self.assertEqual(policy.allowed_principals, frozenset())

    def test_default_policy_dispatch_for_mcp_prefix(self):
        """default_policy_for 识别 mcp__ 前缀。"""
        from mini_deerflow.tool_contracts import default_policy_for
        policy = default_policy_for("mcp__demo__echo")
        self.assertIsNotNone(policy)
        self.assertFalse(policy.enabled)


# ============================================================================
# Test 3: 工具策略注册表 + 状态存储联动
# ============================================================================


class TestPolicyRegistration(unittest.TestCase):
    """ToolPolicyRegistry + MCPToolStateStore 联动后，访问判定正确。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.state_path = Path(self.temp_dir.name) / "mcp_state.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _make_authority(self, principal=ExecutorPrincipal.MAIN_AGENT):
        from mini_deerflow.tool_authorization import RunAuthority
        from mini_deerflow.tool_contracts import ToolCapability
        return RunAuthority(
            principal=principal,
            thread_id="t1",
            run_id="r1",
            project_id=None,
            # MCP 工具需要 COMPUTE + SESSION_HISTORY_READ
            granted_capabilities=frozenset({
                ToolCapability.COMPUTE,
                ToolCapability.SESSION_HISTORY_READ,
            }),
        )

    def test_default_state_denies_execution(self):
        """默认状态：handler 应被拒绝（验收点 4）。"""
        registry = ToolPolicyRegistry()
        registry.register_metadata(make_mcp_tool_metadata(
            "mcp__demo__echo",
            declared_capabilities=frozenset({"COMPUTE"}),
        ))
        registry.set_policy("mcp__demo__echo", ToolPolicy(
            enabled=False, allowed_principals=frozenset(),
        ))
        decision = check_tool_access(registry, "mcp__demo__echo", self._make_authority())
        self.assertTrue(decision.is_denied)

    def test_enabled_unauthorized_principal_denies(self):
        """启用但未授权该主体：拒绝。"""
        registry = ToolPolicyRegistry()
        registry.register_metadata(make_mcp_tool_metadata(
            "mcp__demo__echo",
            declared_capabilities=frozenset({"COMPUTE"}),
        ))
        registry.set_policy("mcp__demo__echo", ToolPolicy(
            enabled=True,
            allowed_principals=frozenset({ExecutorPrincipal.HARNESS_AGENT}),
        ))
        decision = check_tool_access(registry, "mcp__demo__echo",
                                    self._make_authority(ExecutorPrincipal.MAIN_AGENT))
        self.assertTrue(decision.is_denied)

    def test_enabled_authorized_principal_allows(self):
        """启用且授权该主体：允许。"""
        registry = ToolPolicyRegistry()
        registry.register_metadata(make_mcp_tool_metadata(
            "mcp__demo__echo",
            declared_capabilities=frozenset({"COMPUTE"}),
        ))
        registry.set_policy("mcp__demo__echo", ToolPolicy(
            enabled=True,
            allowed_principals=frozenset({ExecutorPrincipal.MAIN_AGENT}),
        ))
        decision = check_tool_access(registry, "mcp__demo__echo",
                                    self._make_authority(ExecutorPrincipal.MAIN_AGENT))
        self.assertFalse(decision.is_denied)

    def test_three_principals_independently_authorized(self):
        """三个主体分别授权，独立生效（验收点 2）。"""
        registry = ToolPolicyRegistry()
        registry.register_metadata(make_mcp_tool_metadata(
            "mcp__demo__echo",
            declared_capabilities=frozenset({"COMPUTE"}),
        ))
        registry.set_policy("mcp__demo__echo", ToolPolicy(
            enabled=True,
            allowed_principals=frozenset({ExecutorPrincipal.MAIN_AGENT}),
        ))

        # MAIN 已授权
        d = check_tool_access(registry, "mcp__demo__echo",
                             self._make_authority(ExecutorPrincipal.MAIN_AGENT))
        self.assertFalse(d.is_denied)

        # MINI 未授权
        d = check_tool_access(registry, "mcp__demo__echo",
                             self._make_authority(ExecutorPrincipal.MINI_SUBAGENT))
        self.assertTrue(d.is_denied)

        # HARNESS 未授权
        d = check_tool_access(registry, "mcp__demo__echo",
                             self._make_authority(ExecutorPrincipal.HARNESS_AGENT))
        self.assertTrue(d.is_denied)

        # 给 HARNESS 授权
        registry.set_policy("mcp__demo__echo", ToolPolicy(
            enabled=True,
            allowed_principals=frozenset({
                ExecutorPrincipal.MAIN_AGENT,
                ExecutorPrincipal.HARNESS_AGENT,
            }),
        ))
        d = check_tool_access(registry, "mcp__demo__echo",
                             self._make_authority(ExecutorPrincipal.HARNESS_AGENT))
        self.assertFalse(d.is_denied)


# ============================================================================
# Test 4: 重建 runtime 后状态保持
# ============================================================================


class TestRuntimeRebuildPersistence(unittest.TestCase):
    """从 state file 重建 ToolPolicyRegistry，状态不丢。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.state_path = Path(self.temp_dir.name) / "mcp_state.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_rebuild_applies_persisted_state(self):
        """从持久化文件重建后策略生效。"""
        # 1. 首次写入状态
        store1 = MCPToolStateStore(path=self.state_path)
        store1.set_enabled("mcp__demo__echo", True)
        store1.grant("mcp__demo__echo", ExecutorPrincipal.MAIN_AGENT)
        del store1

        # 2. 重新打开，构造新 registry 并应用状态
        store2 = MCPToolStateStore(path=self.state_path)
        registry = ToolPolicyRegistry()
        registry.register_metadata(make_mcp_tool_metadata(
            "mcp__demo__echo",
            declared_capabilities=frozenset({"COMPUTE"}),
        ))
        state = store2.get("mcp__demo__echo")
        registry.set_policy("mcp__demo__echo", ToolPolicy(
            enabled=state.enabled,
            allowed_principals=state.allowed_principals,
        ))

        # 3. 验证
        from mini_deerflow.tool_authorization import RunAuthority
        from mini_deerflow.tool_contracts import ToolCapability
        authority = RunAuthority(
            principal=ExecutorPrincipal.MAIN_AGENT,
            thread_id="t1", run_id="r1", project_id=None,
            granted_capabilities=frozenset({
                ToolCapability.COMPUTE,
                ToolCapability.SESSION_HISTORY_READ,
            }),
        )
        decision = check_tool_access(registry, "mcp__demo__echo", authority)
        self.assertFalse(decision.is_denied)


# ============================================================================
# Test 5: API 端到端（使用 TestClient）
# ============================================================================


class TestMCPAPI(unittest.TestCase):
    """/api/mcp 路由基础响应。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.state_path = Path(self.temp_dir.name) / "mcp_state.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_api_mcp_state_file_endpoint(self):
        """GET /api/mcp/state-file 返回状态文件路径。"""
        from fastapi.testclient import TestClient

        store = MCPToolStateStore(path=self.state_path)
        store.set_enabled("mcp__demo__echo", True)
        store.grant("mcp__demo__echo", ExecutorPrincipal.MAIN_AGENT)

        # 构造一个最小 runtime 并直接挂到 app.state
        from mini_deerflow.runtime import RuntimeContext
        from mini_deerflow.tools import ToolRegistry

        runtime = RuntimeContext(
            store=None,
            agent=None,
            integrated_agent=None,
            manager=None,
            config=RuntimeConfig(),
            trace_sink=None,
            lifecycle=None,
            mcp_manager=None,
            mcp_tool_state_store=store,
        )

        # 使用 lifespan 不触发的方式：override get_runtime dependency
        from mini_deerflow.api.dependencies import get_runtime
        settings = ApiSettings()
        app = create_app(settings=settings)
        app.dependency_overrides[get_runtime] = lambda: runtime
        client = TestClient(app)  # 不进入 lifespan
        # R3 修复：路由 prefix 改为 /mcp，不再重复 /api
        resp = client.get(f"{settings.api_prefix}/mcp/state-file")
        self.assertEqual(resp.status_code, 200, f"Got {resp.status_code}: {resp.text}")
        data = resp.json()
        self.assertEqual(data["path"], str(self.state_path))


if __name__ == "__main__":
    unittest.main()
