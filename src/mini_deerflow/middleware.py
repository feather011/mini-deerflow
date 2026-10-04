"""中间件系统（18A 阶段）。

提供统一的执行管线：Agent run、Model call、Tool call、Subagent task。
"""

from __future__ import annotations

import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Generic, TypeVar
from threading import Lock

# =============================================================================
# ExecutionContext
# =============================================================================

class OperationType(Enum):
    """操作类型枚举。"""
    AGENT_RUN = auto()
    MODEL_CALL = auto()
    TOOL_CALL = auto()
    SUBAGENT_TASK = auto()
    # 24：上下文压缩走专用枚举，使它在 Trace 里能与普通决策调用区分
    SUMMARIZATION = auto()


@dataclass
class ExecutionContext:
    """执行上下文：贯穿整个请求的生命周期。"""
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    thread_id: str = ""
    task_id: str | None = None  # Subagent task_id
    depth: int = 0
    operation: OperationType | None = None
    operation_name: str = ""
    attempt: int = 1
    started_at: float = field(default_factory=time.monotonic)
    cancellation_token: "CancellationToken | None" = None
    budget: "Budget | None" = None
    parent_span_id: str | None = None
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_cancelled(self) -> bool:
        return self.cancellation_token.is_cancelled() if self.cancellation_token else False


# =============================================================================
# CancellationToken
# =============================================================================

class CancellationToken:
    """协作取消令牌。"""
    __slots__ = ("_cancelled", "_lock")

    def __init__(self) -> None:
        self._cancelled = False
        self._lock = Lock()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True

    def is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled


# =============================================================================
# Middleware Protocol
# =============================================================================

T = TypeVar("T")

class Middleware(ABC, Generic[T]):
    """中间件基类。"""

    @abstractmethod
    def before(self, ctx: ExecutionContext) -> None:
        """before 钩子：执行前调用。"""

    @abstractmethod
    def success(self, ctx: ExecutionContext, result: T) -> T:
        """success 钩子：成功后调用，返回原始或修改后的结果。"""

    @abstractmethod
    def error(self, ctx: ExecutionContext, exc: Exception) -> Exception:
        """error 钩子：失败时调用，返回原始或包装后的异常。"""

    @abstractmethod
    def finally_(self, ctx: ExecutionContext) -> None:
        """finally 钩子：无论成功失败都会调用。"""


# =============================================================================
# MiddlewarePipeline
# =============================================================================

class MiddlewarePipeline(Generic[T]):
    """中间件管线：before 正序，success/error/finally 倒序。"""

    def __init__(self, middlewares: list[Middleware[T]]) -> None:
        self._middlewares = list(middlewares)

    def execute(
        self,
        ctx: ExecutionContext,
        action: Callable[[], T],
    ) -> T:
        """执行管线。"""
        results: list[Any] = []  # 存储每个中间件的 finally 是否已执行

        # before 正序
        for m in self._middlewares:
            m.before(ctx)
            results.append(False)

        try:
            result = action()
            # success 倒序
            for m in reversed(self._middlewares):
                result = m.success(ctx, result)
            return result
        except Exception as e:
            # error 倒序
            for m in reversed(self._middlewares):
                e = m.error(ctx, e)
            raise e
        finally:
            # finally 倒序，确保恰好执行一次
            for m in reversed(self._middlewares):
                m.finally_(ctx)

    def execute_with_retry(
        self,
        ctx: ExecutionContext,
        action: Callable[[], T],
        max_loops: int = 1000,
    ) -> T:
        """执行管线，并自动重试 RetryAgainError。

        RetryMiddleware.error 抛出 RetryAgainError 时，重新执行 action()。
        上限 max_loops 防止无限循环。
        """
        attempts = 0
        while True:
            try:
                return self.execute(ctx, action)
            except RetryAgainError:
                attempts += 1
                if attempts > max_loops:
                    raise RuntimeError(
                        f"execute_with_retry 重试次数超过上限 {max_loops}"
                    )
                continue


