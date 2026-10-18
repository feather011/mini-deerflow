"""TOOL-05 round 2 integration tests.

- real TestClient + real runtime (no mock on api layer)
- assert frontend confirmationsApi source contains /api prefix
"""
import json
import os
import tempfile
import unittest

from fastapi.testclient import TestClient

import mini_deerflow.api.dependencies as deps_module

from mini_deerflow.runtime import create_runtime, RuntimeConfig
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tools import Tool, ToolRegistry
from mini_deerflow.tool_authorization import RunAuthority, ToolPolicy, ToolPolicyRegistry
from mini_deerflow.tool_contracts import (
    ExecutorPrincipal,
    ToolCapability,
    make_mcp_tool_metadata,
)


class _Harness:
    def __init__(self):
        self.temp_dir = tempfile.mkdtemp()
        store_path = os.path.join(self.temp_dir, "confirmations.json")
        os.environ["MINI_DEERFLOW_CONFIRMATION_STORE"] = store_path
        self.handler_calls: list = []
        store = InMemoryThreadStore()
        store.create("t-int")
        registry = ToolRegistry()
        def echo(args, context=None):
            self.handler_calls.append({"args": dict(args or {})})
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
        self.runtime = create_runtime(store=store, registry=registry, config=config)
        self.runtime.agent._tool_policy_registry.register_metadata(make_mcp_tool_metadata(
            "mcp__echo__echo",
            declared_capabilities=frozenset({"COMPUTE"}),
        ))
        self.runtime.agent._tool_policy_registry.set_policy("mcp__echo__echo", ToolPolicy(
            enabled=True,
            allowed_principals=frozenset({ExecutorPrincipal.MAIN_AGENT}),
            requires_confirmation=True,
        ))
        auth = RunAuthority(
            principal=ExecutorPrincipal.MAIN_AGENT,
            thread_id="t-int",
            run_id="r-int",
            project_id=None,
            granted_capabilities=frozenset({ToolCapability.COMPUTE}),
        )
        self.runtime.register_run("r-int", auth, None)
        self.runtime.thread_run_coordinator.try_acquire("t-int", "r-int")
        from mini_deerflow.api.app import create_app
        from mini_deerflow.api.config import ApiSettings
        settings = ApiSettings()
        self.app = create_app(settings=settings)
        self.app.dependency_overrides[deps_module.get_runtime] = lambda: self.runtime
        self.client = TestClient(self.app)
        self.settings = settings


class TestConfirmationApiIntegration(unittest.TestCase):
    def setUp(self):
        self.h = _Harness()

    def test_list_endpoint_path_with_api_prefix(self):
        resp = self.h.client.get(
            f"{self.h.settings.api_prefix}/confirmations?thread_id=t-int"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("confirmations", resp.json())

    def test_active_run_path_with_api_prefix(self):
        resp = self.h.client.get(
            f"{self.h.settings.api_prefix}/threads/t-int/active_run"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["run_id"], "r-int")

    def test_approve_execute_snapshot_real_path(self):
        # 1. create
        resp = self.h.client.post(
            f"{self.h.settings.api_prefix}/confirmations",
            json={
                "run_id": "r-int", "thread_id": "t-int", "project_id": None,
                "principal": "MAIN_AGENT", "tool_name": "mcp__echo__echo",
                "arguments": {"m": "real"}, "ttl_seconds": 60.0,
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        rid = resp.json()["request_id"]
        # 2. approve
        resp2 = self.h.client.post(
            f"{self.h.settings.api_prefix}/confirmations/{rid}/approve",
            json={"decided_by": "user"},
        )
        self.assertEqual(resp2.status_code, 200)
        # 3. execute-snapshot
        req = self.h.runtime.confirmation_store.get(rid)
        resp3 = self.h.client.post(
            f"{self.h.settings.api_prefix}/confirmations/{rid}/execute-snapshot",
            json={
                "run_id": req.run_id, "thread_id": req.thread_id,
                "tool_name": req.tool_name, "arguments_hash": req.arguments_hash,
            },
        )
        self.assertEqual(resp3.status_code, 200, resp3.text)
        self.assertEqual(resp3.json()["state"], "executed")
        # 4. handler real called once
        self.assertEqual(len(self.h.handler_calls), 1)
        self.assertEqual(self.h.handler_calls[0]["args"].get("m"), "real")


class TestFrontendApiClientUsesApiPrefix(unittest.TestCase):
    """Assert frontend confirmationsApi source has /api prefix (avoids 404)."""

    def test_api_paths_in_source(self):
        from pathlib import Path
        repo = Path(__file__).resolve().parents[1]
        fe_path = repo / "frontend" / "src" / "shared" / "api" / "confirmations.ts"
        src = fe_path.read_text(encoding="utf-8")
        self.assertIn("/api/confirmations", src,
                     f"confirmationsApi source should contain /api/confirmations:\n{src}")
        self.assertIn("/api/threads/", src,
                     f"confirmationsApi source should contain /api/threads/:\n{src}")
        self.assertIn("/active_run", src,
                     f"active_run probe path missing:\n{src}")


if __name__ == "__main__":
    unittest.main()
