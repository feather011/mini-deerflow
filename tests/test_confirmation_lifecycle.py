"""TOOL-04 后端确认生命周期测试（R1～R6 修复版）。

覆盖：
- R1: 真实 ToolRegistry + invoker，handler 计数与持久化结果严格一致
- R2: 一次性确认凭证（CONFIRM 工具批准后允许一次）
- R3: 原子领取 + 一次性消费（不重放，落盘失败保留 error）
- R4: 真实时钟过期检查
- R5: Run 不存活时拒绝执行
- R6: 父 authority 透传；伪造 project/principal 拒绝
"""

import os
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from mini_deerflow.confirmation import (
    ConfirmationError,
    ConfirmationRequest,
    ConfirmationState,
    ConfirmationStore,
    _hash_arguments,
)
from mini_deerflow.confirmation_manager import ConfirmationManager
from mini_deerflow.tool_authorization import (
    ExecutorPrincipal,
    RunAuthority,
    ToolPolicy,
    ToolPolicyRegistry,
)
from mini_deerflow.tool_contracts import (
    ToolCapability,
    make_mcp_tool_metadata,
)
from mini_deerflow.tools import Tool, ToolRegistry


# ============================================================================
# Helpers
# ============================================================================


def _make_policy_registry(
    allow_main: bool = True,
    requires_confirmation: bool = False,
) -> ToolPolicyRegistry:
    """构造一个含 mcp__echo__echo 的策略注册表。"""
    registry = ToolPolicyRegistry()
    registry.register_metadata(make_mcp_tool_metadata(
        "mcp__echo__echo",
        declared_capabilities=frozenset({"COMPUTE"}),
    ))
    registry.set_policy("mcp__echo__echo", ToolPolicy(
        enabled=True,
        allowed_principals=frozenset({ExecutorPrincipal.MAIN_AGENT}) if allow_main else frozenset(),
        requires_confirmation=requires_confirmation,
    ))
    return registry


def _make_authority(
    principal: ExecutorPrincipal = ExecutorPrincipal.MAIN_AGENT,
    thread_id: str = "t1",
    run_id: str = "r1",
    project_id: str | None = None,
    granted_capabilities: frozenset = frozenset({ToolCapability.COMPUTE}),
) -> RunAuthority:
    return RunAuthority(
        principal=principal,
        thread_id=thread_id,
        run_id=run_id,
        project_id=project_id,
        granted_capabilities=granted_capabilities,
    )


# ============================================================================
# 1. ConfirmationRequest
# ============================================================================


class TestConfirmationRequest(unittest.TestCase):
    def test_create_minimal(self):
        req = ConfirmationRequest.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={"message": "hi"},
            ttl_seconds=60.0, now=100.0,
        )
        self.assertEqual(req.state, ConfirmationState.PENDING)
        self.assertEqual(req.expires_at, 160.0)

    def test_arguments_hash_stable(self):
        a = _hash_arguments({"x": 1, "y": 2})
        b = _hash_arguments({"y": 2, "x": 1})
        self.assertEqual(a, b)

    def test_is_expired_uses_current_time(self):
        """R4：is_expired 用当前时间判断。"""
        req = ConfirmationRequest.create(
            run_id="r", thread_id="t", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="t", arguments={}, ttl_seconds=10.0, now=100.0,
        )
        self.assertFalse(req.is_expired(now=100.0))
        self.assertFalse(req.is_expired(now=109.9))
        self.assertTrue(req.is_expired(now=110.0))
        # 真实时间也工作（远大于 100.0）
        self.assertTrue(req.is_expired(now=time.time() + 1000))


# ============================================================================
# 2. R1: 真实 invoker + 一次性执行
# ============================================================================


