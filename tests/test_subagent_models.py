"""Subagent 领域模型测试（17A 阶段）。"""

from __future__ import annotations

import time
import unittest

from mini_deerflow.subagent_models import (
    InvalidTransitionError,
    SCHEMA_VERSION,
    SubagentContextPolicy,
    SubagentResult,
    SubagentTask,
    TaskStatus,
)


def _make_task(
    task_id: str = "task-1",
    status: TaskStatus = TaskStatus.QUEUED,
    **kwargs,
) -> SubagentTask:
    """创建测试用任务。"""
    return SubagentTask(
        task_id=task_id,
        parent_run_id="run-0",
        parent_thread_id="main",
        instruction="do something",
        status=status,
        created_at=time.monotonic(),
        **kwargs,
    )


class TestTaskStatusTransitions(unittest.TestCase):
    """状态转换合法性测试。"""

    # --- 合法转换 ---
    def test_queued_to_running(self):
        t = _make_task()
        t.transition_to(TaskStatus.RUNNING)
        self.assertEqual(t.status, TaskStatus.RUNNING)

    def test_queued_to_cancelled(self):
        t = _make_task()
        t.transition_to(TaskStatus.CANCELLED)
        self.assertEqual(t.status, TaskStatus.CANCELLED)
        self.assertIsNotNone(t.finished_at)

    def test_running_to_succeeded(self):
        t = _make_task(status=TaskStatus.RUNNING)
        t.transition_to(TaskStatus.SUCCEEDED)
        self.assertEqual(t.status, TaskStatus.SUCCEEDED)

    def test_running_to_failed(self):
        t = _make_task(status=TaskStatus.RUNNING)
        t.transition_to(TaskStatus.FAILED)
        self.assertEqual(t.status, TaskStatus.FAILED)

    def test_running_to_cancelled(self):
        t = _make_task(status=TaskStatus.RUNNING)
        t.transition_to(TaskStatus.CANCELLED)
        self.assertEqual(t.status, TaskStatus.CANCELLED)

    def test_running_to_timed_out(self):
        t = _make_task(status=TaskStatus.RUNNING)
        t.transition_to(TaskStatus.TIMED_OUT)
        self.assertEqual(t.status, TaskStatus.TIMED_OUT)

    # --- 非法转换 ---
    def test_queued_cannot_go_to_succeeded(self):
        t = _make_task()
        with self.assertRaises(InvalidTransitionError) as ctx:
            t.transition_to(TaskStatus.SUCCEEDED)
        self.assertEqual(ctx.exception.task_id, "task-1")
        self.assertEqual(ctx.exception.from_, TaskStatus.QUEUED)
        self.assertEqual(ctx.exception.to, TaskStatus.SUCCEEDED)

    def test_terminal_cannot_change(self):
        for status in (TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.TIMED_OUT):
            t = _make_task(status=status)
            with self.assertRaises(InvalidTransitionError):
                t.transition_to(TaskStatus.RUNNING)

    def test_running_cannot_go_to_queued(self):
        t = _make_task(status=TaskStatus.RUNNING)
        with self.assertRaises(InvalidTransitionError):
            t.transition_to(TaskStatus.QUEUED)

    def test_succeeded_cannot_go_to_failed(self):
        t = _make_task(status=TaskStatus.SUCCEEDED)
        with self.assertRaises(InvalidTransitionError):
            t.transition_to(TaskStatus.FAILED)

    # --- 时间戳 ---
    def test_started_at_set_on_running(self):
        t = _make_task()
        self.assertIsNone(t.started_at)
        t.transition_to(TaskStatus.RUNNING)
        self.assertIsNotNone(t.started_at)

    def test_finished_at_set_on_terminal(self):
        t = _make_task(status=TaskStatus.RUNNING)
        self.assertIsNone(t.finished_at)
        t.transition_to(TaskStatus.SUCCEEDED)
        self.assertIsNotNone(t.finished_at)


class TestSubagentTaskValidation(unittest.TestCase):
    """任务字段校验测试。"""

    def test_empty_task_id_rejected(self):
        with self.assertRaises(ValueError):
            _make_task(task_id="")

    def test_negative_timeout_rejected(self):
        with self.assertRaises(ValueError):
            _make_task(timeout_seconds=-1)

    def test_started_before_created_rejected(self):
        now = time.monotonic()
        with self.assertRaises(ValueError):
            SubagentTask(
                task_id="t1",
                parent_run_id="run-0",
                parent_thread_id="main",
                instruction="test",
                status=TaskStatus.QUEUED,
                created_at=now,
                started_at=now - 1,
            )

    def test_context_policy_accepted(self):
        policy = SubagentContextPolicy(
            include_memory=True,
            max_child_depth=2,
            allowed_tools=("calculator",),
        )
        t = _make_task(context_policy=policy)
        self.assertEqual(t.context_policy.include_memory, True)
        self.assertEqual(t.context_policy.max_child_depth, 2)


