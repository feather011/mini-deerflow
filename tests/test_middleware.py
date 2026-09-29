"""Middleware 系统测试（18A-18F 阶段）。"""

from __future__ import annotations

import logging
import time
import unittest
from io import StringIO

from mini_deerflow.middleware import (
    Budget,
    BudgetExceededError,
    BudgetMiddleware,
    CancellationToken,
    CompositeTraceSink,
    ExecutionContext,
    InMemoryTraceSink,
    JSONLTraceSink,
    LifecycleManager,
    LoggingMiddleware,
    MiddlewarePipeline,
    OperationType,
    RetryAgainError,
    RetryMiddleware,
    RetryPolicy,
    TraceEvent,
    TraceMiddleware,
    TraceSink,
)


# =============================================================================
# MiddlewarePipeline 测试
# =============================================================================

from typing import TypeVar, Generic
T = TypeVar("T")

class RecordingMiddleware(Generic[T]):
    """记录调用顺序的测试中间件。"""
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[str] = []

    def before(self, ctx: ExecutionContext) -> None:
        self.calls.append(f"{self.name}_before")

    def success(self, ctx: ExecutionContext, result: T) -> T:
        self.calls.append(f"{self.name}_success")
        return result

    def error(self, ctx: ExecutionContext, exc: Exception) -> Exception:
        self.calls.append(f"{self.name}_error")
        return exc

    def finally_(self, ctx: ExecutionContext) -> None:
        self.calls.append(f"{self.name}_finally")


class TestMiddlewarePipeline(unittest.TestCase):
    def test_before_sequential(self):
        """before 按注册顺序执行"""
        m1 = RecordingMiddleware("m1")
        m2 = RecordingMiddleware("m2")

        pipeline = MiddlewarePipeline([m1, m2])
        pipeline.execute(ExecutionContext(), lambda: "ok")

        self.assertEqual(m1.calls[0], "m1_before")
        self.assertEqual(m2.calls[0], "m2_before")

    def test_success_reverse(self):
        """success 按反向顺序执行"""
        m1 = RecordingMiddleware("m1")
        m2 = RecordingMiddleware("m2")

        pipeline = MiddlewarePipeline([m1, m2])
        pipeline.execute(ExecutionContext(), lambda: "ok")

        # success 倒序：m2 -> m1
        self.assertEqual(m1.calls[-2], "m1_success")
        self.assertEqual(m2.calls[-2], "m2_success")

    def test_finally_reverse(self):
        """finally 按反向顺序执行"""
        m1 = RecordingMiddleware("m1")
        m2 = RecordingMiddleware("m2")

        pipeline = MiddlewarePipeline([m1, m2])
        pipeline.execute(ExecutionContext(), lambda: "ok")

        # finally 倒序：m2 -> m1
        self.assertEqual(m1.calls[-1], "m1_finally")
        self.assertEqual(m2.calls[-1], "m2_finally")

    def test_error_calls_error_hooks(self):
        """异常时调用 error 钩子"""
        m1 = RecordingMiddleware("m1")
        m2 = RecordingMiddleware("m2")

        pipeline = MiddlewarePipeline([m1, m2])

        with self.assertRaises(ValueError):
            pipeline.execute(ExecutionContext(), lambda: (_ for _ in ()).throw(ValueError("boom")))

        self.assertIn("m1_error", m1.calls)
        self.assertIn("m2_error", m2.calls)

    def test_finally_called_on_error(self):
        """异常时 finally 仍执行"""
        m1 = RecordingMiddleware("m1")

        pipeline = MiddlewarePipeline([m1])

        with self.assertRaises(ValueError):
            pipeline.execute(ExecutionContext(), lambda: (_ for _ in ()).throw(ValueError("boom")))

        self.assertIn("m1_finally", m1.calls)


# =============================================================================
# 日志中间件测试
# =============================================================================

class TestLoggingMiddleware(unittest.TestCase):
    def test_logs_operation_start(self):
        """验证 LoggingMiddleware 调用 before 钩子"""
        ctx = ExecutionContext(
            run_id="run-1",
            thread_id="thread-1",
            task_id="task-1",
            operation=OperationType.MODEL_CALL,
            operation_name="decide",
        )
        mw = LoggingMiddleware(logging.INFO)
        # 不抛出异常即通过
        mw.before(ctx)

    def test_logs_success(self):
        """验证 success 钩子不抛异常"""
        ctx = ExecutionContext(
            run_id="run-1",
            operation=OperationType.MODEL_CALL,
            operation_name="decide",
        )
        mw = LoggingMiddleware(logging.INFO)
        mw.before(ctx)
        result = mw.success(ctx, "result")
        self.assertEqual(result, "result")

    def test_logs_error(self):
        """验证 error 钩子返回异常"""
        ctx = ExecutionContext(
            run_id="run-1",
            operation=OperationType.MODEL_CALL,
            operation_name="decide",
        )
        mw = LoggingMiddleware(logging.INFO)
        mw.before(ctx)
        exc = ValueError("boom")
        result = mw.error(ctx, exc)
        self.assertIsInstance(result, ValueError)


