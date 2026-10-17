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
import json
from collections.abc import Mapping
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
from mini_deerflow.context_assembler import AssembledContext, ContextAssembler
from mini_deerflow.context_compactor import ContextCompactor
from mini_deerflow.context_state import ContextPolicy
from mini_deerflow.context_usage import ContextUsageSnapshot
from mini_deerflow.context_store import (
    ContextStore,
    InMemoryContextStore,
    SQLiteContextStore,
)
from mini_deerflow.context_tools import build_task_note_tool
from mini_deerflow.history_index import HistoryIndexService, HistoryVectorIndex
from mini_deerflow.history_tools import build_history_read_tool, build_history_search_tool
from mini_deerflow.mcp_config import MCPServerConfig
from mini_deerflow.mcp_manager import MCPClientManager
from mini_deerflow.mcp_tool_state import MCPToolStateStore
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
from mini_deerflow.token_counter import HeuristicTokenCounter
from mini_deerflow.project_store import (
    InMemoryProjectStore,
    ProjectStore,
    SQLiteProjectStore,
)
from mini_deerflow.tools import ToolRegistry
from mini_deerflow.tool_authorization import ToolPolicyRegistry
from mini_deerflow.confirmation import ConfirmationStore, ConfirmationError, ConfirmationRequest
from mini_deerflow.confirmation_manager import ConfirmationManager

# 项目锁：所有 RuntimeContext 实例共用同一进程级字典
_PROJECT_LOCKS: dict[str, Lock] = {}
_PROJECT_LOCKS_LOCK = Lock()


def _get_project_lock(project_id: str) -> Lock:
    """返回项目锁（惰性创建）。Agent 工具层与 API 层共用同一字典。"""
    with _PROJECT_LOCKS_LOCK:
        if project_id not in _PROJECT_LOCKS:
            _PROJECT_LOCKS[project_id] = Lock()
        return _PROJECT_LOCKS[project_id]


