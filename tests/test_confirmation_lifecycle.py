"""TOOL-04 后端确认生命周期测试。

覆盖：
1. 请求绑定 run_id/thread_id/project_id/principal/tool_name+args
2. 批准前 handler 零执行；批准后执行一次；重复执行不重复
3. 拒绝/过期/取消/停止后不执行
4. 重启后失效批准不能直接执行
5. 执行前再次检查权限
6. 参数替换/换 run 都拒绝
"""

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

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
    ToolPolicy,
    ToolPolicyRegistry,
)
from mini_deerflow.tool_authorization import RunAuthority
from mini_deerflow.tool_contracts import ToolCapability, make_mcp_tool_metadata


# ============================================================================
# 1. ConfirmationRequest 基础
# ============================================================================


class TestConfirmationRequest(unittest.TestCase):
    """ConfirmationRequest 数据类与哈希稳定性。"""

    def test_create_minimal(self):
        """必填字段：run_id, thread_id, tool_name, principal, arguments。"""
        req = ConfirmationRequest.create(
            run_id="r1",
            thread_id="t1",
            project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo",
            arguments={"message": "hi"},
            ttl_seconds=60.0,
            now=100.0,
        )
        self.assertEqual(req.state, ConfirmationState.PENDING)
        self.assertEqual(req.expires_at, 160.0)
        self.assertEqual(req.principal, "MAIN_AGENT")
        self.assertEqual(req.run_id, "r1")
        self.assertEqual(req.thread_id, "t1")
        self.assertIsNone(req.decided_at)

    def test_arguments_hash_stable(self):
        """相同 arguments（key 顺序不同）应得到相同哈希。"""
        a = _hash_arguments({"x": 1, "y": 2})
        b = _hash_arguments({"y": 2, "x": 1})
        self.assertEqual(a, b, "key 顺序不影响哈希")

    def test_arguments_hash_differs_on_value(self):
        """value 改变 → 哈希改变。"""
        a = _hash_arguments({"x": 1})
        b = _hash_arguments({"x": 2})
        self.assertNotEqual(a, b)

    def test_is_expired(self):
        """is_expired 在 expires_at 之后为 True。"""
        req = ConfirmationRequest.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="t", arguments={}, ttl_seconds=10.0, now=100.0,
        )
        self.assertFalse(req.is_expired(now=100.0))
        self.assertFalse(req.is_expired(now=109.9))
        self.assertTrue(req.is_expired(now=110.0))

    def test_create_rejects_empty_required(self):
        """空 run_id / thread_id / tool_name 抛错。"""
        with self.assertRaises(ConfirmationError):
            ConfirmationRequest.create(
                run_id="", thread_id="t1", project_id=None,
                principal=ExecutorPrincipal.MAIN_AGENT,
                tool_name="t", arguments={}, ttl_seconds=10.0,
            )

    def test_serialize_round_trip(self):
        """to_dict / from_dict 往返一致。"""
        req = ConfirmationRequest.create(
            run_id="r1", thread_id="t1", project_id="p1",
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__x__y", arguments={"a": 1, "b": "z"},
            ttl_seconds=60.0, now=200.0,
        )
        d = req.to_dict()
        req2 = ConfirmationRequest.from_dict(d)
        self.assertEqual(req2.request_id, req.request_id)
        self.assertEqual(req2.arguments_hash, req.arguments_hash)
        self.assertEqual(req2.project_id, "p1")


# ============================================================================
# 2. ConfirmationStore 持久化
# ============================================================================