# =============================================================================
# 日志中间件（18B）
# =============================================================================

_LOGGER = logging.getLogger("mini_deerflow.middleware")


def _mask_sensitive(value: str) -> str:
    """脱敏敏感字段。"""
    SENSITIVE_KEYS = frozenset({
        "token", "api_key", "apikey", "password", "secret",
        "authorization", "auth", "credential",
    })
    words = value.split()
    masked = []
    for word in words:
        lower = word.lower()
        # 检查是否包含敏感关键词
        if any(k in lower for k in SENSITIVE_KEYS):
            masked.append("[REDACTED]")
        elif len(word) > 8 and word[0].isupper() and any(c.islower() for c in word[1:]):
            # 可能是 API key 格式
            masked.append(word[:4] + "****")
        else:
            masked.append(word)
    return " ".join(masked)


class LoggingMiddleware(Middleware[T]):
    """结构化日志中间件。"""

    def __init__(self, min_level: int = logging.INFO) -> None:
        self._min_level = min_level

    def before(self, ctx: ExecutionContext) -> None:
        _LOGGER.log(
            self._min_level,
            "operation_start",
            extra={
                "run_id": ctx.run_id,
                "thread_id": ctx.thread_id,
                "task_id": ctx.task_id,
                "depth": ctx.depth,
                "operation": ctx.operation.name if ctx.operation else None,
                "operation_name": ctx.operation_name,
                "attempt": ctx.attempt,
            },
        )

    def success(self, ctx: ExecutionContext, result: T) -> T:
        duration = time.monotonic() - ctx.started_at
        _LOGGER.log(
            self._min_level,
            "operation_success",
            extra={
                "run_id": ctx.run_id,
                "thread_id": ctx.thread_id,
                "task_id": ctx.task_id,
                "depth": ctx.depth,
                "operation": ctx.operation.name if ctx.operation else None,
                "operation_name": ctx.operation_name,
                "attempt": ctx.attempt,
                "duration_seconds": round(duration, 3),
                "status": "success",
            },
        )
        return result

    def error(self, ctx: ExecutionContext, exc: Exception) -> Exception:
        duration = time.monotonic() - ctx.started_at
        _LOGGER.log(
            logging.ERROR,
            "operation_error",
            extra={
                "run_id": ctx.run_id,
                "thread_id": ctx.thread_id,
                "task_id": ctx.task_id,
                "depth": ctx.depth,
                "operation": ctx.operation.name if ctx.operation else None,
                "operation_name": ctx.operation_name,
                "attempt": ctx.attempt,
                "duration_seconds": round(duration, 3),
                "status": "error",
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:200],
            },
        )
        return exc

    def finally_(self, ctx: ExecutionContext) -> None:
        """finally 不记录日志（已在 success/error 中处理）。"""


# =============================================================================
# 重试中间件（18C）
# =============================================================================

@dataclass
class RetryPolicy:
    """重试策略。"""
    max_attempts: int = 1
    initial_delay: float = 0.5
    multiplier: float = 2.0
    max_delay: float = 30.0
    # 可注入的 sleeper（用于测试）
    _sleeper: Callable[[float], None] = field(
        default_factory=lambda s=None, time=__import__("time"): time.sleep(s) if s else None,
        repr=False,
    )
    # 哪些异常类型可以重试
    retryable_types: tuple[type[Exception], ...] = ()


