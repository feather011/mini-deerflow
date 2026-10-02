"""集成执行运行时（17E+18 阶段）。

整合 Agent、Subagent、Middleware 到统一执行管线。
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any

from mini_deerflow.agent import Agent
from mini_deerflow.api.middleware.run_event import RunEventMiddleware
from mini_deerflow.api.services.event_broker import RunEventBroker
from mini_deerflow.api.services.run_stream import RunStreamManager
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
from mini_deerflow.mcp_config import MCPServerConfig
from mini_deerflow.mcp_manager import MCPClientManager
from mini_deerflow.model_config import ModelSettings
from mini_deerflow.model_factory import create_model
from mini_deerflow.models import FinalAnswer, Message, Model, ToolCall
from mini_deerflow.subagent_executor import SubagentBudget, SubagentManager
from mini_deerflow.subagent_tools import create_subagent_tools
from mini_deerflow.thread_run_coordinator import (
    ThreadRunConflictError,
    ThreadRunCoordinator,
)
from mini_deerflow.thread_store import ThreadStore
from mini_deerflow.tools import ToolRegistry


if TYPE_CHECKING:
    from mini_deerflow.api.services.agent_runs import AgentRunService


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
    # SSE Stream（19D）
    stream_max_workers: int = 8
    stream_queue_maxsize: int = 1024
    # Workspace File Service（19E）
    workspace_root: str | None = None  # None = 临时目录
    workspace_max_upload_bytes: int = 10 * 1024 * 1024
    workspace_max_list_entries: int = 1000
    # Model Provider（21A）
    # 默认 ModelSettings() 即 provider="fake"，因此既有测试与本地开发
    # 不读环境、不联网；CLI/FastAPI 会显式传入 load_model_settings()。
    model_settings: ModelSettings = field(default_factory=ModelSettings)
    # MCP 服务器（22）
    # 默认空列表 = **不启用 MCP**（不连任何子进程、不建事件循环桥）。
    # 由配置文件显式列出后才会启动，不存在"默认偷偷拉起外部进程"。
    mcp_servers: list[MCPServerConfig] = field(default_factory=list)


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
        extra_middlewares: list[Middleware] | None = None,
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
        if extra_middlewares:
            self._middlewares.extend(extra_middlewares)
        self._middlewares.append(LoggingMiddleware())

    def _next_run_id(self) -> str:
        with self._run_id_lock:
            self._run_id_counter += 1
            return f"run-{uuid.uuid4().hex[:8]}"

    def run(
        self,
        thread_id: str,
        user_input: str,
        run_id: str | None = None,
    ) -> tuple[str, list[dict]]:
        """执行 Agent，带 Middleware。

        Args:
            thread_id: 目标 Thread ID。
            user_input: 用户输入文本。
            run_id: 可选；调用方提供时原样使用（用于跨层 ID 贯通），
                缺省时由本方法自动生成（向后兼容 CLI / 旧调用方）。

        Returns:
            (final_answer, trace_events) 元组。
        """
        # 唯一 ID 生成点：未指定时内部生成；指定时绝不二次生成。
        effective_run_id = run_id if run_id else self._next_run_id()

        ctx = ExecutionContext(
            run_id=effective_run_id,
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
        store: ThreadStore,
        agent: Agent,
        integrated_agent: IntegratedAgent,
        manager: SubagentManager | None,
        config: RuntimeConfig,
        trace_sink: TraceSink | None,
        lifecycle: LifecycleManager,
        run_event_broker: RunEventBroker | None = None,
        run_stream_manager: RunStreamManager | None = None,
        run_event_middleware: RunEventMiddleware | None = None,
        stream_executor: ThreadPoolExecutor | None = None,
        agent_run_service: "AgentRunService | None" = None,
        file_service: "WorkspaceFileService | None" = None,
        mcp_manager: MCPClientManager | None = None,
    ) -> None:
        self.store = store
        self.agent = agent
        self.integrated_agent = integrated_agent
        self.manager = manager
        self.config = config
        self.trace_sink = trace_sink
        self.lifecycle = lifecycle
        self.thread_run_coordinator = ThreadRunCoordinator()
        # 19D SSE Stream
        self.run_event_broker = run_event_broker
        self.run_stream_manager = run_stream_manager
        self.run_event_middleware = run_event_middleware
        self.stream_executor = stream_executor
        self.agent_run_service = agent_run_service
        # 19E Workspace File Service
        self.file_service = file_service
        # 22 MCP 客户端管理器（未启用时为 None 或空配置的管理器）
        self.mcp_manager = mcp_manager

        # 注入 coordinator（构造期循环依赖，延后绑定）
        if self.agent_run_service is not None:
            self.agent_run_service._coordinator = self.thread_run_coordinator
        if self.run_stream_manager is not None:
            self.run_stream_manager._coordinator = self.thread_run_coordinator

    def shutdown(self) -> None:
        """关闭所有资源；幂等，重复调用安全。

        19D 关闭顺序：
            1. coordinator.shutdown() — 清占用表；
            2. run_stream_manager.shutdown() — executor 关闭 + broker.close()（投递哨兵）；
            3. manager.shutdown()；
            4. trace_sink.close()；
            5. lifecycle.close_all()。
            6. store.close()（23）— SQLite 连接收尾；内存实现无此方法则跳过。
        """
        if hasattr(self, "thread_run_coordinator"):
            self.thread_run_coordinator.shutdown()
        if hasattr(self, "run_stream_manager") and self.run_stream_manager is not None:
            self.run_stream_manager.shutdown()
        # 22：先关 MCP（它持有外部子进程与事件循环桥），再关本进程内的其他资源。
        # 顺序反了会让 MCP 的收尾协程没有可用的桥去执行。
        mcp = getattr(self, "mcp_manager", None)
        if mcp is not None:
            try:
                mcp.stop()
            except Exception:
                _LOGGER.warning("MCP 关闭失败", exc_info=True)
        if self.manager:
            self.manager.shutdown()
        if self.trace_sink:
            self.trace_sink.close()
        self.lifecycle.close_all()
        # 23：SQLite ThreadStore 必须显式关闭，否则容器停止时可能留下未 flush 的写入。
        # 用 getattr 而不是直接调用：测试会构造不含 store 的 RuntimeContext。
        close_store = getattr(getattr(self, "store", None), "close", None)
        if callable(close_store):
            try:
                close_store()
            except Exception:
                _LOGGER.warning("ThreadStore 关闭失败", exc_info=True)


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
    # 19D：额外注入 RunEventMiddleware（同一实例同时挂在 IntegratedAgent.pipeline，
    #     共享 thread-local 栈关联 AGENT_RUN 与 MODEL_CALL/TOOL_CALL）。
    run_event_broker = RunEventBroker(queue_maxsize=config.stream_queue_maxsize)
    run_event_middleware = RunEventMiddleware(run_event_broker)

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
    pipeline_middlewares.append(run_event_middleware)
    pipeline_middlewares.append(LoggingMiddleware())
    agent_pipeline = MiddlewarePipeline(pipeline_middlewares)

    # 22：MCP 工具在 Agent 构造前注册进 registry。
    # registry 就地变更、Agent 持有同一引用，因此先启动 MCP 再建 Agent 最直观。
    # mcp_servers 为空（默认）时 start() 直接返回 0，不会创建任何子进程或线程。
    mcp_manager = MCPClientManager(config.mcp_servers)
    if config.mcp_servers:
        # required 服务器失败会在这里 fail-closed 抛 MCPStartupError；
        # start() 内部已自行清理事件循环桥与子进程，不留半开状态。
        mcp_manager.start(registry)

    agent = Agent(
        # 显式传入的 model 优先（测试注入 FakeModel 等）；
        # 否则走统一 Model Factory，由 config.model_settings 决定 provider。
        model=model or create_model(config.model_settings),
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
        extra_middlewares=[run_event_middleware],
    )

    # 19D：Stream Manager + 有界 Executor
    # 延迟导入避免循环依赖（agent_runs → runtime.ThreadRunCoordinator）
    from mini_deerflow.api.services.agent_runs import AgentRunService

    stream_executor = ThreadPoolExecutor(
        max_workers=config.stream_max_workers,
        thread_name_prefix="run-stream",
    )
    # coordinator 由 RuntimeContext 构造；这里临时占位 None
    agent_run_service = AgentRunService(
        runner=integrated_agent.run,
        store=store,
        coordinator=None,  # type: ignore[arg-type]
    )
    run_stream_manager = RunStreamManager(
        broker=run_event_broker,
        agent_run_service=agent_run_service,
        coordinator=None,  # type: ignore[arg-type]
        executor=stream_executor,
    )

    # 19E：Workspace File Service
    from mini_deerflow.api.services.workspace_file import WorkspaceFileService
    from mini_deerflow.sandbox_path import WorkspacePathResolver

    if config.workspace_root:
        ws_root = Path(config.workspace_root)
        ws_root.mkdir(parents=True, exist_ok=True)
        is_temp = False
    else:
        ws_root = Path(tempfile.mkdtemp(prefix="mdf_ws_"))
        is_temp = True

    resolver = WorkspacePathResolver(ws_root)
    file_service = WorkspaceFileService(
        resolver=resolver,
        max_upload_bytes=config.workspace_max_upload_bytes,
        max_list_entries=config.workspace_max_list_entries,
    )
    lifecycle.register(_WorkspaceCleanup(ws_root, is_temp))

    return RuntimeContext(
        store=store,
        agent=agent,
        integrated_agent=integrated_agent,
        manager=manager,
        config=config,
        trace_sink=trace_sink,
        lifecycle=lifecycle,
        run_event_broker=run_event_broker,
        run_stream_manager=run_stream_manager,
        run_event_middleware=run_event_middleware,
        stream_executor=stream_executor,
        agent_run_service=agent_run_service,
        file_service=file_service,
        mcp_manager=mcp_manager,
    )


class _TraceSinkResource:
    """TraceSink 的生命周期资源封装。"""
    def __init__(self, sink: TraceSink) -> None:
        self._sink = sink

    def start(self) -> None:
        pass

    def close(self) -> None:
        self._sink.close()


class _WorkspaceCleanup:
    """临时 Workspace 目录的清理器。"""
    def __init__(self, root: Path, is_temp: bool) -> None:
        self._root = root
        self._is_temp = is_temp

    def start(self) -> None:
        pass

    def close(self) -> None:
        import shutil
        if self._is_temp and self._root.exists():
            try:
                shutil.rmtree(self._root)
            except OSError:
                pass