class TestConfirmationStore(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "confirmations.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_add_and_get(self):
        store = ConfirmationStore(path=self.path)
        req = ConfirmationRequest.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="t", arguments={"a": 1}, ttl_seconds=60.0,
        )
        store.add(req)
        self.assertEqual(store.get(req.request_id).request_id, req.request_id)

    def test_persistence_across_reload(self):
        store1 = ConfirmationStore(path=self.path)
        req = ConfirmationRequest.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="t", arguments={}, ttl_seconds=60.0,
        )
        store1.add(req)
        del store1
        store2 = ConfirmationStore(path=self.path)
        self.assertIsNotNone(store2.get(req.request_id))

    def test_save_failure_rolls_back(self):
        """R1 同款：落盘失败 → 内存不变。"""
        import os as _os
        from unittest.mock import patch

        store = ConfirmationStore(path=self.path)
        req = ConfirmationRequest.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="t", arguments={}, ttl_seconds=60.0,
        )
        store.add(req)

        # 注入：os.replace 失败
        def failing_replace(src, dst):
            raise OSError("simulated")

        new_req = ConfirmationRequest.create(
            run_id="r2", thread_id="t2", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="t2", arguments={}, ttl_seconds=60.0,
        )
        with patch("mini_deerflow.confirmation.os.replace", side_effect=failing_replace):
            with self.assertRaises(OSError):
                store.add(new_req)

        # 内存中不应有 r2
        self.assertIsNone(store.get(new_req.request_id))


# ============================================================================
# 3. ConfirmationManager 状态机
# ============================================================================


def _make_policy_registry(allow_main: bool = True) -> ToolPolicyRegistry:
    registry = ToolPolicyRegistry()
    registry.register_metadata(make_mcp_tool_metadata(
        "mcp__echo__echo",
        declared_capabilities=frozenset({"COMPUTE"}),
    ))
    registry.set_policy("mcp__echo__echo", ToolPolicy(
        enabled=True,
        allowed_principals=frozenset({ExecutorPrincipal.MAIN_AGENT}) if allow_main else frozenset(),
    ))
    return registry


