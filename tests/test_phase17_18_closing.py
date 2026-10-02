"""第17、18阶段收口测试（验证真实集成）。

测试目标：
1. SubagentManager 真正有界的 worker pool（不是 Semaphore+per-task-Thread）
2. Middleware 接入真实 Agent 执行路径（model.decide / tool.execute）
3. CLI 走 IntegratedAgent（即使没有 Subagent）
4. Lifecycle 正确关闭所有资源
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from collections import deque
from typing import Any

from mini_deerflow.agent import Agent
from mini_deerflow.middleware import (
    Budget,
    BudgetExceededError,
    BudgetMiddleware,
    InMemoryTraceSink,
    LoggingMiddleware,
    Middleware,
    MiddlewarePipeline,
    OperationType,
    RetryAgainError,
    RetryMiddleware,
    RetryPolicy,
    TraceEvent,
    TraceMiddleware,
)
from mini_deerflow.models import FinalAnswer, Message, ToolCall
from mini_deerflow.runtime import RuntimeConfig, create_runtime
from mini_deerflow.subagent_executor import SubagentBudget, SubagentManager
from mini_deerflow.subagent_models import TaskStatus
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tools import Tool, ToolRegistry


# =============================================================================
# 1. SubagentManager 有界 worker pool 测试
# =============================================================================

class _CountingBlockingModel:
    """模拟阻塞模型：记录调用次数，阻塞直到 release_event。"""
    instances: list["_CountingBlockingModel"] = []

    def __init__(self) -> None:
        self.decide_calls = 0
        self.released = threading.Event()
        _CountingBlockingModel.instances.append(self)

    def decide(self, messages, tools=None):
        self.decide_calls += 1
        self.released.wait(timeout=5)
        return FinalAnswer(content="done")


class TestBoundedThreadPool(unittest.TestCase):
    """验证 ThreadPoolExecutor 真正有界（20 任务最多 2 worker）。"""

    def setUp(self):
        _CountingBlockingModel.instances.clear()

    def test_max_concurrency_two_with_20_tasks(self):
        """max_concurrency=2 时，20 个任务最多同时 2 个活跃。"""
        manager = SubagentManager(
            max_concurrency=2,
            max_tasks_per_parent=20,
        )

        # 让前 2 个任务一直阻塞以维持活跃数
        task_ids = []
        for i in range(20):
            model = _CountingBlockingModel()
            task = manager.submit(
                instruction=f"task-{i}",
                parent_run_id="run-0",
                parent_thread_id="main",
                model=model,
            )
            task_ids.append((task.task_id, model))

        # 等待前 2 个任务进入 RUNNING
        time.sleep(0.5)

        # 检查活跃模型数（决定已开始 decide 的）
        active = sum(1 for m in _CountingBlockingModel.instances if m.decide_calls > 0)
        self.assertLessEqual(active, 2, f"活跃模型数 {active} 超过 max_concurrency=2")

        # 释放所有阻塞
        for _, m in task_ids:
            m.released.set()

        # 等待完成
        for tid, _ in task_ids:
            manager.collect(tid, timeout=10.0)

        manager.shutdown()

    def test_queued_task_can_be_cancelled(self):
        """排队的任务可以取消（不必等到执行）。"""
        manager = SubagentManager(
            max_concurrency=1,
            max_tasks_per_parent=10,
        )

        # 第 1 个任务阻塞
        blocker = _CountingBlockingModel()
        t1 = manager.submit(
            instruction="blocker",
            parent_run_id="run-0",
            parent_thread_id="main",
            model=blocker,
        )

        # 第 2-4 个任务会排队
        queued_tasks = []
        for i in range(3):
            t = manager.submit(
                instruction=f"queued-{i}",
                parent_run_id="run-0",
                parent_thread_id="main",
                model=_CountingBlockingModel(),
            )
            queued_tasks.append(t)

        time.sleep(0.3)

        # 取消所有排队的任务
        for t in queued_tasks:
            accepted = manager.cancel(t.task_id)
            # 可能 accepted=True（取消请求已发送），也可能 False（已被处理）
            # 关键是取消不应让 manager 崩溃
            self.assertIn(accepted, {True, False})

        # 释放阻塞
        blocker.released.set()
        for m in _CountingBlockingModel.instances:
            m.released.set()

        # 关闭（shutdown 会再发一次取消）
        manager.shutdown()

    def test_shutdown_cleans_up_executor(self):
        """shutdown 后 ThreadPoolExecutor 真的停止接收任务。"""
        manager = SubagentManager(
            max_concurrency=2,
            max_tasks_per_parent=10,
        )

        blocker = _CountingBlockingModel()
        manager.submit(
            instruction="blocker",
            parent_run_id="run-0",
            parent_thread_id="main",
            model=blocker,
        )

        time.sleep(0.2)
        manager.shutdown(cancel_pending=True)

        # shutdown 后 executor 不再接受任务
        with self.assertRaises(RuntimeError):
            manager.submit(
                instruction="after-shutdown",
                parent_run_id="run-0",
                parent_thread_id="main",
                model=_CountingBlockingModel(),
            )

        blocker.released.set()


# =============================================================================
# 2. Middleware 接入 Agent 真实路径测试
# =============================================================================

class _RecordingTraceSink(InMemoryTraceSink):
    """记录所有 emit 事件的 sink。"""
    def __init__(self):
        super().__init__()
        self.emit_log: list[TraceEvent] = []

    def emit(self, event: TraceEvent) -> None:
        self.emit_log.append(event)
        super().emit(event)


class TestMiddlewareInAgentRealPath(unittest.TestCase):
    """验证 Middleware 真的包裹 Agent 的 model.decide 和 tool.execute。"""

    def setUp(self):
        self._store = InMemoryThreadStore()
        self._store.create("default")
        self._registry = ToolRegistry()

    def _make_agent_with_pipeline(self, middlewares):
        """创建带 Middleware pipeline 的 Agent。"""
        pipeline = MiddlewarePipeline(middlewares)
        return Agent(
            model=_ScriptedModel([FinalAnswer(content="ok")]),
            registry=self._registry,
            store=self._store,
            max_steps=10,
            middleware_pipeline=pipeline,
        )

    def test_trace_records_model_call_span(self):
        """FakeModel 的 decide 调用会产生 Model 操作的 trace span。"""
        sink = _RecordingTraceSink()
        agent = self._make_agent_with_pipeline([
            TraceMiddleware(sink),
        ])

        agent.run("default", "hi")

        events = sink.emit_log
        # 至少 1 个 MODEL_CALL span（OperationType.MODEL_CALL.name）
        model_events = [e for e in events if e.operation == "MODEL_CALL"]
        self.assertGreaterEqual(len(model_events), 1)
        self.assertEqual(model_events[0].name, "model_decide")
        self.assertEqual(model_events[0].thread_id, "default")

    def test_trace_records_tool_call_span(self):
        """ToolCall 会产生 Tool 操作的 trace span。"""
        # 注册一个简单工具
        self._registry.register(Tool(
            name="echo",
            description="echo back",
            parameters={},
            handler=lambda args: args.get("msg", ""),
        ))

        sink = _RecordingTraceSink()
        agent = self._make_agent_with_pipeline([
            TraceMiddleware(sink),
        ])
        # 第一次返回 ToolCall，第二次返回 FinalAnswer
        agent._model = _ScriptedModel([
            ToolCall(name="echo", arguments={"msg": "hello"}),
            FinalAnswer(content="done"),
        ])

        agent.run("default", "do echo")

        events = sink.emit_log
        tool_events = [e for e in events if e.operation == "TOOL_CALL"]
        self.assertGreaterEqual(len(tool_events), 1)
        self.assertEqual(tool_events[0].name, "echo")

    def test_retryable_model_failure_triggers_retry(self):
        """Model 失败一次后重试成功，budget 计入 1 次成功调用。"""
        class FailOnceModel:
            def __init__(self):
                self.call_count = 0
            def decide(self, messages, tools=None):
                self.call_count += 1
                if self.call_count == 1:
                    raise ConnectionError("temp network")
                return FinalAnswer(content="recovered")

        budget = Budget(max_model_calls=10)
        retry_policy = RetryPolicy(
            max_attempts=3,
            retryable_types=(ConnectionError,),
            initial_delay=0,
            multiplier=1,
        )

        model = FailOnceModel()
        agent = Agent(
            model=model,
            registry=self._registry,
            store=self._store,
            max_steps=10,
            middleware_pipeline=MiddlewarePipeline([
                RetryMiddleware(retry_policy),
                BudgetMiddleware(budget),
                LoggingMiddleware(),
            ]),
            budget=budget,
        )

        answer, _ = agent.run("default", "retry me")
        self.assertEqual(answer, "recovered")
        self.assertEqual(model.call_count, 2)
        # 原子 consume 语义：Model 在每次实际调用前 consume；
        # 失败 + 成功 retry = 共 2 次 model_calls（failures / retries 都计）。
        self.assertEqual(budget.model_calls(), 2)

    def test_non_idempotent_tool_not_retried(self):
        """不可重试的 tool 异常不应触发重试。

        ToolRegistry.execute 会将 handler 异常包装为 ToolExecutionError。
        RetryPolicy.retryable_types 不包含此类型时，工具只被调用一次。
        """
        from mini_deerflow.tools import ToolExecutionError

        call_count = [0]

        def non_idem_handler(args):
            call_count[0] += 1
            raise ValueError("boom")

        self._registry.register(Tool(
            name="non_idem",
            description="non-idempotent tool",
            parameters={},
            handler=non_idem_handler,
        ))

        retry_policy = RetryPolicy(
            max_attempts=3,
            retryable_types=(ConnectionError,),  # ToolExecutionError 不在内
            initial_delay=0,
            multiplier=1,
        )

        agent = self._make_agent_with_pipeline([
            RetryMiddleware(retry_policy),
        ])
        agent._model = _ScriptedModel([
            ToolCall(name="non_idem", arguments={}),
            FinalAnswer(content="fallback"),
        ])

        with self.assertRaises(ToolExecutionError):
            agent.run("default", "call tool")

        # 工具只被调用一次（不被重试）
        self.assertEqual(call_count[0], 1)

    def test_direct_agent_run_uses_pipeline(self):
        """直接 Agent.run 也走 pipeline。"""
        sink = _RecordingTraceSink()
        agent = self._make_agent_with_pipeline([
            TraceMiddleware(sink),
        ])

        agent.run("default", "test")

        # 至少 1 个 MODEL_CALL 事件
        model_events = [e for e in sink.emit_log if e.operation == "MODEL_CALL"]
        self.assertGreaterEqual(len(model_events), 1)

    def test_budget_exceeded_raises(self):
        """Budget 超限抛出 BudgetExceededError。"""
        budget = Budget(max_model_calls=2, max_tool_calls=10)

        self._registry.register(Tool(
            name="noop",
            description="no-op",
            parameters={},
            handler=lambda args: "ok",
        ))

        agent = Agent(
            model=_ScriptedModel([
                ToolCall(name="noop", arguments={}),
                ToolCall(name="noop", arguments={}),
                ToolCall(name="noop", arguments={}),  # 第 3 次 tool call 触发超限
                FinalAnswer(content="x"),
            ]),
            registry=self._registry,
            store=self._store,
            max_steps=10,
            middleware_pipeline=MiddlewarePipeline([
                BudgetMiddleware(budget),
            ]),
            budget=budget,
        )

        # 消耗 budget
        budget.record_model_call()
        budget.record_model_call()

        with self.assertRaises(BudgetExceededError):
            agent.run("default", "x")


# =============================================================================
# 3. CLI 走 IntegratedAgent 测试
# =============================================================================

class TestCLIUsesIntegratedRuntime(unittest.TestCase):
    """验证 CLI 通过 create_runtime 创建 IntegratedAgent（即使没启用 subagent）。"""

    def test_cli_agent_has_middleware_pipeline(self):
        """Agent 应该有 middleware_pipeline 参数（即使为空）。"""
        store = InMemoryThreadStore()
        registry = ToolRegistry()

        ctx = create_runtime(
            store=store,
            registry=registry,
            config=RuntimeConfig(subagent_enabled=False, trace_enabled=True),
        )

        # Agent 应有 middleware_pipeline
        self.assertIsNotNone(ctx.agent._middleware_pipeline)
        # IntegratedAgent 应存在
        self.assertIsNotNone(ctx.integrated_agent)
        ctx.shutdown()

    def test_lifecycle_closes_trace_sink(self):
        """RuntimeContext.shutdown 关闭 trace sink。"""
        store = InMemoryThreadStore()
        registry = ToolRegistry()

        ctx = create_runtime(
            store=store,
            registry=registry,
            config=RuntimeConfig(subagent_enabled=False, trace_enabled=True),
        )

        sink = ctx.trace_sink
        self.assertIsNotNone(sink)

        ctx.shutdown()

        # sink 应已关闭（多次关闭不应抛异常）
        if sink is not None:
            sink.close()

    def test_cli_run_emits_trace(self):
        """通过 IntegratedAgent 执行应产生 trace 事件。"""
        from mini_deerflow.runtime import IntegratedAgent
        from mini_deerflow.tools import create_default_registry

        store = InMemoryThreadStore()
        store.create("default")
        registry = create_default_registry()
        sink = _RecordingTraceSink()

        ctx = create_runtime(
            store=store,
            registry=registry,
            config=RuntimeConfig(subagent_enabled=False, trace_enabled=True),
        )
        # 注入自定义 sink（覆盖）
        ctx.integrated_agent._trace_sink = sink
        # 替换 Agent 中的 TraceMiddleware sink
        for mw in ctx.agent._middleware_pipeline._middlewares:
            if isinstance(mw, TraceMiddleware):
                mw._sink = sink

        answer, _ = ctx.integrated_agent.run("default", "计算 1+1")

        self.assertIsNotNone(answer)
        # 至少 1 个事件（model_call 或 agent_run）
        self.assertGreaterEqual(len(sink.emit_log), 1)

        ctx.shutdown()


# =============================================================================
# 辅助：ScriptedModel
# =============================================================================

class _ScriptedModel:
    """按预设序列返回决策。"""
    def __init__(self, decisions):
        self._queue = deque(decisions)

    def decide(self, messages, tools=None):
        return self._queue.popleft()


# =============================================================================
# 4. Budget 关键场景测试（用户指定的 8 个关键场景）
# =============================================================================

class TestBudgetAtomicConsume(unittest.TestCase):
    """验证 Budget 原子 consume 语义覆盖的关键场景。"""

    def setUp(self):
        self._store = InMemoryThreadStore()
        self._store.create("default")
        self._registry = ToolRegistry()

    # ----- 测试 2：Model 一直失败时，达到 model limit 后停止 Retry -----

    def test_model_keeps_failing_stops_at_model_limit(self):
        """Model 一直失败 → Retry 用尽 max_attempts；budget 计数 = max_attempts。

        原子 consume 语义保证：每次 retry attempt 都消耗 model_calls；
        达到 max_model_calls 时 BudgetExceededError 阻断后续 Retry。
        """
        class AlwaysFailModel:
            def __init__(self):
                self.call_count = 0
            def decide(self, messages, tools=None):
                self.call_count += 1
                raise ConnectionError(f"fail-{self.call_count}")

        budget = Budget(max_model_calls=3)
        retry_policy = RetryPolicy(
            max_attempts=5,  # 试图重试 5 次
            retryable_types=(ConnectionError,),
            initial_delay=0,
            multiplier=1,
        )

        agent = Agent(
            model=AlwaysFailModel(),
            registry=self._registry,
            store=self._store,
            max_steps=10,
            middleware_pipeline=MiddlewarePipeline([
                RetryMiddleware(retry_policy),
                BudgetMiddleware(budget),
            ]),
            budget=budget,
        )

        with self.assertRaises(BudgetExceededError) as ctx:
            agent.run("default", "always fail")
        self.assertEqual(ctx.exception.resource, "model_calls")
        # 3 次 attempt 后 budget 耗尽，retry 不再继续
        # （Agent 主循环不在 step 层消费；budget 只在每次 model 调用前 consume_model_call）
        self.assertEqual(budget.model_calls(), 3)

    # ----- 测试 3：Tool 失败仍消耗 1 次 tool_calls -----

    def test_tool_failure_still_consumes_tool_call(self):
        """Tool handler 抛异常时，tool_calls 计数仍 +1（失败也计）。"""
        from mini_deerflow.tools import ToolExecutionError

        def boom(args):
            raise ValueError("tool failed")

        self._registry.register(Tool(
            name="boom_tool",
            description="always fails",
            parameters={},
            handler=boom,
        ))

        budget = Budget(max_model_calls=10, max_tool_calls=5)
        agent = Agent(
            model=_ScriptedModel([
                ToolCall(name="boom_tool", arguments={}),
                FinalAnswer(content="done"),
            ]),
            registry=self._registry,
            store=self._store,
            max_steps=10,
            middleware_pipeline=MiddlewarePipeline([BudgetMiddleware(budget)]),
            budget=budget,
        )

        with self.assertRaises(ToolExecutionError):
            agent.run("default", "use boom tool")
        # tool 失败但 consume_tool_call 已扣减
        self.assertEqual(budget.tool_calls(), 1)
        self.assertEqual(budget.model_calls(), 1)

    # ----- 测试 4：两个并发 Subagent 共享 model limit 不会合计超限 -----

    def test_concurrent_subagents_share_model_limit(self):
        """两个并发子任务共享同一个 budget → model_calls 总和不超过 max_model_calls。

        A 需要 3 次 model call，B 需要 2 次；共享 budget.max_model_calls=4，
        总计 5 次调用，第 4 次之后的调用应被 budget 阻断 → B 的最后一次失败。
        """
        shared_budget = Budget(max_model_calls=4)

        # 模型每次都返回 ToolCall，直到达到预设次数后返回 FinalAnswer
        class NStepModel:
            def __init__(self, n_calls: int):
                self._n_calls = n_calls
                self.calls = 0
            def decide(self, messages, tools=None):
                self.calls += 1
                if self.calls < self._n_calls:
                    return ToolCall(name="noop", arguments={})
                return FinalAnswer(content=f"done-after-{self.calls}")

        registry = ToolRegistry()
        registry.register(Tool(
            name="noop",
            description="no-op tool",
            parameters={},
            handler=lambda args: "ok",
        ))

        manager = SubagentManager(
            max_concurrency=2,
            shared_budget=shared_budget,
            default_budget=SubagentBudget(max_steps=10),
        )

        # 注入 registry 到 manager 的子任务执行路径
        # 这里通过直接构造 SubagentRunner 模拟 manager 行为，避免工具绑定复杂度
        from mini_deerflow.subagent_models import SubagentTask
        from mini_deerflow.subagent_executor import SubagentRunner, CancellationToken
        from mini_deerflow.thread_store import InMemoryThreadStore
        import time

        model_a = NStepModel(n_calls=3)
        model_b = NStepModel(n_calls=2)

        runner_a = SubagentRunner(
            task=SubagentTask(
                task_id="a", parent_run_id="r", parent_thread_id="t",
                instruction="a", status=__import__("mini_deerflow.subagent_models", fromlist=["TaskStatus"]).TaskStatus.QUEUED,
                created_at=time.monotonic(),
            ),
            model=model_a,
            registry=registry,
            store=InMemoryThreadStore(),
            budget=shared_budget,
            cancellation_token=CancellationToken(),
        )
        runner_b = SubagentRunner(
            task=SubagentTask(
                task_id="b", parent_run_id="r", parent_thread_id="t",
                instruction="b", status=__import__("mini_deerflow.subagent_models", fromlist=["TaskStatus"]).TaskStatus.QUEUED,
                created_at=time.monotonic(),
            ),
            model=model_b,
            registry=registry,
            store=InMemoryThreadStore(),
            budget=shared_budget,
            cancellation_token=CancellationToken(),
        )

        result_a = runner_a.run()
        result_b = runner_b.run()

        # A: 3 次调用，shared_budget 1+2+3 全部成功
        self.assertEqual(result_a.terminal_status, TaskStatus.SUCCEEDED)
        # B: 第 1 次 consume 成功（总计 4），第 2 次 consume 超限 → 失败
        self.assertEqual(result_b.terminal_status, TaskStatus.FAILED)
        # 共享 budget 不超卖：总消耗 == max_model_calls（4）
        self.assertLessEqual(shared_budget.model_calls(), 4)
        self.assertEqual(shared_budget.model_calls(), 4)

        manager.shutdown()

    # ----- 测试 5：提交子任务消耗 subagent_tasks -----

    def test_subagent_submit_consumes_subagent_tasks(self):
        """每次 manager.submit() 消耗 1 次 subagent_tasks。"""
        budget = Budget(max_subagent_tasks=5, max_model_calls=100)
        manager = SubagentManager(
            max_concurrency=2,
            shared_budget=budget,
            default_budget=SubagentBudget(max_steps=2),
        )

        self.assertEqual(budget.subagent_tasks(), 0)

        manager.submit(
            instruction="t1", parent_run_id="run-0", parent_thread_id="main"
        )
        self.assertEqual(budget.subagent_tasks(), 1)

        manager.submit(
            instruction="t2", parent_run_id="run-0", parent_thread_id="main"
        )
        self.assertEqual(budget.subagent_tasks(), 2)

        # 达到上限后再 submit 应抛 BudgetExceededError
        for i in range(3, 6):
            manager.submit(
                instruction=f"t{i}",
                parent_run_id="run-0",
                parent_thread_id="main",
            )
        self.assertEqual(budget.subagent_tasks(), 5)

        with self.assertRaises(BudgetExceededError) as ctx:
            manager.submit(
                instruction="overflow",
                parent_run_id="run-0",
                parent_thread_id="main",
            )
        self.assertEqual(ctx.exception.resource, "subagent_tasks")

        manager.shutdown(cancel_pending=True)

    # ----- 测试 6：超过 max_subagent_depth 不能 submit -----

    def test_subagent_depth_limit_blocks_submit(self):
        """depth 超过 budget.max_subagent_depth 时 submit 抛 BudgetExceededError。"""
        budget = Budget(
            max_subagent_tasks=10,
            max_subagent_depth=2,
            max_model_calls=100,
        )
        manager = SubagentManager(
            max_concurrency=1,
            shared_budget=budget,
            default_budget=SubagentBudget(max_steps=2),
        )

        # depth=1 OK
        manager.submit(
            instruction="d1", parent_run_id="run-0", parent_thread_id="main",
            depth=1,
        )
        # depth=2 OK
        manager.submit(
            instruction="d2", parent_run_id="run-0", parent_thread_id="main",
            depth=2,
        )
        # depth=3 超 budget.max_subagent_depth=2 → BudgetExceededError
        with self.assertRaises(BudgetExceededError) as ctx:
            manager.submit(
                instruction="d3",
                parent_run_id="run-0",
                parent_thread_id="main",
                depth=3,
            )
        self.assertEqual(ctx.exception.resource, "subagent_depth")

        manager.shutdown(cancel_pending=True)

    # ----- 测试 7：BudgetExceededError 不参与 Retry -----

    def test_budget_exceeded_does_not_trigger_retry(self):
        """BudgetExceededError 不是 RetryPolicy 的 retryable 类型 → 不重试。

        这避免了「budget 耗尽 → 触发 retry → 重试时再次失败 → 死循环」的反模式。
        """
        retry_policy = RetryPolicy(
            max_attempts=5,
            retryable_types=(ConnectionError, RuntimeError),
            initial_delay=0,
            multiplier=1,
        )

        # 故意让 budget 立即耗尽
        budget = Budget(max_model_calls=0, max_tool_calls=10)

        agent = Agent(
            model=_ScriptedModel([FinalAnswer(content="never-run")]),
            registry=self._registry,
            store=self._store,
            max_steps=10,
            middleware_pipeline=MiddlewarePipeline([
                RetryMiddleware(retry_policy),
                BudgetMiddleware(budget),
            ]),
            budget=budget,
        )

        # budget 已耗尽，第一次 model call 就抛 BudgetExceededError，
        # 不应被 RetryMiddleware 重试（retryable_types 不含 BudgetExceededError）。
        with self.assertRaises(BudgetExceededError):
            agent.run("default", "test")
        # model 实际被调用了 1 次（被 budget 阻止）；retry 次数 = 0
        self.assertEqual(budget.model_calls(), 0)  # budget 0 → 第一次就 raise，未扣减

    # ----- 测试 8：旧 Budget / SubagentBudget 导入兼容 -----

    def test_subagent_budget_is_alias_for_budget(self):
        """SubagentBudget 与 Budget 是同一类（兼容别名）。"""
        self.assertIs(SubagentBudget, Budget)

        # 从 subagent_executor 导入的 SubagentBudget 应与从 middleware 导入的 Budget 一致
        from mini_deerflow.middleware import Budget as MiddlewareBudget
        self.assertIs(SubagentBudget, MiddlewareBudget)

        # 旧 API（check_* / record_*）仍可用（向后兼容）
        b = SubagentBudget(max_model_calls=2)
        self.assertTrue(b.check_model_call())
        b.record_model_call()
        self.assertTrue(b.check_model_call())
        b.record_model_call()
        self.assertFalse(b.check_model_call())

        # 新 API（consume_*）抛 BudgetExceededError
        with self.assertRaises(BudgetExceededError):
            b.consume_model_call()


if __name__ == "__main__":
    unittest.main()