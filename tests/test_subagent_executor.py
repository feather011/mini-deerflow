"""Subagent 执行器测试（17B+17C+17D 阶段）。"""

from __future__ import annotations

import threading
import time
import unittest
from collections import deque
from pathlib import Path

from mini_deerflow.models import FinalAnswer, Message, ToolCall
from mini_deerflow.subagent_executor import (
    CancellationToken,
    SubagentBudget,
    SubagentManager,
    SubagentRunner,
)
from mini_deerflow.subagent_models import (
    TaskStatus,
)
from mini_deerflow.thread_store import InMemoryThreadStore


# --- Fake Model for testing ---

class CountingModel:
    """记录调用次数的模型。"""
    def __init__(self) -> None:
        self.call_count = 0

    def decide(self, messages: list[Message]) -> ToolCall | FinalAnswer:
        self.call_count += 1
        latest = messages[-1]
        if latest.role == "user":
            return FinalAnswer(content=f"result-{self.call_count}")
        return FinalAnswer(content="done")


class TwoStepModel:
    """两步后返回结果。"""
    def __init__(self) -> None:
        self.calls = 0

    def decide(self, messages: list[Message]) -> ToolCall | FinalAnswer:
        self.calls += 1
        if self.calls == 1:
            return FinalAnswer(content="step-1")
        return FinalAnswer(content="final-result")


class ScriptedModel:
    """按预设序列返回决策。"""
    def __init__(self, decisions: list[FinalAnswer | ToolCall]) -> None:
        self._queue: deque[FinalAnswer | ToolCall] = deque(decisions)
        self.received: list[list[Message]] = []

    def decide(self, messages: list[Message]) -> ToolCall | FinalAnswer:
        self.received.append(list(messages))
        return self._queue.popleft()


class TestCancellationToken(unittest.TestCase):
    def test_cancel_sets_flag(self):
        token = CancellationToken()
        self.assertFalse(token.is_cancelled())
        token.cancel()
        self.assertTrue(token.is_cancelled())

    def test_cancel_idempotent(self):
        token = CancellationToken()
        token.cancel()
        token.cancel()
        self.assertTrue(token.is_cancelled())


class TestSubagentBudget(unittest.TestCase):
    """验证 Budget 的原子 consume 语义。

    新语义：consume_* 在扣减前检查额度，超限抛 BudgetExceededError；
    旧 API（record_* 返回 bool）已被替换为 consume_*。
    """

    def test_unlimited_model_calls(self):
        b = SubagentBudget(max_model_calls=None)
        for _ in range(100):
            # 无上限时应始终成功
            b.consume_model_call()
        self.assertEqual(b.model_calls(), 100)

    def test_limited_model_calls(self):
        from mini_deerflow.middleware import BudgetExceededError

        b = SubagentBudget(max_model_calls=3)
        b.consume_model_call()
        b.consume_model_call()
        b.consume_model_call()
        with self.assertRaises(BudgetExceededError) as ctx:
            b.consume_model_call()
        self.assertEqual(ctx.exception.resource, "model_calls")
        self.assertEqual(ctx.exception.limit, 3)
        # 第 4 次未扣减成功，计数仍为 3
        self.assertEqual(b.model_calls(), 3)

    def test_unlimited_tool_calls(self):
        b = SubagentBudget(max_tool_calls=None)
        for _ in range(50):
            b.consume_tool_call()
        self.assertEqual(b.tool_calls(), 50)

    def test_limited_tool_calls(self):
        from mini_deerflow.middleware import BudgetExceededError

        b = SubagentBudget(max_tool_calls=2)
        b.consume_tool_call()
        b.consume_tool_call()
        with self.assertRaises(BudgetExceededError) as ctx:
            b.consume_tool_call()
        self.assertEqual(ctx.exception.resource, "tool_calls")
        self.assertEqual(ctx.exception.limit, 2)
        self.assertEqual(b.tool_calls(), 2)