class TestConfirmationManagerLifecycle(unittest.TestCase):
    """状态机：PENDING → APPROVED/EXPIRED/DENIED/CANCELLED/EXECUTED"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "conf.json"
        self.store = ConfirmationStore(path=self.path)
        self.policy = _make_policy_registry()
        self.manager = ConfirmationManager(self.store, self.policy)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_create_and_approve(self):
        """创建 → 批准 → 状态 APPROVED。"""
        req = self.manager.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={"m": "hi"},
        )
        approved = self.manager.approve(req.request_id, decided_by="user")
        self.assertEqual(approved.state, ConfirmationState.APPROVED)
        self.assertEqual(approved.decided_by, "user")
        self.assertIsNotNone(approved.decided_at)

    def test_approve_idempotent(self):
        """重复批准同一请求：状态保持，不重复写盘。"""
        req = self.manager.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={},
        )
        self.manager.approve(req.request_id, decided_by="user1")
        # 第二次 approve 应幂等
        approved2 = self.manager.approve(req.request_id, decided_by="user2")
        self.assertEqual(approved2.state, ConfirmationState.APPROVED)
        self.assertEqual(approved2.decided_by, "user1", "重复 approve 不覆盖决定者")

    def test_deny_prevents_execute(self):
        """拒绝后不能执行。"""
        req = self.manager.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={},
        )
        self.manager.deny(req.request_id, decided_by="user")
        with self.assertRaises(ConfirmationError):
            self.manager.execute(
                req.request_id,
                run_id="r1", thread_id="t1",
                tool_name="mcp__echo__echo", arguments={},
            )

    def test_cancel_prevents_execute(self):
        """取消后不能执行。"""
        req = self.manager.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={},
        )
        self.manager.cancel(req.request_id, decided_by="user")
        with self.assertRaises(ConfirmationError):
            self.manager.execute(
                req.request_id,
                run_id="r1", thread_id="t1",
                tool_name="mcp__echo__echo", arguments={},
            )

    def test_expire_prevents_execute(self):
        """过期后不能执行。"""
        req = self.manager.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={},
            ttl_seconds=10.0, now=100.0,
        )
        # 推进到过期
        self.manager.expire(req.request_id, now=200.0)
        with self.assertRaises(ConfirmationError):
            self.manager.execute(
                req.request_id,
                run_id="r1", thread_id="t1",
                tool_name="mcp__echo__echo", arguments={},
            )

    def test_sweep_expired(self):
        """sweep_expired 把所有 PENDING 过期请求置为 EXPIRED。"""
        r1 = self.manager.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={},
            ttl_seconds=10.0, now=100.0,
        )
        r2 = self.manager.create(
            run_id="r2", thread_id="t2", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={},
            ttl_seconds=100.0, now=100.0,
        )
        # 用 199.5：r1 (expires=110) 已过期，r2 (expires=200) 未过期
        n = self.manager.sweep_expired(now=199.5)
        self.assertEqual(n, 1, "应只把 r1 置为 EXPIRED")
        self.assertEqual(
            self.store.get(r1.request_id).state,
            ConfirmationState.EXPIRED,
        )
        self.assertEqual(
            self.store.get(r2.request_id).state,
            ConfirmationState.PENDING,
        )


# ============================================================================
# 4. 上下文/参数匹配
# ============================================================================


class TestConfirmationExecuteMatching(unittest.TestCase):
    """执行必须完全匹配 run/thread/tool/args。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "conf.json"
        self.store = ConfirmationStore(path=self.path)
        self.policy = _make_policy_registry()
        self.manager = ConfirmationManager(self.store, self.policy)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_approved(self) -> ConfirmationRequest:
        req = self.manager.create(
            run_id="r1", thread_id="t1", project_id="p1",
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={"m": "hi"},
        )
        self.manager.approve(req.request_id, decided_by="user")
        return req

    def test_execute_with_correct_context_succeeds(self):
        """完全匹配 → 状态 EXECUTED。"""
        req = self._create_approved()
        executed = self.manager.execute(
            req.request_id,
            run_id="r1", thread_id="t1",
            tool_name="mcp__echo__echo", arguments={"m": "hi"},
        )
        self.assertEqual(executed.state, ConfirmationState.EXECUTED)
        self.assertIsNotNone(executed.executed_at)

    def test_execute_with_wrong_run_id_rejected(self):
        """run_id 不匹配 → 拒绝。"""
        req = self._create_approved()
        with self.assertRaises(ConfirmationError) as ctx:
            self.manager.execute(
                req.request_id,
                run_id="r2", thread_id="t1",
                tool_name="mcp__echo__echo", arguments={"m": "hi"},
            )
        self.assertIn("run_id 不匹配", str(ctx.exception))

    def test_execute_with_wrong_thread_id_rejected(self):
        """thread_id 不匹配 → 拒绝。"""
        req = self._create_approved()
        with self.assertRaises(ConfirmationError):
            self.manager.execute(
                req.request_id,
                run_id="r1", thread_id="t2",
                tool_name="mcp__echo__echo", arguments={"m": "hi"},
            )

    def test_execute_with_wrong_arguments_rejected(self):
        """参数被替换 → 哈希不匹配 → 拒绝。"""
        req = self._create_approved()
        with self.assertRaises(ConfirmationError) as ctx:
            self.manager.execute(
                req.request_id,
                run_id="r1", thread_id="t1",
                tool_name="mcp__echo__echo", arguments={"m": "hacked"},
            )
        self.assertIn("参数不匹配", str(ctx.exception))

    def test_execute_with_wrong_tool_name_rejected(self):
        """tool_name 不匹配 → 拒绝。"""
        req = self._create_approved()
        with self.assertRaises(ConfirmationError):
            self.manager.execute(
                req.request_id,
                run_id="r1", thread_id="t1",
                tool_name="mcp__other__x", arguments={"m": "hi"},
            )


# ============================================================================
# 5. 重复执行/不重放
# ============================================================================