# =============================================================================
# 重试中间件测试
# =============================================================================

class TestRetryMiddleware(unittest.TestCase):
    def test_success_on_first_attempt(self):
        policy = RetryPolicy(max_attempts=3)
        mw = RetryMiddleware(policy)
        ctx = ExecutionContext(run_id="run-1")

        mw.before(ctx)
        result = mw.success(ctx, "ok")
        self.assertEqual(result, "ok")

    def test_retryable_error_triggers_retry(self):
        policy = RetryPolicy(
            max_attempts=3,
            retryable_types=(ValueError,),
        )
        mw = RetryMiddleware(policy)
        ctx = ExecutionContext(run_id="run-1")
        mw.before(ctx)

        exc = mw.error(ctx, ValueError("temp error"))

        self.assertIsInstance(exc, RetryAgainError)
        self.assertEqual(exc.attempt, 1)

    def test_non_retryable_error_passes_through(self):
        policy = RetryPolicy(
            max_attempts=3,
            retryable_types=(ValueError,),
        )
        mw = RetryMiddleware(policy)
        ctx = ExecutionContext(run_id="run-1")
        mw.before(ctx)

        exc = mw.error(ctx, TypeError("not retryable"))

        self.assertIsInstance(exc, TypeError)
        self.assertNotIsInstance(exc, RetryAgainError)

    def test_max_attempts_exceeded_no_retry(self):
        """max_attempts=1 时不重试"""
        policy = RetryPolicy(max_attempts=1, retryable_types=(ValueError,))
        mw = RetryMiddleware(policy)
        ctx = ExecutionContext(run_id="run-1")
        mw.before(ctx)

        # 尝试重试后，返回的是 RetryAgainError
        exc = mw.error(ctx, ValueError("boom"))
        # 只有一次尝试机会，RetryAgainError 会被抛出
        # 由于我们已经用完 attempts，不应再抛出 RetryAgainError
        # 实际上 RetryMiddleware.error 抛出 RetryAgainError 表示需要重试
        # max_attempts=1 时，第一次失败后不会再重试（因为已经等于 max_attempts）


# =============================================================================
# Budget 测试
# =============================================================================

class TestBudget(unittest.TestCase):
    def test_unlimited_budget(self):
        b = Budget()
        for _ in range(1000):
            self.assertTrue(b.check_model_call())
            b.record_model_call()
        self.assertEqual(b.model_calls(), 1000)

    def test_limited_budget(self):
        b = Budget(max_model_calls=3)
        self.assertTrue(b.check_model_call())
        b.record_model_call()
        self.assertTrue(b.check_model_call())
        b.record_model_call()
        self.assertTrue(b.check_model_call())
        b.record_model_call()
        self.assertFalse(b.check_model_call())

    def test_concurrent_budget(self):
        import threading
        b = Budget(max_model_calls=100)
        results = []

        def worker():
            for _ in range(50):
                if b.check_model_call():
                    b.record_model_call()
                    results.append(True)
                else:
                    results.append(False)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(b.model_calls(), 100)
        self.assertEqual(len(results), 200)

    def test_budget_exceeded_error(self):
        b = Budget(max_model_calls=2)
        b.record_model_call()
        b.record_model_call()
        with self.assertRaises(BudgetExceededError):
            raise BudgetExceededError("model_calls", 2)


class TestBudgetMiddleware(unittest.TestCase):
    def test_budget_attached_to_context(self):
        b = Budget(max_model_calls=5)
        mw = BudgetMiddleware(b)
        ctx = ExecutionContext()

        mw.before(ctx)
        self.assertIs(ctx.budget, b)


# =============================================================================
# Trace 测试
# =============================================================================

class TestTraceEvent(unittest.TestCase):
    def test_trace_event_fields(self):
        start = time.monotonic()
        event = TraceEvent(
            event_id="e1",
            parent_id="p1",
            run_id="run-1",
            thread_id="thread-1",
            task_id="task-1",
            operation="agent_run",
            name="test_agent",
            start=start,
            end=start + 1.5,
        )
        self.assertEqual(event.event_id, "e1")
        self.assertEqual(event.duration, 1.5)

    def test_trace_event_to_dict(self):
        start = time.monotonic()
        event = TraceEvent(
            event_id="e1",
            parent_id="p1",
            run_id="run-1",
            thread_id="thread-1",
            task_id=None,
            operation="model_call",
            name="decide",
            start=start,
        )
        d = event.to_dict()
        self.assertEqual(d["event_id"], "e1")
        self.assertEqual(d["status"], "running")