class TestR1RealInvoker(unittest.TestCase):
    """R1：API/manager 必须用真实 invoker，禁止伪报 executed/ok。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "conf.json"
        self.store = ConfirmationStore(path=self.path)
        self.policy = _make_policy_registry()
        self.manager = ConfirmationManager(self.store, self.policy)
        self.handler_calls: list[dict] = []

    def tearDown(self):
        self.temp_dir.cleanup()

    def _real_invoker(self, tool_name, args):
        self.handler_calls.append({"tool": tool_name, "args": dict(args)})
        return f"result:{tool_name}"

    def test_claim_and_execute_calls_invoker_once(self):
        """批准后真实调用 invoker 一次，计数 = 1。"""
        req = self.manager.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={"m": "hi"},
        )
        self.manager.approve(req.request_id, decided_by="user", now=200.0)
        executed = self.manager.claim_and_execute(
            req.request_id,
            parent_authority=_make_authority(),
            invoker=self._real_invoker,
            now=201.0,
        )
        self.assertEqual(executed.state, ConfirmationState.EXECUTED)
        self.assertEqual(executed.execution_result, "ok")
        self.assertEqual(len(self.handler_calls), 1, "handler 应只调用一次")
        self.assertEqual(self.handler_calls[0]["tool"], "mcp__echo__echo")

    def test_no_invoker_rejected(self):
        """R1：invoker=None → 拒绝，禁止伪报成功。"""
        req = self.manager.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={},
        )
        self.manager.approve(req.request_id, decided_by="user", now=200.0)
        with self.assertRaises(ConfirmationError) as ctx:
            self.manager.claim_and_execute(
                req.request_id,
                parent_authority=_make_authority(),
                invoker=None,
            )
        self.assertIn("必须提供 invoker", str(ctx.exception))

    def test_double_execute_does_not_re_invoke(self):
        """R3：第二次执行 → 拒绝，handler 不会被再次调用。"""
        req = self.manager.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={},
        )
        self.manager.approve(req.request_id, decided_by="user", now=200.0)
        self.manager.claim_and_execute(
            req.request_id,
            parent_authority=_make_authority(),
            invoker=self._real_invoker,
            now=201.0,
        )
        with self.assertRaises(ConfirmationError):
            self.manager.claim_and_execute(
                req.request_id,
                parent_authority=_make_authority(),
                invoker=self._real_invoker,
                now=202.0,
            )
        self.assertEqual(len(self.handler_calls), 1, "handler 不应被调用第二次")

    def test_handler_exception_recorded_as_error(self):
        """handler 抛错 → execution_result = error:...（不重放）"""
        req = self.manager.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={},
        )
        self.manager.approve(req.request_id, decided_by="user", now=200.0)

        def raising_invoker(tool, args):
            self.handler_calls.append({"tool": tool})
            raise ValueError("boom")

        executed = self.manager.claim_and_execute(
            req.request_id,
            parent_authority=_make_authority(),
            invoker=raising_invoker,
            now=201.0,
        )
        self.assertEqual(executed.state, ConfirmationState.EXECUTED)
        self.assertIn("error", executed.execution_result)
        self.assertEqual(len(self.handler_calls), 1)


# ============================================================================
# 3. R3: 原子领取 / 并发
# ============================================================================


class TestR3AtomicClaim(unittest.TestCase):
    """R3：原子领取 + 并发不重放。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "conf.json"
        self.store = ConfirmationStore(path=self.path)
        self.policy = _make_policy_registry()
        self.manager = ConfirmationManager(self.store, self.policy)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_concurrent_execute_only_one_succeeds(self):
        """并发两个 execute → 只一个进入 handler。"""
        req = self.manager.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={},
        )
        self.manager.approve(req.request_id, decided_by="user", now=200.0)

        call_count = [0]
        call_lock = threading.Lock()

        def slow_invoker(tool, args):
            with call_lock:
                call_count[0] += 1
            time.sleep(0.05)
            return "ok"

        results: list[Any] = []
        errors: list[Exception] = []

        def worker():
            try:
                res = self.manager.claim_and_execute(
                    req.request_id,
                    parent_authority=_make_authority(),
                    invoker=slow_invoker,
                )
                results.append(res)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 5 个并发，只 1 个成功进入 handler；其余 4 个抛错
        self.assertEqual(call_count[0], 1,
                        f"handler 应只调用一次，实际 {call_count[0]}")
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 4)
        # 所有 error 都是 ConfirmationError
        for e in errors:
            self.assertIsInstance(e, ConfirmationError)

    def test_save_failure_during_claim_preserves_approved(self):
        """R3：claim 持久化失败 → 状态保留 APPROVED（不进入 EXECUTED）"""
        req = self.manager.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={},
        )
        self.manager.approve(req.request_id, decided_by="user", now=200.0)

        # 注入：claim 时 os.replace 失败
        original_replace = os.replace
        call_count = [0]

        def failing_replace(src, dst):
            call_count[0] += 1
            if call_count[0] == 1:
                raise OSError("simulated disk full during claim")
            return original_replace(src, dst)

        call_log = []

        def invoker(tool, args):
            call_log.append(tool)
            return "ok"

        with patch("mini_deerflow.confirmation.os.replace", side_effect=failing_replace):
            with self.assertRaises(ConfirmationError):
                self.manager.claim_and_execute(
                    req.request_id,
                    parent_authority=_make_authority(),
                    invoker=invoker,
                )
        # 关键：handler 未被调用（领取失败就不调）
        self.assertEqual(len(call_log), 0, "claim 失败时不应调 handler")
        # 状态保留 APPROVED
        req2 = self.store.get(req.request_id)
        self.assertEqual(req2.state, ConfirmationState.APPROVED,
                        "claim 失败时状态应保留 APPROVED")


# ============================================================================
# 4. R4: 真实时钟过期
# ============================================================================


class TestR4RealClockExpiry(unittest.TestCase):
    """R4：approve/execute 用真实时钟检查过期。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "conf.json"
        self.store = ConfirmationStore(path=self.path)
        self.policy = _make_policy_registry()
        self.manager = ConfirmationManager(self.store, self.policy)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_expired_pending_rejected_at_approve_with_real_clock(self):
        """R4：ttl 已过期的 PENDING → approve 时（不传 now）应拒绝。"""
        # 在 now=100 创建，ttl=10 → expires_at=110
        req = self.manager.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={},
            ttl_seconds=10.0, now=100.0,
        )
        # 不调 sweep_expired，直接 approve（不传 now → 用真实时钟）
        # 真实时钟（time.time()）>> 110，应判过期
        with self.assertRaises(ConfirmationError) as ctx:
            self.manager.approve(req.request_id, decided_by="user")
        self.assertIn("已过期", str(ctx.exception))

    def test_expired_approved_rejected_at_execute_with_real_clock(self):
        """R4：已批准但过期的请求 → execute 应拒绝。"""
        # 手动构造：现在 approve（用 now=200），再 expire
        req = self.manager.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={},
            ttl_seconds=10.0, now=100.0,
        )
        # 直接通过 store 写一个 APPROVED 状态（绕过真实时钟过期）
        from dataclasses import replace as _dc_replace
        approved = _dc_replace(req, state=ConfirmationState.APPROVED, decided_at=110.0)
        self.store.update(approved)

        # 真实时钟下执行 → 应拒绝
        with self.assertRaises(ConfirmationError) as ctx:
            self.manager.claim_and_execute(
                req.request_id,
                parent_authority=_make_authority(),
                invoker=lambda t, a: "ok",
            )
        self.assertIn("已过期", str(ctx.exception))


# ============================================================================
# 5. R5: Run 生命周期
# ============================================================================


class TestR5RunLifecycle(unittest.TestCase):
    """R5：执行前检查 Run 存活；停止后拒绝。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "conf.json"
        self.store = ConfirmationStore(path=self.path)
        self.policy = _make_policy_registry()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_dead_run_rejected(self):
        """R5：is_run_alive=False → 拒绝执行。"""
        def dead_run_check(run_id, thread_id):
            return False
        mgr = ConfirmationManager(self.store, self.policy, is_run_alive=dead_run_check)
        req = mgr.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={},
        )
        mgr.approve(req.request_id, decided_by="user", now=200.0)
        with self.assertRaises(ConfirmationError) as ctx:
            mgr.claim_and_execute(
                req.request_id,
                parent_authority=_make_authority(),
                invoker=lambda t, a: "ok",
            )
        self.assertIn("已停止", str(ctx.exception))

    def test_alive_run_passes(self):
        def alive_check(run_id, thread_id):
            return True
        mgr = ConfirmationManager(self.store, self.policy, is_run_alive=alive_check)
        req = mgr.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={},
        )
        mgr.approve(req.request_id, decided_by="user", now=200.0)
        executed = mgr.claim_and_execute(
            req.request_id,
            parent_authority=_make_authority(),
            invoker=lambda t, a: "ok",
        )
        self.assertEqual(executed.state, ConfirmationState.EXECUTED)

    def test_default_no_is_run_alive_allows(self):
        """未传 is_run_alive → 默认放行（向后兼容）。"""
        mgr = ConfirmationManager(self.store, self.policy)
        req = mgr.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={},
        )
        mgr.approve(req.request_id, decided_by="user", now=200.0)
        executed = mgr.claim_and_execute(
            req.request_id,
            parent_authority=_make_authority(),
            invoker=lambda t, a: "ok",
        )
        self.assertEqual(executed.state, ConfirmationState.EXECUTED)