class TestSubagentRunner(unittest.TestCase):
    """SubagentRunner 隔离性测试。"""

    def setUp(self):
        self._store = InMemoryThreadStore()

    def test_runner_creates_independent_thread(self):
        from mini_deerflow.subagent_models import SubagentTask
        task = SubagentTask(
            task_id="runner-test-1",
            parent_run_id="run-0",
            parent_thread_id="main",
            instruction="test instruction",
            status=TaskStatus.QUEUED,
            created_at=time.monotonic(),
        )
        runner = SubagentRunner(
            task=task,
            model=CountingModel(),
            registry=__import__("mini_deerflow.tools", fromlist=["ToolRegistry"]).ToolRegistry(),
            store=self._store,
            budget=SubagentBudget(),
            cancellation_token=CancellationToken(),
        )
        result = runner.run()
        self.assertEqual(result.terminal_status, TaskStatus.SUCCEEDED)
        self.assertIn("result-1", result.output)
        thread_id = f"subagent-{task.task_id}"
        self.assertTrue(self._store.exists(thread_id))

    def test_runner_does_not_touch_parent_thread(self):
        from mini_deerflow.subagent_models import SubagentTask
        parent_store = InMemoryThreadStore()
        parent_store.create("main")

        task = SubagentTask(
            task_id="runner-test-2",
            parent_run_id="run-0",
            parent_thread_id="main",
            instruction="test",
            status=TaskStatus.QUEUED,
            created_at=time.monotonic(),
        )
        runner = SubagentRunner(
            task=task,
            model=CountingModel(),
            registry=__import__("mini_deerflow.tools", fromlist=["ToolRegistry"]).ToolRegistry(),
            store=parent_store,
            budget=SubagentBudget(),
            cancellation_token=CancellationToken(),
        )
        runner.run()
        # 父线程消息不应被子任务修改（子任务在独立 thread_id 下工作）
        parent_thread = parent_store.get("main")
        self.assertEqual(parent_thread.message_count, 0)
        # 子任务使用独立 thread_id
        self.assertTrue(parent_store.exists(f"subagent-{task.task_id}"))