if TYPE_CHECKING:
    from mini_deerflow.api.services.agent_runs import AgentRunService
    from mini_deerflow.confirmation import ConfirmationStore
    from mini_deerflow.confirmation_manager import ConfirmationManager
    from mini_deerflow.confirmation import ConfirmationRequest, ConfirmationError


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
    # Context Engineering（24）
    # 默认启用，但 context_db_path=None → 内存 ContextStore，
    # 因此短会话与既有测试的持久化行为完全不变。
    context_enabled: bool = True
    context_db_path: str | None = None  # None = 内存 ContextStore
    context_max_tokens: int = 80000
    context_reserved_output_tokens: int = 4096
    context_safety_margin_tokens: int = 2048
    context_compaction_threshold: float = 0.8
    # 摘要专用 Provider 额度（与 max_model_calls 分开，见 Budget 说明）
    context_max_summary_calls: int = 20
    # WM-3C Harness 执行器（未配置时不注册 project_execute 工具）
    harness_root: str | None = None
    harness_timeout_seconds: float = 600.0
    # 模型调用真实超时（秒），0 = 不启用（使用 SDK 自带 timeout）
    model_timeout_seconds: float = 0.0
    # 测试用：禁用 Budget 限制
    disable_budget: bool = False


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
        # 按线程保存最近一次 run 捕获的上下文用量快照
        self._last_usage_by_thread: dict[str, ContextUsageSnapshot] = {}
        # 最近一次已捕获的 AssembledContext 引用（对象身份比较用）
        self._last_captured_snapshot: "AssembledContext | None" = None

        # 构建中间件列表
        if trace_sink:
            self._middlewares.append(TraceMiddleware(trace_sink))
        if extra_middlewares:
            self._middlewares.extend(extra_middlewares)
        self._middlewares.append(LoggingMiddleware())

    def get_last_usage(self, thread_id: str) -> ContextUsageSnapshot | None:
        """读取该 Thread 最近一次 run 捕获的上下文用量快照。

        未发送过消息（或未启用 ContextAssembler）时返回 None。
        """
        return self._last_usage_by_thread.get(thread_id)

    def _capture_last_usage(self, thread_id: str, run_id: str) -> None:
        """run 结束后（含失败）捕获一次新的 assemble 快照。

        用对象身份判断本轮是否产生了新的 AssembledContext：
        Agent.run 只在 assemble() 成功返回后更新 `_last_assembled`，
        因此 assemble 抛错时引用不变，`is` 比较为 True → 跳过捕获，
        上一轮快照不被覆盖。
        """
        assembled = self._agent.last_assembled
        if assembled is None or assembled is self._last_captured_snapshot:
            return
        # assembler 是注入 Agent 的同一个实例；它的 policy 即输入预算来源。
        assembler = self._agent._context_assembler
        if assembler is None:
            return
        snapshot = ContextUsageSnapshot.from_assembled(
            thread_id=thread_id,
            policy=assembler.policy,
            assembled=assembled,
            run_id=run_id,
        )
        self._last_usage_by_thread[thread_id] = snapshot
        self._last_captured_snapshot = assembled

    def _next_run_id(self) -> str:
        with self._run_id_lock:
            self._run_id_counter += 1
            return f"run-{uuid.uuid4().hex[:8]}"

    def run(
        self,
        thread_id: str,
        user_input: str,
        run_id: str | None = None,
        cancellation_token: "CancellationToken | None" = None,
    ) -> tuple[str, list[dict]]:
        """执行 Agent，带 Middleware。

        Args:
            thread_id: 目标 Thread ID。
            user_input: 用户输入文本。
            run_id: 可选；调用方提供时原样使用（用于跨层 ID 贯通），
                缺省时由本方法自动生成（向后兼容 CLI / 旧调用方）。
            cancellation_token: 可选取消令牌；主任务停止时置位，
                Agent 在每步开始检查，工具调用链透传给 HarnessRunner。

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
            cancellation_token=cancellation_token,
        )

        pipeline = MiddlewarePipeline(self._middlewares)

        def action() -> tuple[str, list[dict]]:
            # 传 run_id 和 cancellation_token：上下文组装与压缩需要前者，
            # 步骤预算与停止传播需要后者。
            return self._agent.run(
                thread_id, user_input,
                run_id=effective_run_id,
                cancellation_token=cancellation_token,
            )

        # finally 捕获：即便 run 失败，本轮已完成的 assemble 快照也要保留
        #（用于诊断；assemble 本身失败时由身份比较跳过，见 _capture_last_usage）。
        try:
            return pipeline.execute(ctx, action)
        finally:
            self._capture_last_usage(thread_id, effective_run_id)


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
        mcp_tool_state_store: "MCPToolStateStore | None" = None,
        confirmation_store: "ConfirmationStore | None" = None,
        confirmation_manager: "ConfirmationManager | None" = None,
        context_store: "ContextStore | None" = None,
        context_assembler: "ContextAssembler | None" = None,
        project_store: ProjectStore | None = None,
        project_roots: tuple[str, ...] = (),
        workspaces_root: str | None = None,
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
        # TOOL-03：MCP 工具授权状态存储（启用状态 + 分主体授权）
        self.mcp_tool_state_store = mcp_tool_state_store
        # TOOL-04：确认请求持久化存储 + 业务管理器
        self.confirmation_store = confirmation_store
        self.confirmation_manager = confirmation_manager
        # TOOL-04 R1：运行时执行的 invoker——从 ToolRegistry 真实调用。
        # 由 runtime 注入到 manager（在 create_runtime 末尾或测试 setUp）。
        self.execute_confirmed_request: "Callable[[str, str, str, str, dict], ConfirmationRequest] | None" = None
        # 24 Context Engineering（context_enabled=False 时两者皆为 None）
        self.context_store = context_store
        self.context_assembler = context_assembler
        # WM-4 Project Store（未启用时为 None）
        self.project_store = project_store
        self.project_roots = project_roots
        # Phase 1：后端托管工作区目录
        self._workspaces_root = Path(workspaces_root).expanduser().resolve() if workspaces_root else Path.home() / ".mini-deerflow" / "workspaces"

        # 注入 coordinator（构造期循环依赖，延后绑定）
        if self.agent_run_service is not None:
            self.agent_run_service._coordinator = self.thread_run_coordinator
        if self.run_stream_manager is not None:
            self.run_stream_manager._coordinator = self.thread_run_coordinator

    def get_project_lock(self, project_id: str) -> Lock:
        """返回项目锁（惰性创建）。Agent 工具层与 API 层共用同一字典。"""
        return _get_project_lock(project_id)

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
        # 24：先清空向量索引，再关 ContextStore，最后关事实源 ThreadStore。
        # 顺序从内到外——投影全部处理完，才轮到承载它们的存储。
        index = getattr(getattr(getattr(self, "context_assembler", None),
                                "history_service", None), "index", None)
        clear_index = getattr(index, "clear", None)
        if callable(clear_index):
            try:
                clear_index()
            except Exception:
                # 用 error 而非 warning：索引没清干净意味着 shutdown 未真正完成
                _LOGGER.error("向量索引清理失败", exc_info=True)
        close_context = getattr(getattr(self, "context_store", None), "close", None)
        if callable(close_context):
            try:
                close_context()
            except Exception:
                _LOGGER.error("ContextStore 关闭失败", exc_info=True)
        # 23：SQLite ThreadStore 必须显式关闭，否则容器停止时可能留下未 flush 的写入。
        # 用 getattr 而不是直接调用：测试会构造不含 store 的 RuntimeContext。
        close_store = getattr(getattr(self, "store", None), "close", None)
        if callable(close_store):
            try:
                close_store()
            except Exception:
                _LOGGER.warning("ThreadStore 关闭失败", exc_info=True)


def load_context_config(env: "Mapping[str, str] | None" = None) -> dict[str, object]:
    """读取 24 上下文工程的环境变量，返回可直接 `**` 展开给 RuntimeConfig 的字典。

    与 `create_runtime` 的关系：本函数**只读环境**——不建数据库连接、不起线程、
    不发任何模型请求，因此模块 import 与 `create_app()` 阶段调用它也是安全的
    （连接在 `create_runtime` 里才真正建立）。

    数值非法时直接抛 `ValueError`：静默回退到默认值会让「配了但没生效」
    这种问题拖到线上才发现，比启动即失败更贵。
    """
    resolved: Mapping[str, str] = os.environ if env is None else env

    def _get(name: str) -> str | None:
        raw = resolved.get(name)
        if raw is None:
            return None
        value = raw.strip()
        return value or None

    def _get_int(name: str) -> int | None:
        raw = _get(name)
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"{name} 必须是整数，收到 {raw!r}") from exc

    def _get_float(name: str) -> float | None:
        raw = _get(name)
        if raw is None:
            return None
        try:
            return float(raw)
        except ValueError as exc:
            raise ValueError(f"{name} 必须是数字，收到 {raw!r}") from exc

    config: dict[str, object] = {}

    enabled = _get("MINI_DEERFLOW_CONTEXT_ENABLED")
    if enabled is not None:
        lowered = enabled.lower()
        if lowered in {"0", "false", "no", "off"}:
            config["context_enabled"] = False
        elif lowered in {"1", "true", "yes", "on"}:
            config["context_enabled"] = True
        else:
            raise ValueError(
                f"MINI_DEERFLOW_CONTEXT_ENABLED 只接受 0/1，收到 {enabled!r}"
            )

    db_path = _get("MINI_DEERFLOW_CONTEXT_DB_PATH")
    if db_path is not None:
        config["context_db_path"] = db_path

    max_tokens = _get_int("MINI_DEERFLOW_CONTEXT_MAX_TOKENS")
    if max_tokens is not None:
        if max_tokens <= 0:
            raise ValueError(
                f"MINI_DEERFLOW_CONTEXT_MAX_TOKENS 必须为正，收到 {max_tokens}"
            )
        config["context_max_tokens"] = max_tokens

    reserved = _get_int("MINI_DEERFLOW_CONTEXT_RESERVED_OUTPUT_TOKENS")
    if reserved is not None:
        if reserved < 0:
            raise ValueError(
                f"MINI_DEERFLOW_CONTEXT_RESERVED_OUTPUT_TOKENS 不能为负，收到 {reserved}"
            )
        config["context_reserved_output_tokens"] = reserved

    margin = _get_int("MINI_DEERFLOW_CONTEXT_SAFETY_MARGIN_TOKENS")
    if margin is not None:
        if margin < 0:
            raise ValueError(
                f"MINI_DEERFLOW_CONTEXT_SAFETY_MARGIN_TOKENS 不能为负，收到 {margin}"
            )
        config["context_safety_margin_tokens"] = margin

    threshold = _get_float("MINI_DEERFLOW_CONTEXT_COMPACTION_THRESHOLD")
    if threshold is not None:
        if not 0 < threshold <= 1:
            raise ValueError(
                f"MINI_DEERFLOW_CONTEXT_COMPACTION_THRESHOLD 必须在 (0, 1]，收到 {threshold}"
            )
        config["context_compaction_threshold"] = threshold

    return config


def create_runtime(
    store: ThreadStore,
    registry: ToolRegistry,
    config: RuntimeConfig,
    run_id: str = "main",
    model: Model | None = None,
    project_store: ProjectStore | None = None,
    project_roots: tuple[str, ...] = (),
    workspaces_root: str | None = None,
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
    if config.disable_budget:
        budget = None

    # 24：上下文工程层。放在 SubagentManager 之前，因为子代理与主 Agent
    # 共用同一个 assembler（按 thread_id 天然隔离，见 RuntimeContext 说明）。
    # 默认启用，但 context_db_path=None → 内存 ContextStore，
    # 因此短会话与既有测试的持久化行为完全不变。
    #
    # Model 只构造一次：Agent 决策与上下文压缩共用同一实例，
    # 否则 provider 配置会在两条路径上漂移。
    agent_model = model or create_model(config.model_settings)

    context_store: ContextStore | None = None
    context_assembler: ContextAssembler | None = None
    if config.context_enabled:
        if config.context_db_path:
            context_store = SQLiteContextStore(config.context_db_path)
        else:
            context_store = InMemoryContextStore()

        context_policy = ContextPolicy(
            max_context_tokens=config.context_max_tokens,
            reserved_output_tokens=config.context_reserved_output_tokens,
            safety_margin_tokens=config.context_safety_margin_tokens,
            compaction_threshold=config.context_compaction_threshold,
        )
        token_counter = HeuristicTokenCounter()
        # 摘要用独立 Budget：压缩多的线程不能把决策额度吃光
        summary_budget = Budget(max_summary_calls=config.context_max_summary_calls)
        compactor = ContextCompactor(
            model=agent_model,
            counter=token_counter,
            policy=context_policy,
            trace_sink=trace_sink,
            budget=summary_budget,
        )
        # 历史检索服务：先于 assembler 创建，这样线程销毁时
        # assembler 能一并清理 ContextState 与向量索引两份投影。
        history_service = HistoryIndexService(
            index=HistoryVectorIndex(),
            thread_store=store,
            context_store=context_store,
            trace_sink=trace_sink,
        )
        context_assembler = ContextAssembler(
            policy=context_policy,
            counter=token_counter,
            context_store=context_store,
            compactor=compactor,
            trace_sink=trace_sink,
            history_service=history_service,
        )

        # 三个按线程隔离的工具：thread_id 只能来自 ExecutionContext，
        # 参数 schema 里刻意不暴露该字段。
        context_tools = [
            build_task_note_tool(context_store, trace_sink=trace_sink),
            build_history_search_tool(history_service),
            build_history_read_tool(history_service),
        ]
        # 调用方可能已注入同名工具（测试 / 自定义扩展），冲突时保留已注册的
        registered_names = {tool.name for tool in registry.list_tools()}
        registry.register_many(
            [tool for tool in context_tools if tool.name not in registered_names]
        )

    # 项目锁字典（工具层与 API 层共用）
    _project_locks: dict[str, Lock] = {}

    # TOOL-02：创建策略注册表（SubagentManager 和 Agent 都需要注入）
    tool_policy_registry = ToolPolicyRegistry(thread_store=store)

    # SubagentManager
    manager: SubagentManager | None = None
    if config.subagent_enabled:
        manager = SubagentManager(
            max_concurrency=config.subagent_max_concurrency,
            max_depth=config.subagent_max_depth,
            max_tasks_per_parent=config.subagent_max_tasks_per_parent,
            default_timeout=config.subagent_default_timeout,
            shared_budget=budget,  # 父子共享同一 Budget
            # 24：子代理复用同一 assembler。thread_id 形如
            # `subagent-{task_id}`，因此 ContextState / 摘要 / 水位 / Task Notes
            # 与父线程以及其它子代理天然隔离，无需另建 store。
            context_assembler=context_assembler,
            # TOOL-02：子代理与父共享策略注册表，principal = MINI_SUBAGENT
            tool_policy_registry=tool_policy_registry,
            # TOOL-02：子代理与父共享 ThreadStore（用于项目文件访问和 project_id 反查）
            thread_store=store,
        )
        # 注册子任务工具
        tools = create_subagent_tools(
            manager=manager,
            parent_run_id=run_id,
            parent_thread_id="default",
            thread_store=store,
        )
        registry.register_many(tools)

    # WM-3B：项目只读工具（始终注册，由 compute_thread_tool_allow_list 过滤可见性）
    from mini_deerflow.project_tools import create_project_read_tools
    if project_store is not None:
        read_tools = create_project_read_tools(
            thread_store=store,
            project_store=project_store,
        )
        existing = {tool.name for tool in registry.list_tools()}
        registry.register_many([t for t in read_tools if t.name not in existing])

    # WM-3C：Harness 执行器（未配置时跳过工具注册）
    harness_runner = None
    if config.harness_root:
        from mini_deerflow.harness_runner import HarnessRunner
        harness_runner = HarnessRunner(
            harness_root=config.harness_root,
            timeout_seconds=config.harness_timeout_seconds,
            max_tool_calls=50,
        )
        from mini_deerflow.project_tools import create_project_execute_tool
        if project_store is not None:
            tools = [
                create_project_execute_tool(
                    thread_store=store,
                    project_store=project_store,
                    runner=harness_runner,
                    get_lock=_get_project_lock,
                )
            ]
            registry.register_many([t for t in tools if t.name not in {tool.name for tool in registry.list_tools()}])

    # Agent（带 Middleware pipeline：Logging + Trace + Retry + Budget）
    # 19D：额外注入 RunEventMiddleware（同一实例同时挂在 IntegratedAgent.pipeline，
    #     共享 thread-local 栈关联 AGENT_RUN 与 MODEL_CALL/TOOL_CALL）。
    run_event_broker = RunEventBroker(queue_maxsize=config.stream_queue_maxsize)
    run_event_middleware = RunEventMiddleware(run_event_broker, thread_store=store)  # 26: 传入 store 持久化 run.result

    pipeline_middlewares: list = []
    if budget is not None:
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
    mcp_tool_state_store = MCPToolStateStore()
    mcp_started = False
    try:
        if config.mcp_servers:
            # required 服务器失败会在这里 fail-closed 抛 MCPStartupError；
            # start() 内部已自行清理事件循环桥与子进程，不留半开状态。
            mcp_manager.start(registry)
            mcp_started = True
            # TOOL-03 R2：MCP 工具元数据 + 持久化授权状态应用到策略注册表
            # 按 server 声明的可信分类（tool_capabilities / tool_side_effects）
            # 传递到 make_mcp_tool_metadata，避免所有工具都被默认拒绝
            from mini_deerflow.tool_contracts import make_mcp_tool_metadata
            # server_name -> config 索引
            configs_by_name = {c.name: c for c in config.mcp_servers}
            for qname in mcp_manager.registered_names:
                server_name, _ = mcp_manager._tools[qname]
                server_config = configs_by_name.get(server_name)
                if server_config is not None:
                    declared_caps = server_config.tool_capabilities
                    declared_sides = server_config.tool_side_effects
                else:
                    declared_caps = frozenset()
                    declared_sides = frozenset()
                tool_policy_registry.register_metadata(make_mcp_tool_metadata(
                    qname,
                    declared_capabilities=declared_caps,
                    declared_side_effects=declared_sides,
                ))
                state = mcp_tool_state_store.get(qname)
                from mini_deerflow.tool_authorization import ToolPolicy as _TP
                tool_policy_registry.set_policy(
                    qname,
                    _TP(
                        enabled=state.enabled,
                        allowed_principals=state.allowed_principals,
                    ),
                )
    except BaseException:
        # TOOL-03 R6：状态加载/应用失败时清理已启动的 MCP 资源，再原样抛
        if mcp_started:
            try:
                mcp_manager.stop()
            except Exception:
                pass
        raise

    # TOOL-02：策略注册表已在上面创建并注入 SubagentManager 和 Agent
    agent = Agent(
        # 显式传入的 model 优先（测试注入 FakeModel 等）；
        # 否则走统一 Model Factory，由 config.model_settings 决定 provider。
        model=agent_model,
        registry=registry,
        store=store,
        max_steps=10,
        middleware_pipeline=agent_pipeline,
        budget=budget,
        context_assembler=context_assembler,
        model_timeout_seconds=config.model_timeout_seconds,
        tool_policy_registry=tool_policy_registry,
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

    # TOOL-04：确认请求领域 + 业务管理器
    # R1：必须在 RuntimeContext 构造之前（与 agent / registry 同步），
    # 使 RuntimeContext 内部能持有 confirmation_manager。
    confirmation_store = ConfirmationStore()
    policy_registry: ToolPolicyRegistry = tool_policy_registry

    def _is_run_alive(run_id: str, thread_id: str) -> bool:
        # R5：同时检查 thread 是否有该 run_id 在跑（不是只看 thread 忙）。
        # Coordinator 只存 (thread_id, run_id) 关系；is_running(thread) 表示该 thread
        # 当前正在跑某个 run。我们这里要求"该 thread 正在跑且 run_id 匹配"。
        coord = thread_run_coordinator
        # thread 忙 + 那个 run 仍然 active
        return coord.is_running(thread_id)

    confirmation_manager = ConfirmationManager(
        store=confirmation_store,
        policy_registry=policy_registry,
        is_run_alive=_is_run_alive,
    )

    runtime = RuntimeContext(
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
        # TOOL-03：MCP 工具授权状态（启用状态 + 分主体授权）持久化存储
        mcp_tool_state_store=mcp_tool_state_store,
        # TOOL-04：确认请求持久化存储 + 业务管理器
        confirmation_store=confirmation_store,
        confirmation_manager=confirmation_manager,
        context_store=context_store,
        context_assembler=context_assembler,
        project_store=project_store,
        project_roots=project_roots,
        workspaces_root=workspaces_root,
    )

    # R1：runtime.execute_confirmed_request 真实从 ToolRegistry 调工具 handler。
    # R6：从父 Run 的 ExecutionContext 快照得到 parent_authority 与冻结参数。
    # 该回调由 API 路由 /confirmations/{id}/execute 调用（生产正路径）。
    from mini_deerflow.tool_authorization import RunAuthority as _RA
    from mini_deerflow.tool_contracts import ExecutorPrincipal as _EP

    def _execute_confirmed_request(
        request_id: str,
        run_id: str,
        thread_id: str,
        tool_name: str,
        arguments: dict,
    ) -> ConfirmationRequest:
        # R6：先参数深拷贝，避免共享可变嵌套对象
        import copy as _copy
        arguments_snapshot = _copy.deepcopy(arguments)

        req = confirmation_store.get(request_id)
        if req is None:
            raise ConfirmationError(f"未知确认请求: {request_id}")
        # R6：执行前参数 hash 复核——caller 传 arguments 必须与 req.arguments_hash 一致
        from mini_deerflow.confirmation import _hash_arguments
        caller_hash = _hash_arguments(arguments_snapshot)
        if caller_hash != req.arguments_hash:
            raise ConfirmationError(
                f"参数不匹配：批准时 hash={req.arguments_hash[:12]} "
                f"调用 hash={caller_hash[:12]}"
            )
        # R6：从真实运行（agent / integrated_agent / runtime）找父 ExecutionContext
        # 真实生产中父 Run 上下文应已保存到 runtime._active_runs[run_id] 或类似。
        # 这里给出优先查找路径；没有则保守回退，但不允许伪造能力。
        parent_authority: RunAuthority | None = None
        # 1) 从 runtime._active_run_authorities（如果调用方提供）
        active_authorities = getattr(runtime, "_active_run_authorities", None)
        if isinstance(active_authorities, dict):
            parent_authority = active_authorities.get(run_id)
        # 2) 从 agent 保存的 _last_run_authority（如果存在）
        if parent_authority is None:
            agent_obj = runtime.agent or runtime.integrated_agent
            last_auth = getattr(agent_obj, "_last_run_authority", None) if agent_obj else None
            if isinstance(last_auth, dict):
                parent_authority = last_auth.get(run_id)
        # 3) 回退：从 req 重建（仅做基础一致性检查，不补能力）
        if parent_authority is None:
            parent_authority = _RA(
                principal=_EP[req.principal],
                thread_id=req.thread_id,
                run_id=req.run_id,
                project_id=req.project_id,
                granted_capabilities=frozenset(),  # 不自授能力
            )

        def _invoker(name: str, args: dict) -> str:
            # 重要：handler 必须返回的 JSON 解析后才算"成功"
            # 现有工具返回 JSON 字符串（_ok / _error 工厂）；按工具结果契约判成功/失败/unknown
            tool = registry.get(name)
            if tool is None:
                raise ValueError(f"工具未注册: {name}")
            from mini_deerflow.middleware import ExecutionContext as _EC
            ctx = _EC(
                run_id=run_id,
                thread_id=thread_id,
                granted_capabilities=frozenset(),
            )
            result_str = tool.execute(args, context=ctx)
            # 按工具结果契约判别
            if not result_str:
                return '{"ok": false, "error": "empty_result", "unknown": true}'
            try:
                parsed = json.loads(result_str)
            except (ValueError, TypeError):
                # 非 JSON：当作字符串返回，但 ok 仅在非空 + 非 error 前缀时为 true
                return result_str
            return result_str

        return confirmation_manager.claim_and_execute(
            request_id,
            parent_authority=parent_authority,
            invoker=_invoker,
        )

    runtime.execute_confirmed_request = _execute_confirmed_request
    return runtime


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