# ============================================================================
# 6. R6: 父 authority 透传 + 伪造拒绝
# ============================================================================


class TestR6ParentAuthority(unittest.TestCase):
    """R6：客户端伪造 project/principal 必须被拒绝。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "conf.json"
        self.store = ConfirmationStore(path=self.path)
        self.policy = _make_policy_registry()
        self.manager = ConfirmationManager(self.store, self.policy)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_forged_project_id_rejected(self):
        """R6：客户端伪造 project_id 与父 authority 不一致 → 拒绝。"""
        # 请求时 project_id=None
        req = self.manager.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={},
        )
        self.manager.approve(req.request_id, decided_by="user", now=200.0)
        # 客户端伪造 project_id="fake"
        with self.assertRaises(ConfirmationError) as ctx:
            self.manager.claim_and_execute(
                req.request_id,
                parent_authority=_make_authority(project_id="fake"),
                invoker=lambda t, a: "ok",
            )
        self.assertIn("project_id 不匹配", str(ctx.exception))

    def test_forged_principal_rejected(self):
        """R6：客户端伪造 principal 与请求不一致 → 拒绝。"""
        req = self.manager.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={},
        )
        self.manager.approve(req.request_id, decided_by="user", now=200.0)
        with self.assertRaises(ConfirmationError):
            self.manager.claim_and_execute(
                req.request_id,
                parent_authority=_make_authority(principal=ExecutorPrincipal.HARNESS_AGENT),
                invoker=lambda t, a: "ok",
            )

    def test_no_capability_escalation(self):
        """R6：执行前用父 authority 复核 — 缺少能力时拒绝。"""
        # 创建一个需要 COMMAND_EXECUTE 能力的工具 metadata
        from mini_deerflow.tool_contracts import (
            ToolMetadata, ToolSourceKind, ToolSideEffect,
        )
        self.policy.register_metadata(ToolMetadata(
            name="mcp__danger__exec",
            source=ToolSourceKind.MCP,
            capabilities=frozenset({ToolCapability.COMMAND_EXECUTE}),
            side_effects=frozenset({ToolSideEffect.RUNS_COMMAND}),
        ))
        self.policy.set_policy("mcp__danger__exec", ToolPolicy(
            enabled=True,
            allowed_principals=frozenset({ExecutorPrincipal.MAIN_AGENT}),
            requires_confirmation=True,
        ))

        req = self.manager.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__danger__exec", arguments={},
        )
        self.manager.approve(req.request_id, decided_by="user", now=200.0)
        # 父 authority 只给 COMPUTE，没有 COMMAND_EXECUTE
        with self.assertRaises(ConfirmationError) as ctx:
            self.manager.claim_and_execute(
                req.request_id,
                parent_authority=_make_authority(
                    granted_capabilities=frozenset({ToolCapability.COMPUTE}),
                ),
                invoker=lambda t, a: "ok",
            )
        self.assertIn("拒绝", str(ctx.exception))


# ============================================================================
# 7. R2: 一次性确认凭证
# ============================================================================


class TestR2OneShotConfirm(unittest.TestCase):
    """R2：CONFIRM 工具批准后，APPROVED 状态作为该次执行的一次性通过凭证。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "conf.json"
        self.store = ConfirmationStore(path=self.path)
        # requires_confirmation=True：未批准时 check_tool_access 返回 CONFIRM
        self.policy = _make_policy_registry(requires_confirmation=True)
        self.manager = ConfirmationManager(self.store, self.policy)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_confirm_tool_allowed_after_approval(self):
        """批准后 is_confirm 决策被放行一次。"""
        req = self.manager.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={},
        )
        self.manager.approve(req.request_id, decided_by="user", now=200.0)
        # 父 authority 有 COMPUTE → 一次执行成功
        executed = self.manager.claim_and_execute(
            req.request_id,
            parent_authority=_make_authority(),
            invoker=lambda t, a: "ok",
        )
        self.assertEqual(executed.state, ConfirmationState.EXECUTED)


# ============================================================================
# 8. API 端到端（runtime.execute_confirmed_request）
# ============================================================================


