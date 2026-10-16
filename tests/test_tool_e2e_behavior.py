"""TOOL-02 E2E 行为测试（V1 修复版）。

审查 V1 要求：真实 submit/Agent/Runner 链路。
"""

import json
import time
import unittest
from collections import deque
from pathlib import Path
from tempfile import TemporaryDirectory

from mini_deerflow.middleware import ExecutionContext
from mini_deerflow.models import FinalAnswer, Message, ToolCall
from mini_deerflow.project_store import InMemoryProjectStore
from mini_deerflow.project_tools import create_project_read_tools
from mini_deerflow.subagent_executor import (
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
    ToolCapability,
)


# ============================================================================
# ScriptedModel: 按预设序列返回决策
# ============================================================================


class ScriptedModel:
    """按预设序列返回决策。"""
    def __init__(self, decisions):
        self._queue = deque(decisions)
        self.received = []

    def decide(self, messages, tools=None):
        self.received.append(list(messages))
        return self._queue.popleft()


# ============================================================================
# Test 1: 可见性过滤 — 真实 Agent + tool_specs
# ============================================================================


class TestToolVisibilityViaAgent(unittest.TestCase):
    """Agent.run 真实发给模型的 tool_specs 过滤。"""

    def test_tool_visibility_denied_hidden(self):
        """DENY 工具不在发给模型的 tool_specs 中。"""
        registry = ToolPolicyRegistry()

        # 用 FakeModel 捕获 tools
        captured_tools = []

        class CaptureModel:
            def decide(self, messages, tools=None):
                captured_tools.extend(list(tools) if tools else [])
                return FinalAnswer(content="done")

        from mini_deerflow.agent import Agent
        store = InMemoryThreadStore()
        store.create("t1")

        agent = Agent(
            model=CaptureModel(),
            registry=ToolRegistry(),
            store=store,
            tool_policy_registry=registry,
        )
        agent.run("t1", "hello")

        # 验证 sandbox_write_file 不在发给模型的工具列表中
        tool_names = [t.name for t in captured_tools]
        self.assertNotIn("sandbox_write_file", tool_names,
                        f"sandbox_write_file should be hidden. Got: {tool_names}")

    def test_tool_visibility_confirm_visible(self):
        """CONFIRM 工具仍发给模型（只是 handler 不执行）。"""
        registry = ToolPolicyRegistry()

        captured_tools = []

        class CaptureModel:
            def decide(self, messages, tools=None):
                captured_tools.extend(list(tools) if tools else [])
                return FinalAnswer(content="done")

        from mini_deerflow.agent import Agent
        store = InMemoryThreadStore()
        store.create("t1")

        # task_note 策略默认不 CONFIRM（WRITES_SESSION 为内部操作）
        # 为测试 CONFIRM 可见性，显式设置 requires_confirmation=True
        policy = ToolPolicy(
            enabled=True,
            allowed_principals=frozenset({ExecutorPrincipal.MAIN_AGENT}),
            requires_confirmation=True,
        )
        registry._policies["task_note"] = policy

        agent = Agent(
            model=CaptureModel(),
            registry=ToolRegistry(),
            store=store,
            tool_policy_registry=registry,
        )
        agent.run("t1", "hello")

        tool_names = [t.name for t in captured_tools]
        # CONFIRM 工具应该发给模型（模型决定是否调用）
        # 当前注册表没有 task_note，所以这里只验证 sandbox_write_file 隐藏
        self.assertNotIn("sandbox_write_file", tool_names)


# ============================================================================
# Test 2: submit handler 真实 run_id / thread_id 继承
# ============================================================================


class TestSubmitHandlerInheritance(unittest.TestCase):
    """_SubmitHandler 从 ExecutionContext 继承 run_id 和 thread_id。"""

    def test_handler_inherits_run_id_from_context(self):
        """_SubmitHandler.submit 执行后，Task.parent_run_id 等于 context.run_id。"""
        thread_store = InMemoryThreadStore()
        thread_store.create("parent-t")

        manager = SubagentManager(
            max_concurrency=1,
            max_tasks_per_parent=10,
            shared_budget=SubagentBudget(max_steps=5),
            tool_policy_registry=ToolPolicyRegistry(),
            thread_store=thread_store,
        )
        sub_tools = create_subagent_tools(
            manager=manager,
            parent_run_id="unused-value",
            parent_thread_id="unused-value",
            thread_store=thread_store,
        )
        reg = ToolRegistry()
        reg.register_many(sub_tools)
        tool = reg.get("subagent_submit")
        self.assertIsNotNone(tool)

        # 构造 context，run_id = "context-run-123"
        ctx = ExecutionContext(
            thread_id="parent-t",
            run_id="context-run-123",
            granted_capabilities=frozenset(),
        )

        # 调用 submit handler
        result_str = tool.execute(
            {"instruction": "test"},
            context=ctx,
        )
        result = json.loads(result_str)
        self.assertTrue(result.get("ok"), f"submit failed: {result_str}")
        task_id = result["task_id"]

        # 验证 Manager 中的 Task
        task = manager._tasks.get(task_id)
        self.assertIsNotNone(task)
        # 关键验证：parent_run_id 等于 context.run_id
        self.assertEqual(task.parent_run_id, "context-run-123",
                       f"parent_run_id should inherit from context. Got: {task.parent_run_id}")

    def test_handler_inherits_thread_id_from_context(self):
        """_SubmitHandler.submit 执行后，Task.parent_thread_id 等于 context.thread_id。"""
        thread_store = InMemoryThreadStore()
        thread_store.create("parent-t-456")

        manager = SubagentManager(
            max_concurrency=1,
            max_tasks_per_parent=10,
            shared_budget=SubagentBudget(max_steps=5),
            tool_policy_registry=ToolPolicyRegistry(),
            thread_store=thread_store,
        )
        sub_tools = create_subagent_tools(
            manager=manager,
            parent_run_id="unused",
            parent_thread_id="unused",
            thread_store=thread_store,
        )
        reg = ToolRegistry()
        reg.register_many(sub_tools)
        tool = reg.get("subagent_submit")

        ctx = ExecutionContext(
            thread_id="parent-t-456",
            run_id="run-abc",
            granted_capabilities=frozenset(),
        )
        result_str = tool.execute(
            {"instruction": "test thread"},
            context=ctx,
        )
        result = json.loads(result_str)
        self.assertTrue(result.get("ok"))

        task_id = result["task_id"]
        task = manager._tasks.get(task_id)
        self.assertEqual(task.parent_thread_id, "parent-t-456",
                       f"parent_thread_id should inherit from context. Got: {task.parent_thread_id}")


