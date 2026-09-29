"""集成执行运行时（17E+18 阶段）。

整合 Agent、Subagent、Middleware 到统一执行管线。
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from mini_deerflow.agent import Agent
from mini_deerflow.middleware import (
    Budget,
    BudgetMiddleware,
    CancellationToken,
    CompositeTraceSink,
    ExecutionContext,
    InMemoryTraceSink,
    JSONLTraceSink,
    LifecycleManager,
    LoggingMiddleware,
    Middleware,
    MiddlewarePipeline,
    OperationType,
    RetryMiddleware,
    RetryPolicy,
    TraceEvent,
    TraceMiddleware,
    TraceSink,
)
from mini_deerflow.models import FakeModel, FinalAnswer, Message, Model, ToolCall
from mini_deerflow.subagent_executor import SubagentBudget, SubagentManager
from mini_deerflow.subagent_tools import create_subagent_tools
from mini_deerflow.thread_store import ThreadStore
from mini_deerflow.tools import ToolRegistry


_LOGGER = logging.getLogger(__name__)


# =============================================================================
# 执行运行时配置
# =============================================================================

@dataclass
class RuntimeConfig:
    """运行时配置。"""
    # Subagent
    subagent_enabled: bool = False
    subagent_max_concurrency: int = 2
    subagent_max_depth: int = 3
    subagent_max_tasks_per_parent: int = 10
    subagent_default_timeout: float = 300.0
    # Middleware
    trace_enabled: bool = False
    trace_path: str | None = None  # JSONL 文件路径
    # Retry
    retry_enabled: bool = False
    retry_max_attempts: int = 1


# =============================================================================
# Middleware 适配层
# =============================================================================

class AgentMiddleware(Middleware[str]):
    """Agent 执行中间件适配。"""
    def __init__(self, sink: TraceSink | None = None) -> None:
        self._sink = sink
        self._ctx: ExecutionContext | None = None

    def before(self, ctx: ExecutionContext) -> None:
        self._ctx = ctx

    def success(self, ctx: ExecutionContext, result: str) -> str:
        return result

    def error(self, ctx: ExecutionContext, exc: Exception) -> Exception:
        return exc

    def finally_(self, ctx: ExecutionContext) -> None:
        pass


# =============================================================================
# Middleware 执行管线封装
# =============================================================================

class PipelineExecutor:
    """封装 MiddlewarePipeline 的执行器。"""
    def __init__(
        self,
        middlewares: list[Middleware],
        operation: OperationType,
        operation_name: str,
        run_id: str,
        thread_id: str,
        task_id: str | None = None,
        parent_span_id: str | None = None,
    ) -> None:
        self._middlewares = middlewares
        self._operation = operation
        self._operation_name = operation_name
        self._run_id = run_id
        self._thread_id = thread_id
        self._task_id = task_id
        self._parent_span_id = parent_span_id

    def run(self, action: callable) -> Any:
        """通过 MiddlewarePipeline 执行操作。"""
        ctx = ExecutionContext(
            run_id=self._run_id,
            thread_id=self._thread_id,
            task_id=self._task_id,
            operation=self._operation,
            operation_name=self._operation_name,
            parent_span_id=self._parent_span_id,
        )
        pipeline = MiddlewarePipeline(self._middlewares)
        return pipeline.execute(ctx, action)


# =============================================================================
# 集成 Agent
# =============================================================================

class IntegratedAgent:
    """集成 Middleware 的 Agent wrapper。"""
    def __init__(
        self,
        agent: Agent,
        config: RuntimeConfig,
        trace_sink: TraceSink | None = None,
    ) -> None:
        self._agent = agent
        self._config = config
        self._trace_sink = trace_sink
        self._run_id_counter = 0
        self._run_id_lock = Lock()
        self._middlewares: list[Middleware] = []

        # 构建中间件列表
        if trace_sink:
            self._middlewares.append(TraceMiddleware(trace_sink))
        self._middlewares.append(LoggingMiddleware())

    def _next_run_id(self) -> str:
        with self._run_id_lock:
            self._run_id_counter += 1
            return f"run-{uuid.uuid4().hex[:8]}"

    def run(self, thread_id: str, user_input: str) -> tuple[str, list[dict]]:
        """执行 Agent，带 Middleware。"""
        run_id = self._next_run_id()

        ctx = ExecutionContext(
            run_id=run_id,
            thread_id=thread_id,
            operation=OperationType.AGENT_RUN,
            operation_name="agent_run",
        )

        pipeline = MiddlewarePipeline(self._middlewares)

        def action() -> tuple[str, list[dict]]:
            return self._agent.run(thread_id, user_input)

        return pipeline.execute(ctx, action)


# =============================================================================
# 执行上下文 Provider
# =============================================================================

class RuntimeContext:
    """运行时上下文。持有所有运行时组件。"""
    def __init__(
        self,
        agent: Agent,
        integrated_agent: IntegratedAgent,
        manager: SubagentManager | None,
        config: RuntimeConfig,
        trace_sink: TraceSink | None,
        lifecycle: LifecycleManager,
    ) -> None:
        self.agent = agent
        self.integrated_agent = integrated_agent
        self.manager = manager
        self.config = config
        self.trace_sink = trace_sink
        self.lifecycle = lifecycle

    def shutdown(self) -> None:
        """关闭所有资源。"""
        if self.manager:
            self.manager.shutdown()
        if self.trace_sink:
            self.trace_sink.close()
        self.lifecycle.close_all()


def create_runtime(
    store: ThreadStore,
    registry: ToolRegistry,
    config: RuntimeConfig,
    run_id: str = "main",
    model: Model | None = None,
) -> RuntimeContext:
    """创建集成运行时。"""
    lifecycle = LifecycleManager()

    # Trace Sink
    trace_sink: TraceSink | None = None
    if config.trace_enabled:
        if config.trace_path:
            trace_sink = CompositeTraceSink([
                InMemoryTraceSink(),
                JSONLTraceSink(config.trace_path),
            ])
        else:
            trace_sink = InMemoryTraceSink()
        lifecycle.register(_TraceSinkResource(trace_sink))

    # 父子共享 Budget（同时挂到 Agent 与 SubagentManager 上）。
    # Agent 的 Model/Tool 消耗 + Subagent.submit 消耗 + 子任务的 Model/Tool 消耗
    # 都走同一个 Budget 实例的原子 consume 接口。
    budget = Budget(
        max_steps=10,
        max_model_calls=50,
        max_tool_calls=100,
        max_subagent_tasks=20,
        max_subagent_depth=config.subagent_max_depth,
    )

    # SubagentManager
    manager: SubagentManager | None = None
    if config.subagent_enabled:
        manager = SubagentManager(
            max_concurrency=config.subagent_max_concurrency,
            max_depth=config.subagent_max_depth,
            max_tasks_per_parent=config.subagent_max_tasks_per_parent,
            default_timeout=config.subagent_default_timeout,
            shared_budget=budget,  # 父子共享同一 Budget
        )
        # 注册子任务工具
        tools = create_subagent_tools(
            manager=manager,
            parent_run_id=run_id,
            parent_thread_id="default",
        )
        registry.register_many(tools)

    # Agent（带 Middleware pipeline：Logging + Trace + Retry + Budget）
    pipeline_middlewares: list = []
    pipeline_middlewares.append(BudgetMiddleware(budget))
    if trace_sink:
        pipeline_middlewares.append(TraceMiddleware(trace_sink))
    if config.retry_enabled:
        pipeline_middlewares.append(RetryMiddleware(
            RetryPolicy(
                max_attempts=config.retry_max_attempts,
                retryable_types=(RuntimeError, ConnectionError),
            )
        ))
    pipeline_middlewares.append(LoggingMiddleware())
    agent_pipeline = MiddlewarePipeline(pipeline_middlewares)

    agent = Agent(
        model=model or FakeModel(),
        registry=registry,
        store=store,
        max_steps=10,
        middleware_pipeline=agent_pipeline,
        budget=budget,
    )

    integrated_agent = IntegratedAgent(
        agent=agent,
        config=config,
        trace_sink=trace_sink,
    )

    return RuntimeContext(
        agent=agent,
        integrated_agent=integrated_agent,
        manager=manager,
        config=config,
        trace_sink=trace_sink,
        lifecycle=lifecycle,
    )


class _TraceSinkResource:
    """TraceSink 的生命周期资源封装。"""
    def __init__(self, sink: TraceSink) -> None:
        self._sink = sink

    def start(self) -> None:
        pass

    def close(self) -> None:
        self._sink.close()