class RetryMiddleware(Middleware[T]):
    """带退避的重试中间件。"""

    def __init__(
        self,
        policy: RetryPolicy,
        min_level: int = logging.WARNING,
    ) -> None:
        self._policy = policy
        self._min_level = min_level
        self._current_attempt = 0

    def before(self, ctx: ExecutionContext) -> None:
        self._current_attempt = 0

    def success(self, ctx: ExecutionContext, result: T) -> T:
        if self._current_attempt > 1:
            _LOGGER.log(
                self._min_level,
                "retry_succeeded",
                extra={
                    "run_id": ctx.run_id,
                    "task_id": ctx.task_id,
                    "attempt": self._current_attempt,
                },
            )
        return result

    def error(self, ctx: ExecutionContext, exc: Exception) -> Exception:
        should_retry = (
            self._current_attempt < self._policy.max_attempts
            and isinstance(exc, self._policy.retryable_types)
        )
        if not should_retry:
            return exc

        self._current_attempt += 1
        delay = min(
            self._policy.initial_delay * (self._policy.multiplier ** (self._current_attempt - 1)),
            self._policy.max_delay,
        )

        _LOGGER.log(
            self._min_level,
            "retry_scheduled",
            extra={
                "run_id": ctx.run_id,
                "task_id": ctx.task_id,
                "attempt": self._current_attempt,
                "delay_seconds": round(delay, 3),
                "error_type": type(exc).__name__,
            },
        )

        # 调用注入的 sleeper
        if self._policy._sleeper:
            self._policy._sleeper(delay)

        # 返回 RetrySucceeded 异常表示需要重试
        return RetryAgainError(exc, self._current_attempt)

    def finally_(self, ctx: ExecutionContext) -> None:
        pass


class RetryAgainError(Exception):
    """信号异常：表示需要重试。"""
    def __init__(self, original: Exception, attempt: int) -> None:
        self.original = original
        self.attempt = attempt
        super().__init__(f"retry attempt {attempt}: {original}")


def execute_with_retry(
    ctx: ExecutionContext,
    policy: RetryPolicy,
    action: Callable[[], T],
) -> T:
    """执行带重试的操作。"""
    attempts = 0
    delay = 0.0

    while True:
        attempts += 1
        try:
            return action()
        except RetryAgainError as e:
            if attempts >= policy.max_attempts:
                return action()  # 最后一次尝试
            delay = min(
                policy.initial_delay * (policy.multiplier ** (attempts - 1)),
                policy.max_delay,
            )
            if policy._sleeper:
                policy._sleeper(delay)
        except Exception:
            if attempts >= policy.max_attempts:
                raise
            delay = min(
                policy.initial_delay * (policy.multiplier ** (attempts - 1)),
                policy.max_delay,
            )
            if policy._sleeper:
                policy._sleeper(delay)


# =============================================================================
# 统一限额（18D）
# =============================================================================