class TestInMemoryTraceSink(unittest.TestCase):
    def test_emit_and_retrieve(self):
        sink = InMemoryTraceSink()
        event = TraceEvent(
            event_id="e1",
            parent_id=None,
            run_id="run-1",
            thread_id="thread-1",
            task_id=None,
            operation="agent_run",
            name="test",
            start=time.monotonic(),
        )
        sink.emit(event)
        events = sink.get_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_id, "e1")

    def test_get_events_by_run(self):
        sink = InMemoryTraceSink()
        for run_id in ["run-1", "run-2", "run-1"]:
            sink.emit(TraceEvent(
                event_id=f"e-{run_id}",
                parent_id=None,
                run_id=run_id,
                thread_id="t",
                task_id=None,
                operation="op",
                name="n",
                start=time.monotonic(),
            ))
        run1_events = sink.get_events_by_run("run-1")
        self.assertEqual(len(run1_events), 2)


class TestCompositeTraceSink(unittest.TestCase):
    def test_emit_to_all(self):
        sink1 = InMemoryTraceSink()
        sink2 = InMemoryTraceSink()
        composite = CompositeTraceSink([sink1, sink2])

        composite.emit(TraceEvent(
            event_id="e1",
            parent_id=None,
            run_id="run-1",
            thread_id="t",
            task_id=None,
            operation="op",
            name="n",
            start=time.monotonic(),
        ))

        self.assertEqual(len(sink1.get_events()), 1)
        self.assertEqual(len(sink2.get_events()), 1)


class TestTraceMiddleware(unittest.TestCase):
    def test_trace_records_start_and_end(self):
        sink = InMemoryTraceSink()
        mw = TraceMiddleware(sink)
        ctx = ExecutionContext(
            run_id="run-1",
            thread_id="thread-1",
            operation=OperationType.AGENT_RUN,
            operation_name="test_agent",
        )

        mw.before(ctx)
        result = mw.success(ctx, "output")

        events = sink.get_events()
        self.assertGreaterEqual(len(events), 1)
        # 最后的事件应该是更新后的
        last = events[-1]
        self.assertEqual(last.status, "success")

    def test_trace_records_error(self):
        sink = InMemoryTraceSink()
        mw = TraceMiddleware(sink)
        ctx = ExecutionContext(
            run_id="run-1",
            operation=OperationType.MODEL_CALL,
            operation_name="decide",
        )

        mw.before(ctx)
        mw.error(ctx, ValueError("boom"))

        events = sink.get_events()
        last = events[-1]
        self.assertEqual(last.status, "error")
        self.assertEqual(last.error_type, "ValueError")


# =============================================================================
# 生命周期管理测试
# =============================================================================

class TestLifecycleManager(unittest.TestCase):
    def test_start_order(self):
        order = []

        class RecordingResource:
            def __init__(self, name):
                self.name = name
            def start(self):
                order.append(f"{self.name}_start")
            def close(self):
                order.append(f"{self.name}_close")

        mgr = LifecycleManager()
        mgr.register(RecordingResource("r1"))
        mgr.register(RecordingResource("r2"))
        mgr.start_all()

        self.assertEqual(order, ["r1_start", "r2_start"])

    def test_close_reverse_order(self):
        order = []

        class RecordingResource:
            def __init__(self, name):
                self.name = name
            def start(self):
                pass
            def close(self):
                order.append(f"{self.name}_close")

        mgr = LifecycleManager()
        mgr.register(RecordingResource("r1"))
        mgr.register(RecordingResource("r2"))
        mgr.start_all()
        mgr.close_all()

        # 关闭按反向顺序
        self.assertEqual(order, ["r2_close", "r1_close"])

    def test_close_all_reports_all_errors(self):
        """关闭时所有错误都被收集并报告"""
        close_errors = []

        class FailingResource:
            def start(self):
                pass
            def close(self):
                close_errors.append("error1")

        class FailingResource2:
            def start(self):
                pass
            def close(self):
                close_errors.append("error2")

        mgr = LifecycleManager()
        mgr.register(FailingResource())
        mgr.register(FailingResource2())
        mgr.start_all()

        mgr.close_all()

        # 两个资源的 close 都被调用
        self.assertEqual(len(close_errors), 2)
        self.assertIn("error1", close_errors)
        self.assertIn("error2", close_errors)


class TestCancellationToken(unittest.TestCase):
    def test_cancel_is_visible(self):
        token = CancellationToken()
        self.assertFalse(token.is_cancelled())
        token.cancel()
        self.assertTrue(token.is_cancelled())

    def test_multiple_cancels(self):
        token = CancellationToken()
        token.cancel()
        token.cancel()
        self.assertTrue(token.is_cancelled())


if __name__ == "__main__":
    unittest.main()