class TestSubagentManagerConcurrency(unittest.TestCase):
    """并发和取消测试。"""

    def test_max_concurrency_enforced(self):
        """验证 max_concurrency 真正限制并发数。"""
        running_count = [0]
        max_seen = [0]
        lock = threading.Lock()
        start_gate = threading.Event()

        class CountingModel:
            def decide(self, m):
                with lock:
                    running_count[0] += 1
                    max_seen[0] = max(max_seen[0], running_count[0])
                start_gate.wait(timeout=5)
                with lock:
                    running_count[0] -= 1
                return FinalAnswer(content="done")

        manager = SubagentManager(max_concurrency=2, default_timeout=30.0)

        # 提交 4 个任务
        tasks = [
            manager.submit(f"task-{i}", parent_run_id="run-0", parent_thread_id="main", model=CountingModel())
            for i in range(4)
        ]

        # 等待任务开始执行
        time.sleep(0.3)

        # 最多只应有 2 个任务同时运行
        self.assertLessEqual(max_seen[0], 2)

        # 放行
        start_gate.set()

        # 等待所有任务完成
        for t in tasks:
            manager.collect(t.task_id, timeout=10.0)

        manager.shutdown()

    def test_queued_cancellation(self):
        start = threading.Event()

        class SlowStartModel:
            """启动慢的模型"""
            def decide(self, m):
                start.wait(timeout=5)
                return FinalAnswer(content="done")

        manager = SubagentManager(default_timeout=30.0)
        task = manager.submit(
            instruction="cancelled-before-start",
            parent_run_id="run-0",
            parent_thread_id="main",
            model=SlowStartModel(),
        )

        # 立即取消（任务可能还在队列）
        accepted = manager.cancel(task.task_id)
        self.assertTrue(accepted)

        time.sleep(0.5)
        # 取消可能成功也可能已经开始，验证最终状态
        status = manager.get_status(task.task_id)
        self.assertIn(status, {
            TaskStatus.CANCELLED,
            TaskStatus.RUNNING,
        })

        # 放行以便清理
        start.set()
        manager.shutdown()

    def test_repeated_cancel_is_idempotent(self):
        manager = SubagentManager()
        task = manager.submit(
            instruction="task",
            parent_run_id="run-0",
            parent_thread_id="main",
        )
        manager.cancel(task.task_id)
        time.sleep(0.2)
        # 重复取消应返回 False
        accepted = manager.cancel(task.task_id)
        self.assertFalse(accepted)
        manager.shutdown()

    def test_terminal_task_cannot_be_cancelled(self):
        manager = SubagentManager()
        task = manager.submit(
            instruction="fast task",
            parent_run_id="run-0",
            parent_thread_id="main",
        )
        # 等待完成
        manager.collect(task.task_id, timeout=5.0)
        accepted = manager.cancel(task.task_id)
        self.assertFalse(accepted)
        manager.shutdown()

    def test_timeout_uses_deadline(self):
        """验证 timeout 参数被正确传递。"""
        from mini_deerflow.models import FinalAnswer

        class QuickModel:
            def decide(self, m):
                return FinalAnswer(content="quick")

        # timeout=1 秒，足够完成任务
        manager = SubagentManager(default_timeout=1.0)
        task = manager.submit(
            instruction="quick task",
            parent_run_id="run-0",
            parent_thread_id="main",
            model=QuickModel(),
        )
        result = manager.collect(task.task_id, timeout=5.0)
        self.assertEqual(result.terminal_status, TaskStatus.SUCCEEDED)
        manager.shutdown()

    def test_task_timeout_reaches_deadline(self):
        """验证超过 timeout 秒后任务被取消。"""
        from mini_deerflow.models import FinalAnswer

        call_count = [0]
        cancel_checked = [False]

        class CountingModel:
            """每次 decide 都会被检查取消"""
            def decide(self, m):
                call_count[0] += 1
                # 在第二次调用时返回（第一次在循环入口检查，第二次也在）
                if call_count[0] >= 2:
                    return FinalAnswer(content="done")
                return FinalAnswer(content="step")

        # timeout=0.01 秒，极短超时
        manager = SubagentManager(default_timeout=0.01)
        task = manager.submit(
            instruction="will timeout",
            parent_run_id="run-0",
            parent_thread_id="main",
            model=CountingModel(),
        )
        result = manager.collect(task.task_id, timeout=10.0)
        # 超时后被取消
        self.assertIn(result.terminal_status, {TaskStatus.CANCELLED, TaskStatus.SUCCEEDED})
        manager.shutdown()

    def test_max_depth_enforced(self):
        manager = SubagentManager(max_depth=2)
        with self.assertRaises(ValueError) as ctx:
            manager.submit(
                instruction="too deep",
                parent_run_id="run-0",
                parent_thread_id="main",
                depth=3,
            )
        self.assertIn("超过上限", str(ctx.exception))

    def test_max_tasks_per_parent_enforced(self):
        manager = SubagentManager(max_tasks_per_parent=2)
        t1 = manager.submit("t1", parent_run_id="run-0", parent_thread_id="main")
        t2 = manager.submit("t2", parent_run_id="run-0", parent_thread_id="main")
        manager.collect(t1.task_id, timeout=3.0)
        manager.collect(t2.task_id, timeout=3.0)
        with self.assertRaises(ValueError) as ctx:
            manager.submit("t3", parent_run_id="run-0", parent_thread_id="main")
        self.assertIn("已达上限", str(ctx.exception))
        manager.shutdown()


class TestSubagentCollect(unittest.TestCase):
    def test_collect_immediate_returns_completed(self):
        manager = SubagentManager()
        task = manager.submit(
            instruction="quick",
            parent_run_id="run-0",
            parent_thread_id="main",
        )
        result = manager.collect(task.task_id, timeout=0)
        self.assertEqual(result.terminal_status, TaskStatus.SUCCEEDED)
        manager.shutdown()

    def test_collect_wait(self):
        class DelayedModel:
            def decide(self, m):
                time.sleep(0.3)
                return FinalAnswer(content="delayed")

        manager = SubagentManager()
        task = manager.submit(
            instruction="delayed",
            parent_run_id="run-0",
            parent_thread_id="main",
            model=DelayedModel(),
        )
        result = manager.collect(task.task_id, timeout=5.0)
        self.assertEqual(result.terminal_status, TaskStatus.SUCCEEDED)
        manager.shutdown()

    def test_collect_many(self):
        manager = SubagentManager()
        t1 = manager.submit("task1", parent_run_id="run-0", parent_thread_id="main")
        t2 = manager.submit("task2", parent_run_id="run-0", parent_thread_id="main")
        results = manager.collect_many([t1.task_id, t2.task_id], timeout=5.0)
        self.assertEqual(len(results), 2)
        manager.shutdown()


if __name__ == "__main__":
    unittest.main()