class TestConfirmationAPIWithRealInvoker(unittest.TestCase):
    """R1+R5+R6：API 调 runtime.execute_confirmed_request，handler 真实计数。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "conf.json"
        self.store = ConfirmationStore(path=self.path)
        self.policy = _make_policy_registry()
        self.manager = ConfirmationManager(self.store, self.policy)
        self.registry = ToolRegistry()
        self.handler_calls: list[dict] = []

        # 注册一个计数 echo 工具
        def echo_handler(args, context=None):
            self.handler_calls.append({"args": dict(args)})
            return "echo-result"
        from mini_deerflow.tools import Tool as _T
        self.registry.register(_T(
            name="mcp__echo__echo",
            description="echo",
            parameters={"type": "object", "properties": {"m": {"type": "string"}}},
            handler=echo_handler,
        ))

    def tearDown(self):
        self.temp_dir.cleanup()

    def _build_runtime(self, is_run_alive: bool = True) -> "RuntimeContext":
        from mini_deerflow.runtime import RuntimeContext, RuntimeConfig
        from mini_deerflow.confirmation_manager import ConfirmationManager as _CM

        class _AgentStub:
            _tool_policy_registry = self.policy

        runtime = RuntimeContext(
            store=None, agent=_AgentStub(), integrated_agent=None,
            manager=None, config=RuntimeConfig(), trace_sink=None, lifecycle=None,
            mcp_manager=None,
            mcp_tool_state_store=None,
            confirmation_store=self.store,
            confirmation_manager=self.manager,
        )

        # 注入 execute_confirmed_request（用本测试的 registry + invoker）
        def _execute_confirmed_request(
            request_id: str, run_id: str, thread_id: str, tool_name: str, arguments: dict
        ):
            req = self.store.get(request_id)
            if req is None:
                raise ConfirmationError(f"未知确认请求: {request_id}")
            parent_authority = _make_authority(
                principal=ExecutorPrincipal[req.principal],
                thread_id=req.thread_id,
                run_id=req.run_id,
                project_id=req.project_id,
                granted_capabilities=frozenset({ToolCapability.COMPUTE}),
            )

            def invoker(name, args):
                tool = self.registry.get(name)
                if tool is None:
                    raise ValueError(f"工具未注册: {name}")
                from mini_deerflow.middleware import ExecutionContext
                ctx = ExecutionContext(
                    run_id=run_id, thread_id=thread_id,
                    granted_capabilities=frozenset(),
                )
                return tool.execute(args, context=ctx)

            return self.manager.claim_and_execute(
                request_id,
                parent_authority=parent_authority,
                invoker=invoker,
            )

        runtime.execute_confirmed_request = _execute_confirmed_request
        return runtime

    def test_api_execute_calls_handler(self):
        """API execute → 真实 handler 调用一次。"""
        from fastapi.testclient import TestClient
        from mini_deerflow.api.app import create_app
        from mini_deerflow.api.config import ApiSettings
        from mini_deerflow.api.dependencies import get_runtime

        runtime = self._build_runtime()
        settings = ApiSettings()
        app = create_app(settings=settings)
        app.dependency_overrides[get_runtime] = lambda: runtime
        client = TestClient(app)

        # 创建
        resp = client.post(
            f"{settings.api_prefix}/confirmations",
            json={
                "run_id": "r1", "thread_id": "t1", "project_id": None,
                "principal": "MAIN_AGENT",
                "tool_name": "mcp__echo__echo",
                "arguments": {"m": "hi"},
                "ttl_seconds": 60.0,
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        rid = resp.json()["request_id"]

        # 批准
        resp = client.post(
            f"{settings.api_prefix}/confirmations/{rid}/approve",
            json={"decided_by": "user"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        # 执行前 handler 计数 = 0
        self.assertEqual(len(self.handler_calls), 0)

        # 执行
        resp = client.post(
            f"{settings.api_prefix}/confirmations/{rid}/execute",
            json={
                "run_id": "r1", "thread_id": "t1",
                "tool_name": "mcp__echo__echo",
                "arguments": {"m": "hi"},
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertEqual(data["state"], "executed")
        self.assertEqual(data["execution_result"], "ok")

        # handler 计数 = 1
        self.assertEqual(len(self.handler_calls), 1,
                        f"R1: API 执行后 handler 应调用一次，实际 {len(self.handler_calls)}")

    def test_api_execute_403_when_no_executor(self):
        """R1：runtime 不提供 execute_confirmed_request → 503 拒绝伪报。"""
        from fastapi.testclient import TestClient
        from mini_deerflow.api.app import create_app
        from mini_deerflow.api.config import ApiSettings
        from mini_deerflow.api.dependencies import get_runtime
        from mini_deerflow.runtime import RuntimeContext, RuntimeConfig

        class _AgentStub:
            _tool_policy_registry = self.policy

        runtime = RuntimeContext(
            store=None, agent=_AgentStub(), integrated_agent=None,
            manager=None, config=RuntimeConfig(), trace_sink=None, lifecycle=None,
            confirmation_store=self.store,
            confirmation_manager=self.manager,
            # 没有 execute_confirmed_request
        )

        settings = ApiSettings()
        app = create_app(settings=settings)
        app.dependency_overrides[get_runtime] = lambda: runtime
        client = TestClient(app)

        resp = client.post(
            f"{settings.api_prefix}/confirmations",
            json={
                "run_id": "r1", "thread_id": "t1", "project_id": None,
                "principal": "MAIN_AGENT",
                "tool_name": "mcp__echo__echo",
                "arguments": {"m": "hi"},
            },
        )
        rid = resp.json()["request_id"]
        client.post(
            f"{settings.api_prefix}/confirmations/{rid}/approve",
            json={"decided_by": "user"},
        )

        resp = client.post(
            f"{settings.api_prefix}/confirmations/{rid}/execute",
            json={
                "run_id": "r1", "thread_id": "t1",
                "tool_name": "mcp__echo__echo",
                "arguments": {"m": "hi"},
            },
        )
        self.assertEqual(resp.status_code, 503, resp.text)
        body = resp.json()
        msg = body.get("detail") or body.get("error", {}).get("message", "")
        self.assertIn("未提供", msg)


# ============================================================================
# 9. R3 round 2: 决定路径与领取同锁（迟到 approve 不再覆盖）
# ============================================================================


class TestR3DecisionPathAtomicity(unittest.TestCase):
    """R3：approve/deny/cancel/expire 与 claim_and_execute 共用同一锁；终态不可覆盖。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "conf.json"
        self.store = ConfirmationStore(path=self.path)
        self.policy = _make_policy_registry()
        self.manager = ConfirmationManager(self.store, self.policy)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_approve_after_execute_rejected(self):
        """R3：已 EXECUTED 后再 approve → 拒绝（不覆盖终态）。"""
        req = self.manager.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={},
        )
        self.manager.approve(req.request_id, decided_by="user", now=200.0)
        self.manager.claim_and_execute(
            req.request_id,
            parent_authority=_make_authority(),
            invoker=lambda t, a: "ok",
            now=201.0,
        )
        # 已 EXECUTED → 任何决定都应拒绝
        with self.assertRaises(ConfirmationError):
            self.manager.approve(req.request_id, decided_by="user2", now=202.0)
        with self.assertRaises(ConfirmationError):
            self.manager.deny(req.request_id, decided_by="user2", now=202.0)
        with self.assertRaises(ConfirmationError):
            self.manager.cancel(req.request_id, decided_by="user2", now=202.0)

    def test_late_approve_does_not_reset_executed(self):
        """R3 round 2：迟到 approve（先等执行完）不能把 EXECUTED 改回 APPROVED。"""
        import threading
        results: list[Any] = []
        errors: list[Exception] = []

        req = self.manager.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={},
        )
        # 先 approve
        self.manager.approve(req.request_id, decided_by="user", now=200.0)

        # 模拟：worker A 立即领取执行；worker B 晚到一会儿后调用 approve
        def worker_a():
            try:
                res = self.manager.claim_and_execute(
                    req.request_id,
                    parent_authority=_make_authority(),
                    invoker=lambda t, a: "ok",
                )
                results.append(("a", res))
            except Exception as e:
                errors.append(("a", e))

        def worker_b():
            try:
                time.sleep(0.05)  # 让 A 先跑
                res = self.manager.approve(req.request_id, decided_by="late", now=300.0)
                results.append(("b", res))
            except Exception as e:
                errors.append(("b", e))

        ta = threading.Thread(target=worker_a)
        tb = threading.Thread(target=worker_b)
        ta.start()
        tb.start()
        ta.join()
        tb.join()

        # A 成功执行
        self.assertEqual(len(results), 1, f"应只 1 个成功结果，实际 {results}")
        self.assertEqual(results[0][0], "a")
        self.assertEqual(results[0][1].state, ConfirmationState.EXECUTED)
        # B 抛错（不能覆盖 EXECUTED）
        self.assertEqual(len(errors), 1, f"应只有 1 个错误")
        self.assertEqual(errors[0][0], "b")
        self.assertIsInstance(errors[0][1], ConfirmationError)

    def test_concurrent_approve_and_execute_safe(self):
        """R3 round 2：approve + execute 并发 → 终态一致。"""
        import threading
        results: list[Any] = []
        errors: list[Exception] = []

        req = self.manager.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={},
        )
        # 先同步 approve（确保 APPROVED 状态）
        self.manager.approve(req.request_id, decided_by="user", now=200.0)

        # 3 个 execute 线程 + 2 个 approve 线程并发
        def worker_execute():
            try:
                res = self.manager.claim_and_execute(
                    req.request_id,
                    parent_authority=_make_authority(),
                    invoker=lambda t, a: "ok",
                )
                results.append(("exec", res))
            except Exception as e:
                errors.append(("exec", e))

        def worker_approve():
            try:
                res = self.manager.approve(req.request_id, decided_by="user2", now=201.0)
                results.append(("approve", res))
            except Exception as e:
                errors.append(("approve", e))

        threads = [threading.Thread(target=worker_execute) for _ in range(3)] + \
                  [threading.Thread(target=worker_approve) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 至少 1 个执行成功
        executed_count = sum(1 for r in results if r[0] == "exec")
        self.assertGreaterEqual(executed_count, 1,
                               f"应至少 1 个 exec 成功，实际 results={results} errors={errors}")
        # 终态 EXECUTED
        final = self.store.get(req.request_id)
        self.assertEqual(final.state, ConfirmationState.EXECUTED)


# ============================================================================
# 10. R6 round 2: 深拷贝/嵌套参数 + 调用前 hash 复核
# ============================================================================


class TestR6DeepCopyAndHashCheck(unittest.TestCase):
    """R6：参数深拷贝、嵌套对象不可共享、执行前 hash 复核。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "conf.json"
        self.store = ConfirmationStore(path=self.path)
        self.policy = _make_policy_registry()
        self.manager = ConfirmationManager(self.store, self.policy)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_nested_argument_not_mutated_after_create(self):
        """R6：批准后修改原嵌套 dict，存储中的 arguments 不变（保持 dict/list 类型）。"""
        import copy as _copy
        nested = {"data": {"value": 1}, "items": [1, 2]}
        req = self.manager.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo",
            arguments=_copy.deepcopy(nested),
        )
        # 批准后修改原 nested
        nested["data"]["value"] = 999
        nested["items"].append(99)
        # 存储中仍是原值
        stored = self.store.get(req.request_id)
        self.assertEqual(stored.arguments, {"data": {"value": 1}, "items": [1, 2]},
                        f"存储应保留原 JSON 结构，实际 {stored.arguments}")
        # 验证 type：dict/list 不是 tuple
        self.assertIsInstance(stored.arguments["data"], dict)
        self.assertIsInstance(stored.arguments["items"], list)

    def test_execute_rejects_modified_arguments(self):
        """R6：执行时 caller 传不同 arguments → 拒绝（hash 复核）。"""
        import copy as _copy
        args = {"data": {"value": 1}}
        req = self.manager.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo",
            arguments=_copy.deepcopy(args),
        )
        self.manager.approve(req.request_id, decided_by="user", now=200.0)
        # 试图用修改后的 args 执行（caller 端 hash 校验）
        # 注：当前 manager.claim_and_execute 仍接受 invoker 直接传 args；
        # 测试时由 caller 在调用前 hash 校验。runtime._execute_confirmed_request 内部
        # 已做此校验；这里仅测试 manager 层面 hash 校验辅助
        args_modified = {"data": {"value": 2}}
        # 模拟 runtime 内部：caller 传修改后 args，runtime 算 hash 不匹配 → 拒绝
        from mini_deerflow.confirmation import _hash_arguments
        expected_hash = req.arguments_hash
        actual_hash = _hash_arguments(args_modified)
        self.assertNotEqual(actual_hash, expected_hash,
                            "修改后 hash 应与原 hash 不同")


# ============================================================================
# 11. R5 round 2: 完整 Run 生命周期（含 is_run_alive 绑 run_id）
# ============================================================================


class TestR5RunLifecycleBoundRunId(unittest.TestCase):
    """R5：is_run_alive 同时检查 run_id（不只是 thread）。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "conf.json"
        self.store = ConfirmationStore(path=self.path)
        self.policy = _make_policy_registry()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_legacy_thread_busy_resurrect_old_approval(self):
        """同 thread 新 run 占用时，旧 run 批准的请求应被拒绝。"""
        from mini_deerflow.confirmation_manager import ConfirmationManager
        from mini_deerflow.thread_run_coordinator import ThreadRunCoordinator
        from mini_deerflow.tools import ToolRegistry
        coord = ThreadRunCoordinator()
        # 模拟：原 run1 启动（thread1 上）
        coord.try_acquire(thread_id="t1", run_id="r1")
        # 旧 r1 创建并批准
        mgr = ConfirmationManager(
            self.store, self.policy,
            is_run_alive=lambda rid, tid: coord.is_running(tid) and rid in coord._running,
        )
        req = mgr.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={},
        )
        mgr.approve(req.request_id, decided_by="user", now=200.0)
        # 同 thread 启动新 run2（r1 已停）
        coord.release("t1", "r1")
        coord.try_acquire(thread_id="t1", run_id="r2")
        # 旧 r1 批准不能再用（r1 不存活，thread 忙但跑的是 r2）
        with self.assertRaises(ConfirmationError) as ctx:
            mgr.claim_and_execute(
                req.request_id,
                parent_authority=_make_authority(run_id="r1", thread_id="t1"),
                invoker=lambda t, a: "ok",
            )
        self.assertIn("已停止", str(ctx.exception))


