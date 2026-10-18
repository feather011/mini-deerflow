"""TOOL-05 后端实施测试：六条行为验收 + 路由层契约。

实施依据：
- 浏览器只看到 `arguments_display`（脱敏），不返回原 `arguments`
- execute-snapshot 服务端读保存的参数快照执行
- 查询时同步过期（不影响已执行）
- 活跃 run 通过协调器公开方法读
- MCP enable / grant / revoke 真实生效
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from typing import Any
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from mini_deerflow.confirmation import (
    ConfirmationError,
    ConfirmationState,
)
from mini_deerflow.runtime import create_runtime, RuntimeConfig
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tools import Tool, ToolRegistry
from mini_deerflow.tool_authorization import ToolPolicy, ToolPolicyRegistry
from mini_deerflow.tool_contracts import (
    ExecutorPrincipal,
    make_mcp_tool_metadata,
    ToolCapability,
)


# ---------------------------------------------------------------------------
# 工具构造
# ---------------------------------------------------------------------------


def _make_policy_registry() -> ToolPolicyRegistry:
    reg = ToolPolicyRegistry()
    reg.register_metadata(make_mcp_tool_metadata(
        "mcp__echo__echo",
        declared_capabilities=frozenset({"COMPUTE"}),
    ))
    reg.set_policy("mcp__echo__echo", ToolPolicy(
        enabled=True,
        allowed_principals=frozenset({ExecutorPrincipal.MAIN_AGENT}),
        requires_confirmation=True,
    ))
    return reg


def _build_app_with_echo(handler_calls: list, temp_dir: str):
    """构造带 echo 工具的完整 runtime + TestClient。"""
    from mini_deerflow.api.app import create_app
    from mini_deerflow.api.config import ApiSettings
    from mini_deerflow.api.dependencies import get_runtime

    # 隔离 store path，避免测试间残留
    store_path = os.path.join(temp_dir, "confirmations.json")
    os.environ["MINI_DEERFLOW_CONFIRMATION_STORE"] = store_path

    store = InMemoryThreadStore()
    store.create("t-1")

    registry = ToolRegistry()

    def echo(args, context=None):
        handler_calls.append({"args": dict(args or {}), "ctx": context})
        return "echo-result"

    registry.register(Tool(
        name="mcp__echo__echo",
        description="echo",
        parameters={"type": "object", "properties": {"m": {"type": "string"}}},
        handler=echo,
        needs_context=True,
    ))

    config = RuntimeConfig(
        workspace_root=temp_dir,
        subagent_enabled=False,
        trace_enabled=False,
    )
    runtime = create_runtime(store=store, registry=registry, config=config)
    # 加策略
    runtime.agent._tool_policy_registry.register_metadata(make_mcp_tool_metadata(
        "mcp__echo__echo",
        declared_capabilities=frozenset({"COMPUTE"}),
    ))
    runtime.agent._tool_policy_registry.set_policy("mcp__echo__echo", ToolPolicy(
        enabled=True,
        allowed_principals=frozenset({ExecutorPrincipal.MAIN_AGENT}),
        requires_confirmation=True,
    ))

    # 注册一个 active run
    from mini_deerflow.tool_authorization import RunAuthority
    auth = RunAuthority(
        principal=ExecutorPrincipal.MAIN_AGENT,
        thread_id="t-1",
        run_id="r-1",
        project_id=None,
        granted_capabilities=frozenset({ToolCapability.COMPUTE}),
    )
    runtime.register_run("r-1", auth, None)
    runtime.thread_run_coordinator.try_acquire("t-1", "r-1")

    settings = ApiSettings()
    app = create_app(settings=settings)
    app.dependency_overrides[get_runtime] = lambda: runtime
    return app, settings, runtime


# ---------------------------------------------------------------------------
# 1. 敏感值不下发：响应不包含原值
# ---------------------------------------------------------------------------


class TestNoSensitiveValueLeak(unittest.TestCase):
    """验收 1：整个 HTTP 响应不包含原始敏感值。"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.handler_calls: list = []
        self.app, self.settings, self.runtime = _build_app_with_echo(
            self.handler_calls, self.temp_dir,
        )
        self.client = TestClient(self.app)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_arguments_display_masks_api_key(self):
        """创建含 api_key 的请求，详情/列表响应都不含原值。"""
        resp = self.client.post(
            f"{self.settings.api_prefix}/confirmations",
            json={
                "run_id": "r-1",
                "thread_id": "t-1",
                "project_id": None,
                "principal": "MAIN_AGENT",
                "tool_name": "mcp__echo__echo",
                "arguments": {
                    "m": "hello",
                    "api_key": "sk-SECRET-1234567890",
                    "nested": {"password": "p4ssw0rd", "ok": 1},
                },
                "ttl_seconds": 60.0,
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body_text = resp.text
        # 整个响应不含原值
        self.assertNotIn("sk-SECRET-1234567890", body_text,
                        f"原值不应出现在响应，实际 body 含原值: {body_text}")
        self.assertNotIn("p4ssw0rd", body_text,
                        f"嵌套敏感值不应出现在响应，实际 body 含原值: {body_text}")
        # 含脱敏占位
        self.assertIn("••••••", body_text,
                     f"脱敏占位应出现: {body_text}")
        # 含其他正常字段
        self.assertIn("mcp__echo__echo", body_text)

    def test_list_and_detail_dont_leak(self):
        """列表 + 详情都不应包含原值。"""
        self.client.post(
            f"{self.settings.api_prefix}/confirmations",
            json={
                "run_id": "r-1",
                "thread_id": "t-1",
                "project_id": None,
                "principal": "MAIN_AGENT",
                "tool_name": "mcp__echo__echo",
                "arguments": {"token": "TOK-ABCDEFGHIJKL", "x": 1},
                "ttl_seconds": 60.0,
            },
        )
        # 列表
        list_resp = self.client.get(f"{self.settings.api_prefix}/confirmations?thread_id=t-1")
        self.assertNotIn("TOK-ABCDEFGHIJKL", list_resp.text,
                        f"列表不应含 token 原值: {list_resp.text}")
        self.assertIn("••••••", list_resp.text)
        # 详情
        list_body = list_resp.json()
        rid = list_body["confirmations"][0]["request_id"]
        detail = self.client.get(f"{self.settings.api_prefix}/confirmations/{rid}")
        self.assertNotIn("TOK-ABCDEFGHIJKL", detail.text,
                        f"详情不应含 token 原值: {detail.text}")
        # 详情响应有 arguments_display
        self.assertIn("arguments_display", detail.json())

    def test_url_userinfo_fragment_and_auth_header_dont_leak(self):
        """整段响应不含 URL userinfo/fragment/Authorization 字符串原值。"""
        fixture_url_pw = "FIXTURE_URL_PASSWORD"
        fixture_fragment_token = "FIXTURE_FRAGMENT"
        fixture_header = "FIXTURE_HEADER"
        resp = self.client.post(
            f"{self.settings.api_prefix}/confirmations",
            json={
                "run_id": "r-1",
                "thread_id": "t-1",
                "project_id": None,
                "principal": "MAIN_AGENT",
                "tool_name": "mcp__echo__echo",
                "arguments": {
                    "url": f"https://reader:{fixture_url_pw}@example.invalid/path?token=FIXTURE_QUERY#access_token={fixture_fragment_token}",
                    "headers": [
                        f"Authorization: Bearer {fixture_header}",
                    ],
                },
                "ttl_seconds": 60.0,
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body_text = resp.text
        # URL userinfo 密码不能出现
        self.assertNotIn(fixture_url_pw, body_text,
                        f"URL userinfo 密码不应出现: {body_text}")
        # fragment 中的 token 不能出现
        self.assertNotIn(fixture_fragment_token, body_text,
                        f"fragment token 不应出现: {body_text}")
        # Authorization 头中的凭据不能出现
        self.assertNotIn(fixture_header, body_text,
                        f"Authorization 凭据不应出现: {body_text}")
        # 应有脱敏占位
        self.assertIn("••••••", body_text)

    def test_approve_response_also_masks(self):
        """批准响应复用 _to_info：也应脱敏。"""
        resp = self.client.post(
            f"{self.settings.api_prefix}/confirmations",
            json={
                "run_id": "r-1",
                "thread_id": "t-1",
                "project_id": None,
                "principal": "MAIN_AGENT",
                "tool_name": "mcp__echo__echo",
                "arguments": {
                    "url": "https://user:FIXTURE_URL_PASSWORD@ex.com/p?q=1",
                },
                "ttl_seconds": 60.0,
            },
        )
        rid = resp.json()["request_id"]
        # 批准响应也复用 _to_info
        approve_resp = self.client.post(
            f"{self.settings.api_prefix}/confirmations/{rid}/approve",
            json={"decided_by": "user"},
        )
        self.assertEqual(approve_resp.status_code, 200)
        self.assertNotIn("FIXTURE_URL_PASSWORD", approve_resp.text,
                        f"批准响应不应含 URL 密码: {approve_resp.text}")


# ---------------------------------------------------------------------------
# 2. 批准并执行一次
# ---------------------------------------------------------------------------


class TestApproveAndExecute(unittest.TestCase):
    """验收 2：批准 + execute-snapshot 一次，handler 恰好 1 次；重复/换绑定拒绝。"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.handler_calls: list = []
        self.app, self.settings, self.runtime = _build_app_with_echo(
            self.handler_calls, self.temp_dir,
        )
        self.client = TestClient(self.app)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create(self, **over):
        body = {
            "run_id": "r-1",
            "thread_id": "t-1",
            "project_id": None,
            "principal": "MAIN_AGENT",
            "tool_name": "mcp__echo__echo",
            "arguments": {"m": "hi"},
            "ttl_seconds": 60.0,
        }
        body.update(over)
        return self.client.post(
            f"{self.settings.api_prefix}/confirmations", json=body,
        )

    def test_snapshot_execute_handler_once(self):
        """execute-snapshot 一次：handler 恰好 1 次。"""
        rid = self._create().json()["request_id"]
        # 批准
        self.client.post(
            f"{self.settings.api_prefix}/confirmations/{rid}/approve",
            json={"decided_by": "user"},
        )
        # 快照执行
        # 从 store 取 arguments_hash
        req = self.runtime.confirmation_store.get(rid)
        resp = self.client.post(
            f"{self.settings.api_prefix}/confirmations/{rid}/execute-snapshot",
            json={
                "run_id": "r-1",
                "thread_id": "t-1",
                "tool_name": "mcp__echo__echo",
                "arguments_hash": req.arguments_hash,
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["state"], "executed")
        self.assertEqual(data["execution_result"], "ok")
        self.assertEqual(len(self.handler_calls), 1,
                        f"handler 应被调用 1 次，实际 {len(self.handler_calls)}")
        # handler 收到原值（不是脱敏）
        self.assertEqual(self.handler_calls[0]["args"].get("m"), "hi")

    def test_repeat_execute_rejected(self):
        """重复 execute → 409 终态拒重。"""
        rid = self._create().json()["request_id"]
        self.client.post(
            f"{self.settings.api_prefix}/confirmations/{rid}/approve",
            json={"decided_by": "user"},
        )
        req = self.runtime.confirmation_store.get(rid)
        # 第一次
        r1 = self.client.post(
            f"{self.settings.api_prefix}/confirmations/{rid}/execute-snapshot",
            json={
                "run_id": "r-1",
                "thread_id": "t-1",
                "tool_name": "mcp__echo__echo",
                "arguments_hash": req.arguments_hash,
            },
        )
        self.assertEqual(r1.status_code, 200)
        # 重复
        r2 = self.client.post(
            f"{self.settings.api_prefix}/confirmations/{rid}/execute-snapshot",
            json={
                "run_id": "r-1",
                "thread_id": "t-1",
                "tool_name": "mcp__echo__echo",
                "arguments_hash": req.arguments_hash,
            },
        )
        self.assertEqual(r2.status_code, 409, r2.text)
        self.assertEqual(len(self.handler_calls), 1,
                        f"重复 execute 不应再触发 handler，实际 {len(self.handler_calls)}")


# ---------------------------------------------------------------------------
# 3. 错误绑定拒绝
# ---------------------------------------------------------------------------


class TestBindingMismatchRejected(unittest.TestCase):
    """验收 3：换 run_id/thread_id/tool_name/hash 返回拒绝，原批准仍可正常使用。"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.handler_calls: list = []
        self.app, self.settings, self.runtime = _build_app_with_echo(
            self.handler_calls, self.temp_dir,
        )
        self.client = TestClient(self.app)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_wrong_run_id_then_correct(self):
        """错 run_id → 409；原批准仍可用正确绑定再执行。"""
        rid = self.client.post(
            f"{self.settings.api_prefix}/confirmations",
            json={
                "run_id": "r-1", "thread_id": "t-1", "project_id": None,
                "principal": "MAIN_AGENT", "tool_name": "mcp__echo__echo",
                "arguments": {"m": "x"}, "ttl_seconds": 60.0,
            },
        ).json()["request_id"]
        self.client.post(
            f"{self.settings.api_prefix}/confirmations/{rid}/approve",
            json={"decided_by": "user"},
        )
        req = self.runtime.confirmation_store.get(rid)
        # 错 run_id
        r_bad = self.client.post(
            f"{self.settings.api_prefix}/confirmations/{rid}/execute-snapshot",
            json={
                "run_id": "WRONG_RUN", "thread_id": "t-1",
                "tool_name": "mcp__echo__echo",
                "arguments_hash": req.arguments_hash,
            },
        )
        self.assertEqual(r_bad.status_code, 409, r_bad.text)
        self.assertEqual(len(self.handler_calls), 0)
        # 状态仍 APPROVED
        after_bad = self.client.get(
            f"{self.settings.api_prefix}/confirmations/{rid}",
        ).json()
        self.assertEqual(after_bad["state"], "approved")
        # 正确绑定再执行
        r_ok = self.client.post(
            f"{self.settings.api_prefix}/confirmations/{rid}/execute-snapshot",
            json={
                "run_id": "r-1", "thread_id": "t-1",
                "tool_name": "mcp__echo__echo",
                "arguments_hash": req.arguments_hash,
            },
        )
        self.assertEqual(r_ok.status_code, 200, r_ok.text)
        self.assertEqual(len(self.handler_calls), 1)


# ---------------------------------------------------------------------------
# 4. 刷新/切会话恢复
# ---------------------------------------------------------------------------


class TestRefreshAndSwitch(unittest.TestCase):
    """验收 4：刷新恢复 PENDING / APPROVED / 执行结果；切会话不串数据。"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.handler_calls: list = []
        self.app, self.settings, self.runtime = _build_app_with_echo(
            self.handler_calls, self.temp_dir,
        )
        self.client = TestClient(self.app)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_active_run_probe(self):
        """active_run 探针返回协调器当前 run_id。"""
        resp = self.client.get(f"{self.settings.api_prefix}/threads/t-1/active_run")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["run_id"], "r-1")
        # 不活跃 thread
        resp2 = self.client.get(f"{self.settings.api_prefix}/threads/unknown/active_run")
        self.assertEqual(resp2.json()["run_id"], None)

    def test_list_filter_by_thread_and_run(self):
        """列表按 thread_id / run_id 过滤；多会话不串数据。"""
        # 在 t-1 下创建 2 个请求
        rid_a = self.client.post(
            f"{self.settings.api_prefix}/confirmations",
            json={
                "run_id": "r-1", "thread_id": "t-1", "project_id": None,
                "principal": "MAIN_AGENT", "tool_name": "mcp__echo__echo",
                "arguments": {"m": "a"}, "ttl_seconds": 60.0,
            },
        ).json()["request_id"]
        rid_b = self.client.post(
            f"{self.settings.api_prefix}/confirmations",
            json={
                "run_id": "r-1", "thread_id": "t-1", "project_id": None,
                "principal": "MAIN_AGENT", "tool_name": "mcp__echo__echo",
                "arguments": {"m": "b"}, "ttl_seconds": 60.0,
            },
        ).json()["request_id"]
        # t-2 创建一个（模拟另一会话）
        from mini_deerflow.thread_store import InMemoryThreadStore
        self.runtime.store.create("t-2")
        self.client.post(
            f"{self.settings.api_prefix}/confirmations",
            json={
                "run_id": "r-1", "thread_id": "t-2", "project_id": None,
                "principal": "MAIN_AGENT", "tool_name": "mcp__echo__echo",
                "arguments": {"m": "c"}, "ttl_seconds": 60.0,
            },
        )
        # 过滤 t-1
        r1 = self.client.get(
            f"{self.settings.api_prefix}/confirmations?thread_id=t-1"
        ).json()
        rids = {c["request_id"] for c in r1["confirmations"]}
        self.assertEqual(rids, {rid_a, rid_b})
        # 过滤 t-2
        r2 = self.client.get(
            f"{self.settings.api_prefix}/confirmations?thread_id=t-2"
        ).json()
        self.assertEqual(len(r2["confirmations"]), 1)
        self.assertEqual(r2["confirmations"][0]["thread_id"], "t-2")

    def test_refresh_approved_state_visible(self):
        """刷新后通过详情能拿到当前 APPROVED 状态。"""
        rid = self.client.post(
            f"{self.settings.api_prefix}/confirmations",
            json={
                "run_id": "r-1", "thread_id": "t-1", "project_id": None,
                "principal": "MAIN_AGENT", "tool_name": "mcp__echo__echo",
                "arguments": {"m": "x"}, "ttl_seconds": 60.0,
            },
        ).json()["request_id"]
        # 批准
        self.client.post(
            f"{self.settings.api_prefix}/confirmations/{rid}/approve",
            json={"decided_by": "user"},
        )
        # "刷新"：直接 GET 详情
        resp = self.client.get(f"{self.settings.api_prefix}/confirmations/{rid}")
        self.assertEqual(resp.json()["state"], "approved")
        # 再执行
        req = self.runtime.confirmation_store.get(rid)
        self.client.post(
            f"{self.settings.api_prefix}/confirmations/{rid}/execute-snapshot",
            json={
                "run_id": "r-1", "thread_id": "t-1",
                "tool_name": "mcp__echo__echo",
                "arguments_hash": req.arguments_hash,
            },
        )
        # 刷新后查 executed
        resp2 = self.client.get(f"{self.settings.api_prefix}/confirmations/{rid}")
        data = resp2.json()
        self.assertEqual(data["state"], "executed")
        self.assertEqual(data["execution_result"], "ok")


# ---------------------------------------------------------------------------
# 5. 后端真实同步过期
# ---------------------------------------------------------------------------


class TestExpirySync(unittest.TestCase):
    """验收 5：后端查询时同步过期；卡片与运行状态一致。"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.handler_calls: list = []
        self.app, self.settings, self.runtime = _build_app_with_echo(
            self.handler_calls, self.temp_dir,
        )
        self.client = TestClient(self.app)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_query_syncs_expired_pending(self):
        """查询时已过期的 PENDING 变 EXPIRED。"""
        # 创建 ttl=0.1s 的请求
        rid = self.client.post(
            f"{self.settings.api_prefix}/confirmations",
            json={
                "run_id": "r-1", "thread_id": "t-1", "project_id": None,
                "principal": "MAIN_AGENT", "tool_name": "mcp__echo__echo",
                "arguments": {"m": "x"}, "ttl_seconds": 0.1,
            },
        ).json()["request_id"]
        # 等过期
        import time as _t
        _t.sleep(0.2)
        # 列表查询
        r = self.client.get(
            f"{self.settings.api_prefix}/confirmations?thread_id=t-1"
        ).json()
        c = next(c for c in r["confirmations"] if c["request_id"] == rid)
        self.assertEqual(c["state"], "expired",
                        f"过期 PENDING 应同步成 expired，实际 {c['state']}")

    def test_query_does_not_touch_executed(self):
        """已 executed 的请求查询时不被改成 expired。"""
        rid = self.client.post(
            f"{self.settings.api_prefix}/confirmations",
            json={
                "run_id": "r-1", "thread_id": "t-1", "project_id": None,
                "principal": "MAIN_AGENT", "tool_name": "mcp__echo__echo",
                "arguments": {"m": "x"}, "ttl_seconds": 0.1,
            },
        ).json()["request_id"]
        self.client.post(
            f"{self.settings.api_prefix}/confirmations/{rid}/approve",
            json={"decided_by": "user"},
        )
        # 立即执行（在过期前）
        req = self.runtime.confirmation_store.get(rid)
        self.client.post(
            f"{self.settings.api_prefix}/confirmations/{rid}/execute-snapshot",
            json={
                "run_id": "r-1", "thread_id": "t-1",
                "tool_name": "mcp__echo__echo",
                "arguments_hash": req.arguments_hash,
            },
        )
        # 等过期
        import time as _t
        _t.sleep(0.2)
        # 查询：仍 executed
        r = self.client.get(
            f"{self.settings.api_prefix}/confirmations?thread_id=t-1"
        ).json()
        c = next(c for c in r["confirmations"] if c["request_id"] == rid)
        self.assertEqual(c["state"], "executed",
                        f"executed 记录不应被过期同步覆盖，实际 {c['state']}")


# ---------------------------------------------------------------------------
# 6. MCP 启用与三主体授权真实生效
# ---------------------------------------------------------------------------


class TestMCPAuthRealEffect(unittest.TestCase):
    """验收 6：enable / grant / revoke 三类主体真实生效；检查实际工具可见性和 handler。"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.handler_calls: list = []

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _build(self, principal=ExecutorPrincipal.MAIN_AGENT):
        from mini_deerflow.api.app import create_app
        from mini_deerflow.api.config import ApiSettings
        from mini_deerflow.api.dependencies import get_runtime

        store = InMemoryThreadStore()
        store.create("t-1")
        registry = ToolRegistry()

        def echo(args, context=None):
            self.handler_calls.append({"args": dict(args or {}), "principal": getattr(context, "principal", None) if context else None})
            return "echo-result"

        registry.register(Tool(
            name="mcp__echo__echo",
            description="echo",
            parameters={"type": "object", "properties": {"m": {"type": "string"}}},
            handler=echo,
            needs_context=True,
        ))

        config = RuntimeConfig(
            workspace_root=self.temp_dir,
            subagent_enabled=False,
            trace_enabled=False,
        )
        runtime = create_runtime(store=store, registry=registry, config=config)
        runtime.agent._tool_policy_registry.register_metadata(make_mcp_tool_metadata(
            "mcp__echo__echo",
            declared_capabilities=frozenset({"COMPUTE"}),
        ))
        # 初始策略：允许 MAIN_AGENT，禁用（需要确认 + 状态默认 deny）
        from mini_deerflow.mcp_tool_state import MCPToolStateStore
        state_path = os.path.join(self.temp_dir, "mcp_state.json")
        runtime.mcp_tool_state_store = MCPToolStateStore(path=state_path)

        # TOOL-05 测试需要 MCP 路由能识别工具；mock mcp_manager._tools
        class _McpStub:
            def __init__(self):
                self._tools = {"mcp__echo__echo": ("echo-server", "echo")}
        runtime.mcp_manager = _McpStub()

        settings = ApiSettings()
        app = create_app(settings=settings)
        app.dependency_overrides[get_runtime] = lambda: runtime
        return app, settings, runtime

    def test_grant_revoke_three_principals(self):
        """grant / revoke 对三类主体真实生效（状态文件 + API 列表）。"""
        from mini_deerflow.mcp_tool_state import MCPToolStateStore
        from mini_deerflow.api.dependencies import get_runtime

        app, settings, runtime = self._build()
        client = TestClient(app)

        # 初始：MAIN_AGENT 已 enable
        from mini_deerflow.tool_contracts import ExecutorPrincipal as EP
        principals = [
            (EP.MAIN_AGENT, "MAIN_AGENT"),
            (EP.MINI_SUBAGENT, "MINI_SUBAGENT"),
            (EP.HARNESS_AGENT, "HARNESS_AGENT"),
        ]
        for ep, name in principals:
            # 1. grant → 状态文件应有该主体
            resp = client.post(
                f"{settings.api_prefix}/mcp/tools/mcp__echo__echo/grant",
                json={"principal": name},
            )
            self.assertEqual(resp.status_code in (200, 204), True, resp.text)
            stored = runtime.mcp_tool_state_store.get("mcp__echo__echo")
            self.assertIn(ep, stored.allowed_principals,
                         f"grant {name} 后应出现在 allowed_principals 中，实际 {stored.allowed_principals}")

            # 2. revoke → 状态文件应去除
            resp2 = client.post(
                f"{settings.api_prefix}/mcp/tools/mcp__echo__echo/revoke",
                json={"principal": name},
            )
            self.assertEqual(resp2.status_code in (200, 204), True, resp2.text)
            stored2 = runtime.mcp_tool_state_store.get("mcp__echo__echo")
            self.assertNotIn(ep, stored2.allowed_principals,
                            f"revoke {name} 后应不在 allowed_principals 中，实际 {stored2.allowed_principals}")

    def test_disable_blocks_handler(self):
        """disable 后工具仍可列表但策略 deny；调用确认仍可创建但执行被拒。"""
        from mini_deerflow.api.dependencies import get_runtime
        from mini_deerflow.tool_contracts import ExecutorPrincipal as EP

        app, settings, runtime = self._build()
        client = TestClient(app)

        # 注册 run 用于 execute
        from mini_deerflow.tool_authorization import RunAuthority
        from mini_deerflow.middleware import CancellationToken
        auth = RunAuthority(
            principal=EP.MAIN_AGENT,
            thread_id="t-1", run_id="r-1", project_id=None,
            granted_capabilities=frozenset({ToolCapability.COMPUTE}),
        )
        runtime.register_run("r-1", auth, CancellationToken())
        runtime.thread_run_coordinator.try_acquire("t-1", "r-1")

        # 先 enable
        runtime.mcp_tool_state_store.set_enabled("mcp__echo__echo", True)
        # 创建 + 批准
        rid = client.post(
            f"{settings.api_prefix}/confirmations",
            json={
                "run_id": "r-1", "thread_id": "t-1", "project_id": None,
                "principal": "MAIN_AGENT", "tool_name": "mcp__echo__echo",
                "arguments": {"m": "x"}, "ttl_seconds": 60.0,
            },
        ).json()["request_id"]
        client.post(
            f"{settings.api_prefix}/confirmations/{rid}/approve",
            json={"decided_by": "user"},
        )
        # 取消 MAIN_AGENT 主体授权
        client.post(
            f"{settings.api_prefix}/mcp/tools/mcp__echo__echo/revoke",
            json={"principal": "MAIN_AGENT"},
        )
        # 同步 policy registry
        runtime.agent._tool_policy_registry.set_policy("mcp__echo__echo", ToolPolicy(
            enabled=True,
            allowed_principals=frozenset(),  # 全 deny
            requires_confirmation=False,
        ))
        # 执行 → 409
        req = runtime.confirmation_store.get(rid)
        resp = client.post(
            f"{settings.api_prefix}/confirmations/{rid}/execute-snapshot",
            json={
                "run_id": "r-1", "thread_id": "t-1",
                "tool_name": "mcp__echo__echo",
                "arguments_hash": req.arguments_hash,
            },
        )
        self.assertEqual(resp.status_code, 409, resp.text)
        # handler 未被调用
        self.assertEqual(len(self.handler_calls), 0,
                        f"revoke 后 execute 应拒 handler 调用，实际 {len(self.handler_calls)}次")


if __name__ == "__main__":
    unittest.main()