@dataclass
class Budget:
    """执行预算。线程安全。

    唯一正式的 Budget 实现。提供原子 consume_* 语义：
    consume_step / consume_model_call / consume_tool_call / consume_subagent_task /
    consume_summary_call。每个 consume 在同一把锁内完成
    「检查额度 + 扣减 + 超限抛错」三步，并发调用不会超卖。
    失败/异常也要计数（在 action 真正发起前 consume）。

    `summary_calls` 是 24 新增的**独立**限额：上下文压缩的摘要调用不与普通
    Agent 决策调用共享 `model_calls`，否则一次长会话压缩会把决策额度吃光，
    表现为「莫名其妙就到 max_model_calls 了」。总 Provider 消耗用
    `total_provider_calls()` 观测。

    SubagentBudget 是本类的兼容别名（同一定义，两套名字可互换）。
    """
    max_steps: int | None = None
    max_model_calls: int | None = None
    max_tool_calls: int | None = None
    max_subagent_tasks: int | None = None
    max_subagent_depth: int | None = None
    max_wall_time_seconds: float | None = None
    # 24：摘要专用额度（None = 不限，与其它 max_* 语义一致）
    max_summary_calls: int | None = None
    _steps: int = field(default=0, init=False)
    _model_calls: int = field(default=0, init=False)
    _tool_calls: int = field(default=0, init=False)
    _subagent_tasks: int = field(default=0, init=False)
    _summary_calls: int = field(default=0, init=False)
    _max_depth_seen: int = field(default=0, init=False)
    _lock: Lock = field(default_factory=Lock, repr=False)

    # ------------------------------------------------------------------
    # 原子 consume 语义
    # ------------------------------------------------------------------

    def consume_step(self) -> None:
        """原子地：检查 step 额度 → 扣减 → 超限抛 BudgetExceededError。"""
        with self._lock:
            if self.max_steps is not None and self._steps >= self.max_steps:
                raise BudgetExceededError("steps", self.max_steps)
            self._steps += 1

    def consume_model_call(self) -> None:
        """原子地：检查 model_calls 额度 → 扣减 → 超限抛错。

        Model 和 Tool 必须在真正发起调用前 consume；失败 / 异常 / Retry 每次 attempt 都要计数。
        """
        with self._lock:
            if self.max_model_calls is not None and self._model_calls >= self.max_model_calls:
                raise BudgetExceededError("model_calls", self.max_model_calls)
            self._model_calls += 1

    def consume_tool_call(self) -> None:
        """原子地：检查 tool_calls 额度 → 扣减 → 超限抛错。"""
        with self._lock:
            if self.max_tool_calls is not None and self._tool_calls >= self.max_tool_calls:
                raise BudgetExceededError("tool_calls", self.max_tool_calls)
            self._tool_calls += 1

    def consume_subagent_task(self, depth: int) -> None:
        """原子地：检查子任务额度 → 扣减 → 检查深度 → 超限抛错。

        depth 从 1 开始。depth=0 表示父任务自身。
        """
        with self._lock:
            if self.max_subagent_tasks is not None and self._subagent_tasks >= self.max_subagent_tasks:
                raise BudgetExceededError("subagent_tasks", self.max_subagent_tasks)
            if self.max_subagent_depth is not None and depth > self.max_subagent_depth:
                raise BudgetExceededError("subagent_depth", self.max_subagent_depth)
            self._subagent_tasks += 1
            if depth > self._max_depth_seen:
                self._max_depth_seen = depth

    def check_depth(self, depth: int) -> bool:
        """仅检查深度，不扣减。"""
        if self.max_subagent_depth is None:
            return True
        with self._lock:
            return depth <= self.max_subagent_depth

    def consume_summary_call(self) -> None:
        """原子地：检查 summary_calls 额度 → 扣减 → 超限抛错。

        在摘要请求真正发起前调用；格式失败后的修复重试同样计数
        （重试也是真实 Provider 调用）。
        """
        with self._lock:
            if (
                self.max_summary_calls is not None
                and self._summary_calls >= self.max_summary_calls
            ):
                raise BudgetExceededError("summary_calls", self.max_summary_calls)
            self._summary_calls += 1

    # ------------------------------------------------------------------
    # 读访问器
    # ------------------------------------------------------------------

    def steps(self) -> int:
        with self._lock:
            return self._steps

    def model_calls(self) -> int:
        with self._lock:
            return self._model_calls

    def tool_calls(self) -> int:
        with self._lock:
            return self._tool_calls

    def subagent_tasks(self) -> int:
        with self._lock:
            return self._subagent_tasks

    def summary_calls(self) -> int:
        """已消耗的摘要调用次数。"""
        with self._lock:
            return self._summary_calls

    def total_provider_calls(self) -> int:
        """总 Provider 调用量 = 决策调用 + 摘要调用。

        两类限额分开，但对外的用量指标必须合起来看——否则压缩多的线程
        会在账面上「看起来很省」。
        """
        with self._lock:
            return self._model_calls + self._summary_calls

    def max_depth_seen(self) -> int:
        with self._lock:
            return self._max_depth_seen

    def check_wall_time(self, started_at: float) -> bool:
        if self.max_wall_time_seconds is None:
            return True
        return time.monotonic() - started_at < self.max_wall_time_seconds

    # ------------------------------------------------------------------
    # 旧 API（check_* / record_*），保留以兼容现有测试与外部代码。
    # 推荐新代码使用 consume_*。
    # ------------------------------------------------------------------

    def check_step(self) -> bool:
        if self.max_steps is None:
            return True
        with self._lock:
            return self._steps < self.max_steps

    def check_model_call(self) -> bool:
        if self.max_model_calls is None:
            return True
        with self._lock:
            return self._model_calls < self.max_model_calls

    def check_tool_call(self) -> bool:
        if self.max_tool_calls is None:
            return True
        with self._lock:
            return self._tool_calls < self.max_tool_calls

    def check_subagent_task(self) -> bool:
        if self.max_subagent_tasks is None:
            return True
        with self._lock:
            return self._subagent_tasks < self.max_subagent_tasks

    def check_summary_call(self) -> bool:
        """旧 API 风格的摘要额度检查（不扣减）。"""
        if self.max_summary_calls is None:
            return True
        with self._lock:
            return self._summary_calls < self.max_summary_calls

    def record_step(self) -> None:
        with self._lock:
            self._steps += 1

    def record_model_call(self) -> None:
        with self._lock:
            self._model_calls += 1

    def record_tool_call(self) -> None:
        with self._lock:
            self._tool_calls += 1

    def record_subagent_task(self) -> None:
        with self._lock:
            self._subagent_tasks += 1

    def record_summary_call(self) -> None:
        """旧 API 风格的摘要额度记录（不检查，直接累加）。"""
        with self._lock:
            self._summary_calls += 1