class TestConfirmationNoReplay(unittest.TestCase):
    """EXECUTED 后不重放 handler。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "conf.json"
        self.store = ConfirmationStore(path=self.path)
        self.policy = _make_policy_registry()
        self.manager = ConfirmationManager(self.store, self.policy)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_double_execute_does_not_re_invoke(self):
        """两次 execute → handler 只调用一次。"""
        req = self.manager.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={"m": "hi"},
        )
        self.manager.approve(req.request_id, decided_by="user")

        call_count = [0]
        def fake_invoker(tool, args):
            call_count[0] += 1
            return "result"

        self.manager.execute(
            req.request_id, run_id="r1", thread_id="t1",
            tool_name="mcp__echo__echo", arguments={"m": "hi"},
            invoker=fake_invoker,
        )
        # 第二次执行应抛错（EXECUTED 不是 APPROVED）
        with self.assertRaises(ConfirmationError):
            self.manager.execute(
                req.request_id, run_id="r1", thread_id="t1",
                tool_name="mcp__echo__echo", arguments={"m": "hi"},
                invoker=fake_invoker,
            )
        self.assertEqual(call_count[0], 1, "handler 不应被调用第二次")


# ============================================================================
# 6. 执行前重新检查权限
# ============================================================================


class TestConfirmationRecheckPermission(unittest.TestCase):
    """执行前重新检查：权限变更后旧批准不能直接用。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "conf.json"
        self.store = ConfirmationStore(path=self.path)
        self.policy = _make_policy_registry(allow_main=True)
        self.manager = ConfirmationManager(self.store, self.policy)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_permission_revoked_after_approval_blocks_execute(self):
        """批准后撤权 → 执行前复核 DENY。"""
        req = self.manager.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={},
        )
        self.manager.approve(req.request_id, decided_by="user")
        # 撤权
        self.policy.set_policy("mcp__echo__echo", ToolPolicy(
            enabled=True, allowed_principals=frozenset(),  # 移除 MAIN_AGENT
        ))
        with self.assertRaises(ConfirmationError) as ctx:
            self.manager.execute(
                req.request_id, run_id="r1", thread_id="t1",
                tool_name="mcp__echo__echo", arguments={},
            )
        self.assertIn("当前已不再允许", str(ctx.exception))

    def test_re_enable_after_approval_allows_execute(self):
        """批准后仍允许 → 仍可执行。"""
        req = self.manager.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={},
        )
        self.manager.approve(req.request_id, decided_by="user")
        # 权限仍允许，执行成功
        executed = self.manager.execute(
            req.request_id, run_id="r1", thread_id="t1",
            tool_name="mcp__echo__echo", arguments={},
        )
        self.assertEqual(executed.state, ConfirmationState.EXECUTED)


# ============================================================================
# 7. 重启后失效批准
# ============================================================================