class TestSubagentTaskSerialization(unittest.TestCase):
    """任务序列化与反序列化测试。"""

    def test_roundtrip_minimal(self):
        t = _make_task()
        d = t.to_dict()
        self.assertEqual(d["schema_version"], SCHEMA_VERSION)
        restored = SubagentTask.from_dict(d)
        self.assertEqual(restored.task_id, t.task_id)
        self.assertEqual(restored.status, t.status)
        self.assertEqual(restored.instruction, t.instruction)

    def test_roundtrip_full(self):
        now = time.monotonic()
        t = SubagentTask(
            task_id="full-task",
            parent_run_id="run-1",
            parent_thread_id="thread-1",
            instruction="复杂指令",
            status=TaskStatus.RUNNING,
            created_at=now,
            parent_task_id="parent-1",
            depth=2,
            timeout_seconds=60.0,
            started_at=now + 0.1,
            context_policy=SubagentContextPolicy(
                include_memory=True,
                max_child_depth=1,
                allowed_tools=None,
            ),
            metadata={"key": "value"},
        )
        d = t.to_dict()
        restored = SubagentTask.from_dict(d)
        self.assertEqual(restored.task_id, "full-task")
        self.assertEqual(restored.parent_task_id, "parent-1")
        self.assertEqual(restored.depth, 2)
        self.assertEqual(restored.timeout_seconds, 60.0)
        self.assertEqual(restored.context_policy.include_memory, True)
        self.assertEqual(restored.metadata["key"], "value")

    def test_invalid_schema_version(self):
        d = {
            "schema_version": 999,
            "task_id": "t1",
            "parent_run_id": "run-0",
            "parent_thread_id": "main",
            "instruction": "test",
            "status": "QUEUED",
            "created_at": time.monotonic(),
        }
        with self.assertRaises(ValueError):
            SubagentTask.from_dict(d)

    def test_missing_required_field(self):
        d = {
            "schema_version": SCHEMA_VERSION,
            "task_id": "t1",
            # missing parent_run_id
            "parent_thread_id": "main",
            "instruction": "test",
            "status": "QUEUED",
            "created_at": time.monotonic(),
        }
        with self.assertRaises(ValueError):
            SubagentTask.from_dict(d)


class TestSubagentResult(unittest.TestCase):
    """SubagentResult 测试。"""

    def test_succeeded_result(self):
        now = time.monotonic()
        result = SubagentResult(
            task_id="task-1",
            terminal_status=TaskStatus.SUCCEEDED,
            output="42",
            error_type=None,
            error_message=None,
            started_at=now,
            finished_at=now + 5.0,
        )
        self.assertTrue(result.succeeded)
        self.assertEqual(result.duration, 5.0)

    def test_failed_result(self):
        now = time.monotonic()
        result = SubagentResult(
            task_id="task-1",
            terminal_status=TaskStatus.FAILED,
            output="",
            error_type="RuntimeError",
            error_message="boom",
            started_at=now,
            finished_at=now + 2.0,
        )
        self.assertFalse(result.succeeded)
        self.assertEqual(result.duration, 2.0)
        self.assertEqual(result.error_type, "RuntimeError")

    def test_non_terminal_status_rejected(self):
        now = time.monotonic()
        with self.assertRaises(ValueError):
            SubagentResult(
                task_id="task-1",
                terminal_status=TaskStatus.RUNNING,
                output="",
                error_type=None,
                error_message=None,
                started_at=now,
                finished_at=now + 1.0,
            )

    def test_from_task_succeeded(self):
        t = _make_task(status=TaskStatus.SUCCEEDED)
        t.started_at = 100.0
        t.finished_at = 105.0
        result = SubagentResult.from_task(t, output="done")
        self.assertEqual(result.task_id, t.task_id)
        self.assertTrue(result.succeeded)
        self.assertEqual(result.duration, 5.0)

    def test_from_task_failed_with_error(self):
        t = _make_task(status=TaskStatus.FAILED)
        t.started_at = 100.0
        t.finished_at = 102.0
        result = SubagentResult.from_task(
            t,
            output="",
            error_type="ValueError",
            error_message="bad input",
        )
        self.assertFalse(result.succeeded)
        self.assertEqual(result.error_type, "ValueError")

    def test_roundtrip(self):
        now = time.monotonic()
        result = SubagentResult(
            task_id="result-1",
            terminal_status=TaskStatus.SUCCEEDED,
            output="hello",
            error_type=None,
            error_message=None,
            started_at=now,
            finished_at=now + 3.0,
            run_id="run-1",
        )
        d = result.to_dict()
        restored = SubagentResult.from_dict(d)
        self.assertEqual(restored.task_id, result.task_id)
        self.assertEqual(restored.output, result.output)
        self.assertEqual(restored.run_id, result.run_id)


class TestContextPolicy(unittest.TestCase):
    """上下文策略测试。"""

    def test_defaults(self):
        policy = SubagentContextPolicy()
        self.assertFalse(policy.include_memory)
        self.assertFalse(policy.include_skill_context)
        self.assertFalse(policy.include_system_instruction)
        self.assertIsNone(policy.allowed_tools)
        self.assertEqual(policy.max_child_depth, 0)
        self.assertFalse(policy.allow_further_delegation)

    def test_explicit_values(self):
        policy = SubagentContextPolicy(
            include_memory=True,
            include_skill_context=True,
            include_system_instruction=True,
            allowed_tools=("calc", "read"),
            max_child_depth=3,
            allow_further_delegation=True,
        )
        self.assertTrue(policy.include_memory)
        self.assertEqual(policy.allowed_tools, ("calc", "read"))
        self.assertEqual(policy.max_child_depth, 3)


if __name__ == "__main__":
    unittest.main()