class BudgetExceededError(Exception):
    """预算耗尽。"""
    def __init__(self, resource: str, limit: int | None) -> None:
        self.resource = resource
        self.limit = limit
        super().__init__(
            f"预算耗尽：{resource}（上限: {limit}）" if limit else f"预算耗尽：{resource}"
        )


class BudgetMiddleware(Middleware[T]):
    """预算检查中间件。"""

    def __init__(self, budget: Budget) -> None:
        self._budget = budget

    def before(self, ctx: ExecutionContext) -> None:
        ctx.budget = self._budget

    def success(self, ctx: ExecutionContext, result: T) -> T:
        return result

    def error(self, ctx: ExecutionContext, exc: Exception) -> Exception:
        return exc

    def finally_(self, ctx: ExecutionContext) -> None:
        pass


# =============================================================================
# Trace 系统（18E）
# =============================================================================

class TraceEvent:
    """Trace 事件。"""
    def __init__(
        self,
        event_id: str,
        parent_id: str | None,
        run_id: str,
        thread_id: str,
        task_id: str | None,
        operation: str,
        name: str,
        start: float,
        end: float | None = None,
        status: str = "running",
        error_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.event_id = event_id
        self.parent_id = parent_id
        self.run_id = run_id
        self.thread_id = thread_id
        self.task_id = task_id
        self.operation = operation
        self.name = name
        self.start = start
        self.end = end
        self.duration = end - start if end else None
        self.status = status
        self.error_type = error_type
        self.metadata = metadata or {}

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "event_id": self.event_id,
            "parent_id": self.parent_id,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "operation": self.operation,
            "name": self.name,
            "start": self.start,
            "status": self.status,
        }
        if self.task_id:
            result["task_id"] = self.task_id
        if self.end:
            result["end"] = self.end
            result["duration"] = self.duration
        if self.error_type:
            result["error_type"] = self.error_type
        if self.metadata:
            result["metadata"] = self.metadata
        return result


class TraceSink(ABC):
    """Trace 接收器基类。"""
    @abstractmethod
    def emit(self, event: TraceEvent) -> None:
        """发射事件。"""

    @abstractmethod
    def flush(self) -> None:
        """刷新缓冲区。"""

    @abstractmethod
    def close(self) -> None:
        """关闭 sink。"""


class InMemoryTraceSink(TraceSink):
    """内存 Trace Sink（用于测试和 CLI）。"""
    def __init__(self) -> None:
        self._events: list[TraceEvent] = []
        self._lock = Lock()

    def emit(self, event: TraceEvent) -> None:
        with self._lock:
            self._events.append(event)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

    def get_events(self) -> list[TraceEvent]:
        with self._lock:
            return list(self._events)

    def get_events_by_run(self, run_id: str) -> list[TraceEvent]:
        with self._lock:
            return [e for e in self._events if e.run_id == run_id]


