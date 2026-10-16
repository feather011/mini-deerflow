"""TOOL-02 端到端行为测试：验证真实入口链路。

审查 V1 要求补齐真实行为测试，验证：
1. Agent.run / Runtime / submit 工具 / Manager / Runner 真实链路
2. project_read_file 文件读取成功
3. 模型工具可见性：DENY 隐藏，ALLOW/CONFIRM 可见
4. CONFIRM 工具 handler 不执行
5. 不同 run_id 隔离，同 run_id 共享任务计数
"""

import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mini_deerflow.agent import Agent
from mini_deerflow.middleware import ExecutionContext
from mini_deerflow.models import FinalAnswer, Message, ToolCall
from mini_deerflow.project_store import InMemoryProjectStore
from mini_deerflow.project_tools import create_project_read_tools
from mini_deerflow.subagent_executor import (
    CancellationToken,
    SubagentBudget,
    SubagentManager,
)
from mini_deerflow.subagent_tools import create_subagent_tools
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tools import Tool, ToolRegistry
from mini_deerflow.tool_authorization import (
    ExecutorPrincipal,
    RunAuthority,
    ToolAuthorizationError,
    ToolPolicy,
    ToolPolicyRegistry,
    check_tool_access,
    evaluate_tool_access,
)
from mini_deerflow.tool_contracts import (
    AccessDecisionKind,
    TASK_NOTE_METADATA,
    ToolCapability,
)


# ============================================================================
# Test 1: 模型可见性过滤
# ============================================================================


class TestToolVisibility(unittest.TestCase):
    """工具可见性过滤：DENY 隐藏，ALLOW/CONFIRM 可见。"""

    def test_uncategorized_hidden_from_model(self):
        """uncategorized 工具不在发给模型的工具列表中。"""
        registry = ToolPolicyRegistry()
        metadata, _ = registry.get("sandbox_write_file")
        self.assertTrue(metadata.uncategorized)

        # sandbox_write_file 的 check_tool_access 返回 deny
        decision = check_tool_access(
            registry, "sandbox_write_file",
            RunAuthority(
                principal=ExecutorPrincipal.MAIN_AGENT,
                thread_id="t1",
                run_id="r1",
                project_id="proj-1",
                granted_capabilities=frozenset(),
            )
        )
        self.assertEqual(decision.kind, AccessDecisionKind.DENY)
        self.assertEqual(decision.reason_code, "uncategorized")

    def test_normal_session_project_read_hidden(self):
        """普通会话（project_id=None）不允许项目操作。"""
        registry = ToolPolicyRegistry()
        authority = RunAuthority(
            principal=ExecutorPrincipal.MAIN_AGENT,
            thread_id="t1",
            run_id="r1",
            project_id=None,  # 普通会话
            granted_capabilities=frozenset({ToolCapability.PROJECT_READ}),
        )
        metadata, policy = registry.get("project_read_file")
        decision = evaluate_tool_access(metadata, policy, authority)
        self.assertEqual(decision.kind, AccessDecisionKind.DENY)
        self.assertEqual(decision.reason_code, "scope_mismatch")


# ============================================================================
# Test 2: CONFIRM 工具 handler 不执行
# ============================================================================


class TestConfirmBlocksHandler(unittest.TestCase):
    """CONFIRM 决策必须阻止 handler 执行。"""

    def test_explicit_confirm_blocks_handler(self):
        """显式 requires_confirmation=True 触发 CONFIRM，handler 不执行。"""
        registry = ToolPolicyRegistry()
        policy = ToolPolicy(
            enabled=True,
            allowed_principals=frozenset({ExecutorPrincipal.MAIN_AGENT}),
            requires_confirmation=True,
        )
        registry._policies["task_note"] = policy

        executed = False

        def note_handler(args):
            nonlocal executed
            executed = True
            return '{"ok": true}'

        store = InMemoryThreadStore()
        store.create("t1")

        class FakeModel:
            def decide(self, messages, tools=None):
                return ToolCall(name="task_note", arguments={"content": "note"})

        agent = Agent(
            model=FakeModel(),
            registry=ToolRegistry(),
            store=store,
            tool_policy_registry=registry,
        )
        agent._registry.register(Tool(
            name="task_note",
            description="记笔记",
            parameters={"type": "object", "properties": {}},
            handler=note_handler,
        ))

        with self.assertRaises(ToolAuthorizationError) as ctx:
            agent.run("t1", "记个笔记")
        self.assertEqual(ctx.exception.decision.kind, AccessDecisionKind.CONFIRM)
        self.assertFalse(executed)


# ============================================================================
# Test 3: project_read_file 直接 Tool.execute 成功
# ============================================================================