class TestConfirmationRebootSafety(unittest.TestCase):
    """重启后旧 APPROVED 状态仍在：仍可执行（无过期则仍有效）。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "conf.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_approval_persists_across_reload(self):
        """重启：APPROVED 状态保留，相同上下文仍可执行。"""
        store1 = ConfirmationStore(path=self.path)
        policy1 = _make_policy_registry()
        mgr1 = ConfirmationManager(store1, policy1)
        req = mgr1.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={},
        )
        mgr1.approve(req.request_id, decided_by="user")
        del mgr1, store1

        store2 = ConfirmationStore(path=self.path)
        policy2 = _make_policy_registry()
        mgr2 = ConfirmationManager(store2, policy2)
        req2 = store2.get(req.request_id)
        self.assertEqual(req2.state, ConfirmationState.APPROVED)
        executed = mgr2.execute(
            req.request_id, run_id="r1", thread_id="t1",
            tool_name="mcp__echo__echo", arguments={},
        )
        self.assertEqual(executed.state, ConfirmationState.EXECUTED)

    def test_expired_approval_after_reload_cannot_execute(self):
        """重启时若已过期 → 自动 EXPIRED → 不能执行。"""
        store1 = ConfirmationStore(path=self.path)
        policy1 = _make_policy_registry()
        mgr1 = ConfirmationManager(store1, policy1)
        req = mgr1.create(
            run_id="r1", thread_id="t1", project_id=None,
            principal=ExecutorPrincipal.MAIN_AGENT,
            tool_name="mcp__echo__echo", arguments={},
            ttl_seconds=10.0, now=100.0,
        )
        # 不 approve，让其保持 PENDING；sweep 后应置 EXPIRED
        del mgr1, store1

        # 模拟：时间已到 200，扫描过期
        store2 = ConfirmationStore(path=self.path)
        policy2 = _make_policy_registry()
        mgr2 = ConfirmationManager(store2, policy2)
        mgr2.sweep_expired(now=200.0)

        req2 = store2.get(req.request_id)
        self.assertEqual(req2.state, ConfirmationState.EXPIRED)
        with self.assertRaises(ConfirmationError):
            mgr2.execute(
                req.request_id, run_id="r1", thread_id="t1",
                tool_name="mcp__echo__echo", arguments={},
            )


# ============================================================================
# 8. API 端到端
# ============================================================================


class TestConfirmationAPI(unittest.TestCase):
    """/confirmations 路由端到端。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "conf.json"
        self.store = ConfirmationStore(path=self.path)
        self.policy = _make_policy_registry()
        self.manager = ConfirmationManager(self.store, self.policy)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _build_runtime(self) -> "RuntimeContext":
        from mini_deerflow.runtime import RuntimeContext, RuntimeConfig
        # Agent stub 持有 policy registry
        class _AgentStub:
            _tool_policy_registry = self.policy
        return RuntimeContext(
            store=None, agent=_AgentStub(), integrated_agent=None,
            manager=None, config=RuntimeConfig(), trace_sink=None, lifecycle=None,
            confirmation_store=self.store,
            confirmation_manager=self.manager,
        )

    def test_create_approve_execute_flow(self):
        from fastapi.testclient import TestClient
        from mini_deerflow.api.app import create_app
        from mini_deerflow.api.config import ApiSettings
        from mini_deerflow.api.dependencies import get_runtime

        runtime = self._build_runtime()
        settings = ApiSettings()
        app = create_app(settings=settings)
        app.dependency_overrides[get_runtime] = lambda: runtime
        client = TestClient(app)

        # 1. 创建
        resp = client.post(
            f"{settings.api_prefix}/confirmations",
            json={
                "run_id": "r1", "thread_id": "t1", "project_id": "p1",
                "principal": "MAIN_AGENT",
                "tool_name": "mcp__echo__echo",
                "arguments": {"m": "hi"},
                "ttl_seconds": 60.0,
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        rid = resp.json()["request_id"]

        # 2. 批准
        resp = client.post(
            f"{settings.api_prefix}/confirmations/{rid}/approve",
            json={"decided_by": "user"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["state"], "approved")

        # 3. 执行（上下文完全匹配）
        resp = client.post(
            f"{settings.api_prefix}/confirmations/{rid}/execute",
            json={
                "run_id": "r1", "thread_id": "t1",
                "tool_name": "mcp__echo__echo",
                "arguments": {"m": "hi"},
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["state"], "executed")

    def test_execute_with_modified_args_returns_409(self):
        """API 层：参数被替换 → 409。"""
        from fastapi.testclient import TestClient
        from mini_deerflow.api.app import create_app
        from mini_deerflow.api.config import ApiSettings
        from mini_deerflow.api.dependencies import get_runtime

        runtime = self._build_runtime()
        settings = ApiSettings()
        app = create_app(settings=settings)
        app.dependency_overrides[get_runtime] = lambda: runtime
        client = TestClient(app)

        # 创建并批准
        resp = client.post(
            f"{settings.api_prefix}/confirmations",
            json={
                "run_id": "r1", "thread_id": "t1", "project_id": "p1",
                "principal": "MAIN_AGENT",
                "tool_name": "mcp__echo__echo",
                "arguments": {"m": "hi"},
                "ttl_seconds": 60.0,
            },
        )
        rid = resp.json()["request_id"]
        client.post(
            f"{settings.api_prefix}/confirmations/{rid}/approve",
            json={"decided_by": "user"},
        )

        # 尝试用修改后的参数执行
        resp = client.post(
            f"{settings.api_prefix}/confirmations/{rid}/execute",
            json={
                "run_id": "r1", "thread_id": "t1",
                "tool_name": "mcp__echo__echo",
                "arguments": {"m": "hacked"},  # 修改
            },
        )
        self.assertEqual(resp.status_code, 409, resp.text)
        body = resp.json()
        # 自定义错误响应格式
        msg = body.get("detail") or body.get("error", {}).get("message", "")
        self.assertIn("参数不匹配", msg)


if __name__ == "__main__":
    unittest.main()