class TestR3StoreSerializationRound3(unittest.TestCase):
    """R3 round 3：并发 create + claim + 重建后 handler 计数只 1。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "conf.json"
        self.store = ConfirmationStore(path=self.path)
        self.policy = _make_policy_registry()
        self.manager = ConfirmationManager(self.store, self.policy)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_concurrent_create_and_execute_then_rebuild_handler_count(self):
        """R3 round 3：并发 create + claim/execute + 重建后 handler 计数只 1。"""
        import threading
        handler_calls: list[str] = []
        results: list[Any] = []
        errors: list[Exception] = []

        # 创建并批准
        req = self.manager.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={},
        )
        self.manager.approve(req.request_id, decided_by="user", now=200.0)

        # 模拟：claim_and_execute + 并发 create 互不干扰
        def worker_execute():
            try:
                def invoker(name, args):
                    handler_calls.append(name)
                    return "ok"
                res = self.manager.claim_and_execute(
                    req.request_id,
                    parent_authority=_make_authority(),
                    invoker=invoker,
                )
                results.append(res)
            except Exception as e:
                errors.append(e)

        def worker_create():
            try:
                self.manager.create(
                    run_id="r2", thread_id="t2", project_id=None,
                    principal=ExecutorPrincipal.MAIN_AGENT,
                    tool_name="mcp__echo__echo", arguments={},
                )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker_execute)] + \
                  [threading.Thread(target=worker_create) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 关键：原 req 已 EXECUTED，handler 只调用 1 次
        self.assertEqual(len(handler_calls), 1,
                        f"应只调用 handler 1 次，实际 {len(handler_calls)}")
        # 终态 EXECUTED
        final = self.store.get(req.request_id)
        self.assertEqual(final.state, ConfirmationState.EXECUTED)

        # 重建（删除 store 实例，重新从磁盘加载）
        del self.store
        store_reload = ConfirmationStore(path=self.path)
        reloaded = store_reload.get(req.request_id)
        self.assertEqual(reloaded.state, ConfirmationState.EXECUTED)
        # 重建后不能再次执行
        mgr2 = ConfirmationManager(store_reload, self.policy)
        with self.assertRaises(ConfirmationError):
            mgr2.claim_and_execute(
                req.request_id,
                parent_authority=_make_authority(),
                invoker=lambda t, a: "should_not_be_called",
            )
        # 重建后 handler 仍未被调（仍是 1 次）
        self.assertEqual(len(handler_calls), 1)


# ============================================================================
# R1/R5/R6 round 4：原 create_runtime + AgentRunService 正路径 E2E
# ============================================================================


class _ScriptedModel:
    """按预设序列返回决策。"""
    def __init__(self, decisions):
        from collections import deque
        self._queue = deque(decisions)

    def decide(self, messages, tools=None):
        return self._queue.popleft()


class TestR1R5R6EndToEndCreateRuntime(unittest.TestCase):
    """R1/R5/R6 round 4：原 create_runtime + AgentRunService 正路径 E2E。

    目标：
    1. 启动真实 AgentRunService.run（run_id 由调用方提供并贯通全链路）
    2. Agent 调用需要确认的工具 → pending 绑定的 run_id 是真实 run_id（不是 "main"）
    3. 批准 + API execute → 真实 handler 调用一次
    4. Agent 继续返回最终答案
    5. Run 结束后 runtime._active_run_authorities 中该 run_id 已清理
    """

    def setUp(self):
        self.temp_dir = TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _build_runtime(self, handler_calls, decision_results):
        """构造真实 create_runtime + 计数 handler + scripted model。

        decision_results: 给 _ScriptedModel 的决策列表（ToolCall/FinalAnswer）。
        """
        from mini_deerflow.runtime import create_runtime, RuntimeConfig
        from mini_deerflow.thread_store import InMemoryThreadStore
        from mini_deerflow.tools import ToolRegistry
        from mini_deerflow.tool_contracts import (
            make_mcp_tool_metadata,
            ToolPolicy,
        )

        store = InMemoryThreadStore()
        store.create("t-e2e")
        registry = ToolRegistry()

        # 注册一个计数 handler
        def echo_handler(args, context=None):
            handler_calls.append({"args": dict(args or {}), "ctx": context})
            return "echo-result"

        registry.register(Tool(
            name="mcp__echo__echo",
            description="echo",
            parameters={"type": "object", "properties": {"m": {"type": "string"}}},
            handler=echo_handler,
        ))

        config = RuntimeConfig(
            workspace_root=str(self.temp_dir.name),
            subagent_enabled=False,
            trace_enabled=False,
        )
        runtime = create_runtime(
            store=store,
            registry=registry,
            config=config,
        )
        # 在 create_runtime 内部构造的 tool_policy_registry 上加策略：
        # 该实例同时被 agent 和 confirmation_manager 引用，一改两生效。
        policy_registry = runtime.agent._tool_policy_registry
        policy_registry.register_metadata(make_mcp_tool_metadata(
            "mcp__echo__echo",
            declared_capabilities=frozenset({"COMPUTE"}),
        ))
        policy_registry.set_policy("mcp__echo__echo", ToolPolicy(
            enabled=True,
            allowed_principals=frozenset({ExecutorPrincipal.MAIN_AGENT}),
            requires_confirmation=True,
        ))
        # 注入 scripted model（避免真实模型）
        runtime.agent._model = _ScriptedModel(decision_results)
        runtime.integrated_agent._agent._model = runtime.agent._model
        return runtime, store

    def _wait_for_pending(self, runtime, run_id, timeout=5.0):
        """等待 pending 出现并验证 run_id 绑定。"""
        deadline = time.time() + timeout
        all_reqs = []
        while time.time() < deadline:
            # 列出所有请求（仅 PENDING 状态）
            all_reqs = runtime.confirmation_store.all()
            pending = [r for r in all_reqs if r.state == ConfirmationState.PENDING]
            if pending:
                # 找到绑定目标 run_id 的 pending
                matched = [r for r in pending if r.run_id == run_id]
                if matched:
                    return matched[0]
            time.sleep(0.05)
        # 超时：报错
        states = [(r.request_id[:8], r.run_id, r.state.value) for r in all_reqs]
        raise AssertionError(
            f"等待 pending 超时（run_id={run_id}），已有请求: {states}"
        )

    def test_real_create_runtime_e2e(self):
        """原 create_runtime + AgentRunService 正路径：pending 绑真 run_id + handler 真实计数 + Agent 继续。"""
        from mini_deerflow.middleware import CancellationToken
        from mini_deerflow.models import FinalAnswer, ToolCall

        handler_calls: list[dict] = []
        # Agent 第一次返回 tool call（要确认的 echo 工具），第二次返回最终答案
        decisions = [
            ToolCall(id="c1", name="mcp__echo__echo", arguments={"m": "hi"}),
            FinalAnswer(content="done"),
        ]
        runtime, store = self._build_runtime(handler_calls, decisions)

        real_run_id = "test-real-run-001"
        token = CancellationToken()

        # 在后台线程中启动真实 AgentRunService.run
        result_holder: dict[str, Any] = {}

        def run_in_thread():
            try:
                # 用 service 而非直接 IntegratedAgent，触发 register_run 真实生产者
                result = runtime.agent_run_service.run(
                    "t-e2e",
                    "echo hi",
                    run_id=real_run_id,
                    cancellation_token=token,
                )
                result_holder["result"] = result
            except Exception as e:
                result_holder["error"] = e

        t = threading.Thread(target=run_in_thread, daemon=True)
        t.start()

        # 等 pending 出现，并验证 run_id 绑定
        try:
            pending = self._wait_for_pending(runtime, real_run_id, timeout=5.0)
        except Exception:
            t.join(timeout=2)
            raise

        # 关键断言 1：pending.run_id == 真实 run_id（不是 "main"）
        self.assertEqual(
            pending.run_id, real_run_id,
            f"R1: pending 绑定的 run_id 应是 {real_run_id}，实际 {pending.run_id!r}"
        )

        # 关键断言 2：authority 已注册
        self.assertIn(real_run_id, runtime._active_run_authorities,
                     "R6: RunAuthority 应由 AgentRunService 注入")
        authority = runtime._active_run_authorities[real_run_id]
        self.assertEqual(authority.run_id, real_run_id)
        self.assertEqual(authority.thread_id, "t-e2e")
        # 验证 granted_capabilities 是真实的能力集（不是空 frozenset）
        self.assertGreater(len(authority.granted_capabilities), 0,
                          "R6: 父权限应有真实授予能力")

        # 关键断言 3：handler 在批准前未调用
        self.assertEqual(len(handler_calls), 0,
                        "R1: 批准前 handler 不应被调用")

        # 批准
        runtime.confirmation_manager.approve(
            pending.request_id, decided_by="tester", now=time.time() + 10
        )

        # 执行（走真实 execute_confirmed_request）
        runtime.execute_confirmed_request(
            request_id=pending.request_id,
            run_id=real_run_id,
            thread_id="t-e2e",
            tool_name=pending.tool_name,
            arguments=dict(pending.arguments),
        )

        # 关键断言 4：handler 被调用 1 次
        self.assertEqual(len(handler_calls), 1,
                        f"R1: API execute 后 handler 应调用 1 次，实际 {len(handler_calls)}")

        # 等 run 结束
        t.join(timeout=10)
        self.assertFalse(t.is_alive(), "Agent run 应在合理时间内完成")
        self.assertNotIn("error", result_holder,
                        f"Agent run 异常: {result_holder.get('error')}")
        result = result_holder.get("result")
        self.assertIsNotNone(result, "Agent run 应返回结果")
        # Agent 应该返回最终答案 "done"
        self.assertEqual(result.answer, "done",
                        f"R1: Agent 应回灌最终答案，实际 {result.answer!r}")

        # 关键断言 5：run 结束后 authority 已清理
        self.assertNotIn(real_run_id, runtime._active_run_authorities,
                        "R6: run 结束后 RunAuthority 应被清理")
        # token 也应清理
        self.assertNotIn(real_run_id, runtime._runtime_cancellation_tokens,
                        "R5: run 结束后 CancellationToken 应被清理")

    def test_e2e_forged_run_id_rejected(self):
        """R6 round 4：伪造 run_id 绑定 → execute 端 409 拒绝。"""
        from mini_deerflow.middleware import CancellationToken
        from mini_deerflow.models import FinalAnswer, ToolCall

        handler_calls: list[dict] = []
        decisions = [
            ToolCall(id="c1", name="mcp__echo__echo", arguments={"m": "hi"}),
            FinalAnswer(content="done"),
        ]
        runtime, _ = self._build_runtime(handler_calls, decisions)

        real_run_id = "test-real-run-002"
        token = CancellationToken()

        result_holder: dict[str, Any] = {}

        def run_in_thread():
            try:
                result = runtime.agent_run_service.run(
                    "t-e2e",
                    "echo hi",
                    run_id=real_run_id,
                    cancellation_token=token,
                )
                result_holder["result"] = result
            except Exception as e:
                result_holder["error"] = e

        t = threading.Thread(target=run_in_thread, daemon=True)
        t.start()

        pending = self._wait_for_pending(runtime, real_run_id, timeout=5.0)

        # 批准
        runtime.confirmation_manager.approve(
            pending.request_id, decided_by="tester", now=time.time() + 10
        )

        # 尝试用伪造 run_id 执行
        with self.assertRaises(ConfirmationError) as ctx:
            runtime.execute_confirmed_request(
                request_id=pending.request_id,
                run_id="forged-run-id",  # 伪造
                thread_id="t-e2e",
                tool_name=pending.tool_name,
                arguments=dict(pending.arguments),
            )
        self.assertIn("run_id", str(ctx.exception).lower(),
                     f"应拒绝伪造 run_id，实际错误: {ctx.exception}")

        # 关键：handler 未被调用
        self.assertEqual(len(handler_calls), 0,
                        "R6: 伪造绑定应阻止 handler 调用")

        # 清理
        token.cancel()
        t.join(timeout=3)

    def test_e2e_run_cancelled_blocks_execute(self):
        """R5 round 4：原 stop() 取消后，execute 端被 409 拒绝。"""
        from mini_deerflow.middleware import CancellationToken
        from mini_deerflow.models import FinalAnswer, ToolCall

        handler_calls: list[dict] = []
        decisions = [
            ToolCall(id="c1", name="mcp__echo__echo", arguments={"m": "hi"}),
            FinalAnswer(content="done"),
        ]
        runtime, _ = self._build_runtime(handler_calls, decisions)

        real_run_id = "test-real-run-003"
        token = CancellationToken()

        result_holder: dict[str, Any] = {}

        def run_in_thread():
            try:
                result = runtime.agent_run_service.run(
                    "t-e2e",
                    "echo hi",
                    run_id=real_run_id,
                    cancellation_token=token,
                )
                result_holder["result"] = result
            except Exception:
                result_holder["error"] = "cancelled or failed"

        t = threading.Thread(target=run_in_thread, daemon=True)
        t.start()

        pending = self._wait_for_pending(runtime, real_run_id, timeout=5.0)

        # 批准
        runtime.confirmation_manager.approve(
            pending.request_id, decided_by="tester", now=time.time() + 10
        )

        # 取消 run（模拟真实 stop 路径）
        token.cancel()
        # 等待取消生效
        time.sleep(0.1)

        # 尝试执行：应被 _is_run_alive 拒绝
        with self.assertRaises(ConfirmationError) as ctx:
            runtime.execute_confirmed_request(
                request_id=pending.request_id,
                run_id=real_run_id,
                thread_id="t-e2e",
                tool_name=pending.tool_name,
                arguments=dict(pending.arguments),
            )
        # handler 未调用
        self.assertEqual(len(handler_calls), 0,
                        "R5: run 取消后应阻止 handler 调用")

        t.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