class JSONLTraceSink(TraceSink):
    """JSONL 文件 Trace Sink（用于持久化调试）。"""
    def __init__(self, path: str) -> None:
        import json
        self._path = path
        self._file = open(path, "a", encoding="utf-8")
        self._lock = Lock()
        self._json = json

    def emit(self, event: TraceEvent) -> None:
        with self._lock:
            line = self._json.dumps(event.to_dict(), ensure_ascii=False)
            self._file.write(line + "\n")

    def flush(self) -> None:
        with self._lock:
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            self._file.flush()
            self._file.close()


class CompositeTraceSink(TraceSink):
    """组合多个 Trace Sink。"""
    def __init__(self, sinks: list[TraceSink]) -> None:
        self._sinks = list(sinks)

    def emit(self, event: TraceEvent) -> None:
        for s in self._sinks:
            s.emit(event)

    def flush(self) -> None:
        for s in self._sinks:
            s.flush()

    def close(self) -> None:
        errors: list[Exception] = []
        for s in self._sinks:
            try:
                s.close()
            except Exception as e:
                errors.append(e)
        if errors:
            raise RuntimeError(f"关闭 TraceSink 时发生 {len(errors)} 个错误: {errors}")


class TraceMiddleware(Middleware[T]):
    """Trace 中间件。"""

    def __init__(self, sink: TraceSink) -> None:
        self._sink = sink
        self._current_event: TraceEvent | None = None

    def before(self, ctx: ExecutionContext) -> None:
        event = TraceEvent(
            event_id=ctx.span_id,
            parent_id=ctx.parent_span_id,
            run_id=ctx.run_id,
            thread_id=ctx.thread_id,
            task_id=ctx.task_id,
            operation=ctx.operation.name if ctx.operation else "unknown",
            name=ctx.operation_name,
            start=time.monotonic(),
            status="running",
        )
        self._current_event = event
        self._sink.emit(event)

    def success(self, ctx: ExecutionContext, result: T) -> T:
        if self._current_event:
            self._current_event.end = time.monotonic()
            self._current_event.status = "success"
        return result

    def error(self, ctx: ExecutionContext, exc: Exception) -> Exception:
        if self._current_event:
            self._current_event.end = time.monotonic()
            self._current_event.status = "error"
            self._current_event.error_type = type(exc).__name__
        return exc

    def finally_(self, ctx: ExecutionContext) -> None:
        if self._current_event:
            self._sink.emit(self._current_event)
            self._current_event = None


# =============================================================================
# 生命周期管理（18F）
# =============================================================================

class LifecycleResource(ABC):
    """可生命周期管理的资源。"""
    @abstractmethod
    def start(self) -> None:
        """启动资源。"""

    @abstractmethod
    def close(self) -> None:
        """关闭资源。"""


class LifecycleManager:
    """统一生命周期管理器。"""
    def __init__(self) -> None:
        self._resources: list[LifecycleResource] = []
        self._started = False

    def register(self, resource: LifecycleResource) -> None:
        if self._started:
            resource.start()
        self._resources.append(resource)

    def start_all(self) -> None:
        errors: list[Exception] = []
        for r in self._resources:
            try:
                r.start()
            except Exception as e:
                errors.append(e)
        self._started = True
        if errors:
            raise RuntimeError(
                f"启动资源时发生 {len(errors)} 个错误: {errors}"
            )

    def close_all(self) -> None:
        """反向关闭所有资源。"""
        errors: list[tuple[str, Exception]] = []
        for r in reversed(self._resources):
            try:
                r.close()
            except Exception as e:
                errors.append((type(r).__name__, e))
        self._resources.clear()
        self._started = False
        if errors:
            messages = [f"{name}: {e}" for name, e in errors]
            raise RuntimeError(f"关闭资源时发生 {len(errors)} 个错误: {'; '.join(messages)}")