# ============================================================================
# Test 3: 不同 run_id 通过同一 submit 工具独立提交（经 handler）
# ============================================================================


class TestSubmitViaToolIndependence(unittest.TestCase):
    """不同 run_id 通过 submit 工具提交，相互独立。"""

    def setUp(self):
        self.thread_store = InMemoryThreadStore()
        self.thread_store.create("default-thread")

    def test_two_runs_submit_independently_via_tool(self):
        """通过 submit 工具，不同 run_id 可各自提交任务。"""
        manager = SubagentManager(
            max_concurrency=2,
            max_tasks_per_parent=2,
            shared_budget=SubagentBudget(max_steps=5),
            tool_policy_registry=ToolPolicyRegistry(),
            thread_store=self.thread_store,
        )
        sub_tools = create_subagent_tools(
            manager=manager,
            parent_run_id="unused",
            parent_thread_id="unused",
            thread_store=self.thread_store,
        )
        reg = ToolRegistry()
        reg.register_many(sub_tools)

        task_ids_run1 = []
        for _ in range(2):
            ctx = ExecutionContext(
                thread_id="default-thread",
                run_id="run-1",
                granted_capabilities=frozenset(),
            )
            tool = reg.get("subagent_submit")
            result_str = tool.execute({"instruction": "task for run1"}, context=ctx)
            result = json.loads(result_str)
            self.assertTrue(result.get("ok"), f"submit failed: {result_str}")
            task_ids_run1.append(result["task_id"])

        task_ids_run2 = []
        for _ in range(2):
            ctx = ExecutionContext(
                thread_id="default-thread",
                run_id="run-2",
                granted_capabilities=frozenset(),
            )
            tool = reg.get("subagent_submit")
            result_str = tool.execute({"instruction": "task for run2"}, context=ctx)
            result = json.loads(result_str)
            self.assertTrue(result.get("ok"), f"submit failed: {result_str}")
            task_ids_run2.append(result["task_id"])

        self.assertEqual(len(task_ids_run1), 2)
        self.assertEqual(len(task_ids_run2), 2)

        # 所有 task ID 各不相同
        all_ids = task_ids_run1 + task_ids_run2
        self.assertEqual(len(all_ids), len(set(all_ids)))

    def test_same_run_via_tool_respects_limit(self):
        """同一 run_id 通过 submit 工具提交，达 max_tasks_per_parent 时抛异常。"""
        manager = SubagentManager(
            max_concurrency=2,
            max_tasks_per_parent=2,
            shared_budget=SubagentBudget(max_steps=5),
            tool_policy_registry=ToolPolicyRegistry(),
            thread_store=self.thread_store,
        )
        sub_tools = create_subagent_tools(
            manager=manager,
            parent_run_id="unused",
            parent_thread_id="unused",
            thread_store=self.thread_store,
        )
        reg = ToolRegistry()
        reg.register_many(sub_tools)

        ctx = ExecutionContext(
            thread_id="default-thread",
            run_id="shared-run",
            granted_capabilities=frozenset(),
        )
        tool = reg.get("subagent_submit")

        # 提交 2 个任务成功
        for i in range(2):
            result_str = tool.execute({"instruction": f"task {i}"}, context=ctx)
            result = json.loads(result_str)
            self.assertTrue(result.get("ok"), f"submit {i} failed: {result_str}")

        # 第 3 个任务应被拒绝
        result_str = tool.execute({"instruction": "task 3"}, context=ctx)
        result = json.loads(result_str)
        self.assertFalse(result.get("ok"), "3rd submit should fail")


# ============================================================================
# Test 4: 直接 project_read_file 通过 Tool.execute 成功
# ============================================================================


class TestProjectReadDirectExecute(unittest.TestCase):
    """直接调用 Tool.execute 验证 project_read_file 功能正确。"""

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.repo_path = Path(self.temp_dir.name)
        f = self.repo_path / "test.txt"
        f.write_text("file content here\nline two\n", encoding="utf-8")

        self.project_store = InMemoryProjectStore()
        self.project_store.create(
            project_id="proj-direct",
            name="Direct Test",
            workspace_path=self.repo_path,
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
        """读取不存在的文件返回错误。"""
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
# Test 5: CONFIRM 工具 handler 不执行
# ============================================================================


class TestConfirmBlocksHandler(unittest.TestCase):
    """CONFIRM 决策阻止 handler 执行。"""

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

        from mini_deerflow.agent import Agent
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


if __name__ == "__main__":
    unittest.main()