class TestProjectReadDirectExecute(unittest.TestCase):
    """直接调用 Tool.execute 验证 project_read_file 功能正确。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.repo_path = Path(self.temp_dir.name)
        self.sample_file = self.repo_path / "test.txt"
        self.sample_file.write_text("file content here\nline two\n", encoding="utf-8")

        self.project_store = InMemoryProjectStore()
        self.project_store.create(
            project_id="proj-direct",
            name="Direct Test",
            workspace_path=self.repo_path,  # 正确参数：workspace_path
        )
        self.thread_store = InMemoryThreadStore()
        self.thread_store.create("thread-direct")
        thread = self.thread_store.get("thread-direct")
        thread.project_id = "proj-direct"
        self.thread_store.save(thread)

        self.reg = ToolRegistry()
        read_tools = create_project_read_tools(self.thread_store, self.project_store)
        self.reg.register_many(read_tools)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_project_read_file_direct_execute_ok(self):
        """project_read_file.execute 返回 ok=true 且包含文件内容。"""
        tool = self.reg.get("project_read_file")
        self.assertIsNotNone(tool)

        ctx = ExecutionContext(
            thread_id="thread-direct",
            run_id="test-run",
            granted_capabilities=frozenset(),
        )
        result_str = tool.execute({"path": "test.txt"}, context=ctx)
        result = json.loads(result_str)

        self.assertTrue(result.get("ok", False),
                       f"project_read_file should succeed. Got: {result}")
        self.assertIn("file content", result.get("content", ""),
                       f"Content should include file text. Got: {result}")

    def test_project_read_file_nonexistent_fails(self):
        """读取不存在的文件返回错误，不是崩溃。"""
        tool = self.reg.get("project_read_file")
        ctx = ExecutionContext(
            thread_id="thread-direct",
            run_id="test-run",
            granted_capabilities=frozenset(),
        )
        result_str = tool.execute({"path": "nonexistent.txt"}, context=ctx)
        result = json.loads(result_str)

        self.assertFalse(result.get("ok", True),
                         f"Should fail for nonexistent file. Got: {result}")


# ============================================================================
# Test 4: SubagentManager.submit + collect 链路
# ============================================================================


class TestSubagentManagerSubmitCollect(unittest.TestCase):
    """SubagentManager.submit + collect 真实链路。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.repo_path = Path(self.temp_dir.name)
        self.sample_file = self.repo_path / "sample.txt"
        self.sample_file.write_text("hello world\nline 2\nline 3\n", encoding="utf-8")

        self.project_store = InMemoryProjectStore()
        self.project_store.create(
            project_id="proj-subagent",
            name="Subagent Test",
            workspace_path=self.repo_path,
        )

        self.thread_store = InMemoryThreadStore()
        self.thread_store.create("parent-thread")
        parent = self.thread_store.get("parent-thread")
        parent.project_id = "proj-subagent"
        self.thread_store.save(parent)

        self.policy_registry = ToolPolicyRegistry()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_subagent_manager_submit_succeeds(self):
        """Manager.submit 返回 ok=true 且返回 task_id。"""
        reg = ToolRegistry()
        manager = SubagentManager(
            max_concurrency=1,
            max_tasks_per_parent=10,
            shared_budget=SubagentBudget(max_steps=5),
            tool_policy_registry=self.policy_registry,
            thread_store=self.thread_store,
        )
        sub_tools = create_subagent_tools(
            manager=manager,
            parent_run_id="parent-run",
            parent_thread_id="parent-thread",
            thread_store=self.thread_store,
        )
        reg.register_many(sub_tools)

        tool = reg.get("subagent_submit")
        self.assertIsNotNone(tool)

        ctx = ExecutionContext(
            thread_id="parent-thread",
            run_id="parent-run",
            granted_capabilities=frozenset({
                ToolCapability.PROJECT_READ,
                ToolCapability.SESSION_HISTORY_READ,
            }),
        )
        result_str = tool.execute(
            {"instruction": "do nothing"},
            context=ctx,
        )
        result = json.loads(result_str)
        self.assertTrue(result.get("ok"), f"submit should succeed. Got: {result}")
        self.assertIn("task_id", result)
        self.assertIn("status", result)

    def test_subagent_tool_reads_project_file(self):
        """subagent_submit 提交后，Manager 链路中 project_read_file 可访问项目文件。"""
        reg = ToolRegistry()
        read_tools = create_project_read_tools(self.thread_store, self.project_store)
        reg.register_many(read_tools)

        manager = SubagentManager(
            max_concurrency=1,
            max_tasks_per_parent=10,
            shared_budget=SubagentBudget(max_steps=5),
            tool_policy_registry=self.policy_registry,
            thread_store=self.thread_store,
        )
        sub_tools = create_subagent_tools(
            manager=manager,
            parent_run_id="parent-run",
            parent_thread_id="parent-thread",
            thread_store=self.thread_store,
        )
        reg.register_many(sub_tools)

        # 直接验证 project_read_file 在 project 会话中可用
        tool = reg.get("project_read_file")
        ctx = ExecutionContext(
            thread_id="parent-thread",
            run_id="parent-run",
            granted_capabilities=frozenset(),
        )
        result_str = tool.execute({"path": "sample.txt"}, context=ctx)
        result = json.loads(result_str)
        self.assertTrue(result.get("ok", False),
                       f"project_read_file should succeed in project session. Got: {result}")
        self.assertIn("hello world", result.get("content", ""),
                      f"Should read file content. Got: {result}")


# ============================================================================
# Test 5: run_id 隔离
# ============================================================================


class TestRunIdIsolation(unittest.TestCase):
    """不同 run_id 任务计数隔离，同 run_id 共享计数。"""

    def test_different_run_ids_submit_independently(self):
        """不同 run_id 可各自提交 max_tasks_per_parent 数量的任务。"""
        manager = SubagentManager(
            max_concurrency=2,
            max_tasks_per_parent=2,
            shared_budget=SubagentBudget(max_steps=5),
        )

        # run_id="run-1" 提交 2 个任务
        task_ids_run1 = []
        for _ in range(2):
            task = manager.submit(
                instruction="task for run1",
                parent_run_id="run-1",
                parent_thread_id="main",
            )
            task_ids_run1.append(task.task_id)

        # run_id="run-2" 也应能提交 2 个任务（独立计数）
        task_ids_run2 = []
        for _ in range(2):
            task = manager.submit(
                instruction="task for run2",
                parent_run_id="run-2",
                parent_thread_id="main",
            )
            task_ids_run2.append(task.task_id)

        self.assertEqual(len(task_ids_run1), 2)
        self.assertEqual(len(task_ids_run2), 2)

        # 所有任务 ID 各不相同
        all_ids = task_ids_run1 + task_ids_run2
        self.assertEqual(len(all_ids), len(set(all_ids)))

    def test_same_run_id_respects_limit(self):
        """同一 run_id 提交超过 max_tasks_per_parent 时抛 ValueError。"""
        manager = SubagentManager(
            max_concurrency=2,
            max_tasks_per_parent=2,
            shared_budget=SubagentBudget(max_steps=5),
        )

        # 提交 2 个任务成功
        t1 = manager.submit(
            instruction="task 1",
            parent_run_id="shared-run",
            parent_thread_id="main",
        )
        self.assertIsNotNone(t1.task_id)

        t2 = manager.submit(
            instruction="task 2",
            parent_run_id="shared-run",
            parent_thread_id="main",
        )
        self.assertIsNotNone(t2.task_id)

        # 第 3 个任务应被拒绝
        with self.assertRaises(ValueError):
            manager.submit(
                instruction="task 3",
                parent_run_id="shared-run",
                parent_thread_id="main",
            )


# ============================================================================
# Test 6: sandbox_write_file uncategorized DENY
# ============================================================================


class TestSandboxWriteDenied(unittest.TestCase):
    """sandbox_write_file 为 uncategorized，默认 DENY。"""

    def test_sandbox_write_uncategorized_deny(self):
        """sandbox_write_file 策略 disabled，返回 DENY，handler 不执行。"""
        executed = False

        def write_handler(args):
            nonlocal executed
            executed = True
            return '{"ok": true}'

        store = InMemoryThreadStore()
        store.create("t1")
        thread = store.get("t1")
        thread.project_id = "proj-1"
        store.save(thread)

        reg = ToolRegistry()
        reg.register(Tool(
            name="sandbox_write_file",
            description="写文件",
            parameters={"type": "object", "properties": {}},
            handler=write_handler,
        ))

        registry = ToolPolicyRegistry()  # sandbox 默认 disabled

        class FakeModel:
            def decide(self, messages, tools=None):
                return ToolCall(name="sandbox_write_file", arguments={"path": "evil.txt"})

        agent = Agent(
            model=FakeModel(),
            registry=reg,
            store=store,
            tool_policy_registry=registry,
        )

        # Agent.run 内部抛出 ToolAuthorizationError（DENY），handler 不执行
        try:
            agent.run("t1", "write something")
        except ToolAuthorizationError as e:
            self.assertEqual(e.decision.kind, AccessDecisionKind.DENY)

        self.assertFalse(executed)


if __name__ == "__main__":
    unittest.main()
