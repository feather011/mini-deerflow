"""中间件系统（18A 阶段）。

提供统一的执行管线：Agent run、Model call、Tool call、Subagent task。
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Generic, TypeVar
from threading import Lock

from mini_deerflow.trace_safety import safe_trace_detail

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
    # BUDGET-01：本次运行的预算。可以是 `Budget` 本体，也可以是子代理的
    # `ChildBudget` 视图（复审 B1：继续委派时原样传递，链条不中断）。
    budget: "Budget | ChildBudget | None" = None
    parent_span_id: str | None = None
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    # TRACE-01：本次调用的 in-flight span。
    #
    # 放在 ctx 上而不是 TraceMiddleware 实例上：实例被并发运行共享时
    # （agent_pipeline 的那个实例被 API 的线程池共用），实例级状态会
    # 张冠李戴 —— 有的事件永远停在 running，有的被写进别的调用的记录里。
    # 挂在 ctx 上则天然按调用隔离：并发、嵌套各有一份。
    trace_event: "TraceEvent | None" = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # 25C: token 流式回调。Agent.run 在 MODEL_CALL 时设置此字段，
    # 值为一个累积 token 文本的列表引用；model.stream_decide() 每 yield 一个 token
    # 就 append 进去。RunEventMiddleware.success() 读取此字段，逐 token 发布 model.token 事件。
    token_accumulator: "list[str] | None" = None
    # TOOL-02：工具授权检查通过后的已授予能力集合，供 handler（如 subagent_submit）读取并传递给子任务
    granted_capabilities: frozenset = field(default_factory=frozenset)

    def is_cancelled(self) -> bool:
        return self.cancellation_token.is_cancelled() if self.cancellation_token else False


# =============================================================================
# CancellationToken
# =============================================================================

class CancellationToken:
    """协作取消令牌。

    TRACE-01：记录**首次**取消的原因（`cancel_reason()`）。

    「用户主动停止」与「模型调用超时」在 Agent 层都会置位**同一个**令牌
    （模型超时靠 `threading.Timer` 调 `cancel()` 实现）——
    光看 `is_cancelled()` 分不出二者，Trace 的终态分类必须看原因。

    首次取消的原因胜出：用户先点了停止、随后模型超时定时器才到期时，
    原因应保持「用户停止」，不能被后来的超时覆盖。
    """

    __slots__ = ("_cancelled", "_lock", "_reason")

    def __init__(self) -> None:
        self._cancelled = False
        self._lock = Lock()
        self._reason: str | None = None

    def cancel(self, reason: str = "cancelled") -> None:
        """置位取消。**首次取消的原因胜出**，后到的原因不覆盖。"""
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            self._reason = reason

    def is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def cancel_reason(self) -> str | None:
        """首次取消的原因；未取消时为 None。"""
        with self._lock:
            return self._reason

    def cancel_state(self) -> tuple[bool, str | None]:
        """取消状态与原因的**一致快照**（一次加锁拿到两者）。

        分两次读 `is_cancelled()` 与 `cancel_reason()` 之间可能插入一次
        `cancel()`，读到的会是不自洽的组合（"没取消" + "有原因"）——
        终态分类据此会把「用户停止」误判成 `error`。
        判定终态必须用本方法，不能分两次读。
        """
        with self._lock:
            return self._cancelled, self._reason


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

        TRACE-01：每次重试**换一个新 span** —— 复用同一个 `span_id` 会让
        同一 `event_id` 出现多条终态。重试链本身也是可走的：新 attempt 的
        父节点是上一个 attempt。
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
                # 上一个 attempt 成为新 attempt 的父节点
                ctx.parent_span_id = ctx.span_id
                ctx.span_id = uuid.uuid4().hex
                ctx.attempt += 1
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

    def derive(self) -> "Budget":
        """按**同样的额度上限**派生一个独立的计数器（计数从 0 开始）。

        BUDGET-01：预算的**生命周期**必须与「一次运行」对齐，而不是与进程
        对齐。进程级共享的计数器会让**运行 A 消耗掉运行 B 的额度**。

        用法：Agent 持有一个只作为「上限模板」的 Budget 实例，每次 run() 用
        `derive()` 派生一个本次运行专属的计数器。父子 Agent 共享**同一个**
        派生实例 → 子代理消耗真实计入父运行。

        只复制上限，不复制计数；新实例有自己的锁，与原实例互不影响。
        """
        return Budget(
            max_steps=self.max_steps,
            max_model_calls=self.max_model_calls,
            max_tool_calls=self.max_tool_calls,
            max_subagent_tasks=self.max_subagent_tasks,
            max_subagent_depth=self.max_subagent_depth,
            max_wall_time_seconds=self.max_wall_time_seconds,
            max_summary_calls=self.max_summary_calls,
        )

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

    # ------------------------------------------------------------------
    # restore_usage：从持久化恢复使用量（用于重启恢复场景）
    # 只能调用一次，用于将 Budget 恢复到历史记录的消费状态
    # ------------------------------------------------------------------

    def restore_usage(
        self,
        *,
        steps: int | None = ...,
        model_calls: int | None = ...,
        tool_calls: int | None = ...,
        subagent_tasks: int | None = ...,
        summary_calls: int | None = ...,
    ) -> None:
        """恢复使用量（用于从持久化存储恢复状态）。

        只能调用一次，用于在重启后将 Budget 恢复到历史记录的消费状态。
        参数值直接设置为内部计数器，不做上限检查（超限由下一次 consume 报错）。

        Args:
            steps: 恢复的 step 消耗
            model_calls: 恢复的模型调用消耗
            tool_calls: 恢复的工具调用消耗
            subagent_tasks: 恢复的子任务消耗
            summary_calls: 恢复的摘要调用消耗

        Raises:
            RuntimeError: 如果已经调用过 restore_usage
            RuntimeError: 如果已有消耗记录（尚未消费过）
            TypeError: 如果参数类型不是整数
            ValueError: 如果参数为负数
        """
        # 先验证所有参数（失败时不标记已调用，以便测试重试）
        if steps is not ...:
            if not isinstance(steps, int) or isinstance(steps, bool):
                raise TypeError("steps 必须是整数")
            if steps < 0:
                raise ValueError("steps 不能为负")

        if model_calls is not ...:
            if not isinstance(model_calls, int) or isinstance(model_calls, bool):
                raise TypeError("model_calls 必须是整数")
            if model_calls < 0:
                raise ValueError("model_calls 不能为负")

        if tool_calls is not ...:
            if not isinstance(tool_calls, int) or isinstance(tool_calls, bool):
                raise TypeError("tool_calls 必须是整数")
            if tool_calls < 0:
                raise ValueError("tool_calls 不能为负")

        if subagent_tasks is not ...:
            if not isinstance(subagent_tasks, int) or isinstance(subagent_tasks, bool):
                raise TypeError("subagent_tasks 必须是整数")
            if subagent_tasks < 0:
                raise ValueError("subagent_tasks 不能为负")

        if summary_calls is not ...:
            if not isinstance(summary_calls, int) or isinstance(summary_calls, bool):
                raise TypeError("summary_calls 必须是整数")
            if summary_calls < 0:
                raise ValueError("summary_calls 不能为负")

        with self._lock:
            # 检查是否已经调用过
            if hasattr(self, "_restore_usage_called") and self._restore_usage_called:
                raise RuntimeError("restore_usage 只能调用一次")

            # 验证：检查是否已有消耗记录
            for field_name in ("_steps", "_model_calls", "_tool_calls", "_subagent_tasks", "_summary_calls"):
                if getattr(self, field_name, 0) > 0:
                    raise RuntimeError("restore_usage 必须在尚未消费过任何资源时调用")

            # 设置标志（防止重复调用）- 仅在所有验证通过后设置
            self._restore_usage_called = True

            # 设置计数器值（只处理显式传入的值）
            if steps is not ...:
                self._steps = steps

            if model_calls is not ...:
                self._model_calls = model_calls

            if tool_calls is not ...:
                self._tool_calls = tool_calls

            if subagent_tasks is not ...:
                self._subagent_tasks = subagent_tasks

            if summary_calls is not ...:
                self._summary_calls = summary_calls


class BudgetExceededError(Exception):
    """预算耗尽。"""
    def __init__(self, resource: str, limit: int | None) -> None:
        self.resource = resource
        self.limit = limit
        super().__init__(
            f"预算耗尽：{resource}（上限: {limit}）" if limit else f"预算耗尽：{resource}"
        )


# --- 子代理额度视图（BUDGET-01） ---


class ChildBudget:
    """子代理的额度视图：**父运行的额度 + （可选）模型自填的更严局部上限**。

    两条规则同时成立：

    1. **计入父运行** —— 每次消耗都记到父运行的同一个 Budget 实例上，
       父的 `max_model_calls` / `max_tool_calls` 对子代理同样生效；
    2. **不能扩额** —— 模型自填的局部上限只能**收紧**：局部先检查（更严的
       先挡），父额度随后照常计数。

    过去这里直接用独立的 `SubagentBudget` 顶替父额度，子代理的消耗完全不
    进父计数器 —— 等于凭空多出一份额度。
    """

    __slots__ = ("_parent", "_local")

    def __init__(
        self,
        parent: "Budget | ChildBudget | None",
        local: Budget | None,
    ) -> None:
        # 复审 B1：parent 允许再套一层 ChildBudget —— 带局部额度的子代理
        # 继续委派时，上一层的 ChildBudget 视图原样传进来，链条一路链到根。
        self._parent = parent
        self._local = local

    # --- 额度消耗 ---

    def consume_step(self) -> None:
        if self._local is not None:
            self._local.consume_step()
        if self._parent is not None:
            self._parent.consume_step()

    def consume_model_call(self) -> None:
        if self._local is not None:
            self._local.consume_model_call()
        if self._parent is not None:
            self._parent.consume_model_call()

    def consume_tool_call(self) -> None:
        if self._local is not None:
            self._local.consume_tool_call()
        if self._parent is not None:
            self._parent.consume_tool_call()

    def consume_subagent_task(self, depth: int) -> None:
        if self._parent is not None:
            self._parent.consume_subagent_task(depth)

    def consume_summary_call(self) -> None:
        if self._local is not None:
            self._local.consume_summary_call()
        if self._parent is not None:
            self._parent.consume_summary_call()

    # --- 只读属性：子代理用 max_steps 决定自己的步数上限 ---

    @property
    def max_steps(self) -> int | None:
        """局部上限优先（更严的那个）；只有一边有时取那一边。"""
        if self._local is not None and self._local.max_steps is not None:
            return self._local.max_steps
        return self._parent.max_steps if self._parent is not None else None

    @property
    def max_model_calls(self) -> int | None:
        if self._local is not None and self._local.max_model_calls is not None:
            return self._local.max_model_calls
        return self._parent.max_model_calls if self._parent is not None else None

    @property
    def max_tool_calls(self) -> int | None:
        if self._local is not None and self._local.max_tool_calls is not None:
            return self._local.max_tool_calls
        return self._parent.max_tool_calls if self._parent is not None else None

    @property
    def max_subagent_depth(self) -> int | None:
        return self._parent.max_subagent_depth if self._parent is not None else None

    def check_depth(self, depth: int) -> bool:
        if self._parent is not None:
            return self._parent.check_depth(depth)
        return True


def build_child_budget(
    parent: "Budget | ChildBudget | None",
    local: Budget | None,
) -> "Budget | ChildBudget | None":
    """组合父运行额度与模型自填的局部上限。

    - 父有、局部无 → 直接用父的实例（父子共享同一个计数器）；
    - 父有、局部有 → 用 `ChildBudget` 视图（局部只能收紧）；
    - 父无、局部有 → 用局部的（原语义：调用方自持额度）；
    - 两边都无 → None（不限制）。

    复审 B1：`parent` 允许是上一层的 `ChildBudget` 视图 —— 带局部额度的
    子代理继续委派时，链条 `ChildBudget(ChildBudget(...root...), local)`
    一路向上，根计数器不丢。
    """
    if parent is None:
        return local
    if local is None:
        return parent
    return ChildBudget(parent, local)


class BudgetMiddleware(Middleware[T]):
    """预算检查中间件。"""

    def __init__(self, budget: Budget) -> None:
        self._budget = budget

    def before(self, ctx: ExecutionContext) -> None:
        # BUDGET-01：只在 ctx 尚未带预算时发布本中间件的预算。
        # 调用方（Agent.run）会先把**本次运行**的预算放进 ctx —— 那是
        # 与运行对齐的计数器；这里若无条件覆盖，就会把 per-run 预算
        # 换回进程级的模板实例，父子共享与「不同运行互不消耗」都会失效。
        if ctx.budget is None:
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
    """Trace 事件。

    两种形态共存：

    - `kind == "span"`：中间件产生的一次调用，两段式（running + 终态），
      两条记录共用同一个 `event_id`；
    - `kind == "context"`：上下文工程层手工发出的**一次性**事件
      （见 `context_shared.emit_event`），只有终态一条。

    `kind` 用来把两者分开：上下文事件的 operation 也叫 `MODEL_CALL` /
    `TOOL_CALL`，与 span **同名**，不区分就会把两串事件混成一条链。
    """

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
        kind: str = "span",
        error_detail: str | None = None,
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
        self.status = status
        self.error_type = error_type
        self.metadata = metadata or {}
        self.kind = kind
        # TRACE-01：受控的失败原因（见 `trace_safety.safe_trace_detail`）。
        # 绝不放行未识别的原始异常 message。
        self.error_detail = error_detail

    @property
    def duration(self) -> float | None:
        """耗时；尚未收口时为 None。

        做成 property 而不是构造时算一次：终态是**事后**补上的 `end`，
        构造时算出来的 duration 永远是 None。
        """
        if self.end is None:
            return None
        return self.end - self.start

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
            "kind": self.kind,
        }
        if self.task_id:
            result["task_id"] = self.task_id
        if self.end is not None:
            result["end"] = self.end
            result["duration"] = self.duration
        if self.error_type:
            result["error_type"] = self.error_type
        if self.error_detail:
            result["error_detail"] = self.error_detail
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
    """JSONL 文件 Trace Sink（用于持久化调试）。

    **两级失败策略**（TRACE-01 收口）：

    - **启动期**（构造时路径不可写：目录不存在、无权限、路径是目录）：
      **直接报错**。此时还没有任何工作发生，明确失败最省事；
      也避免「配了 Trace 但什么都没落盘」这种最难排查的状态。
    - **运行期**（写入时出错：磁盘满、文件系统故障）：
      **不中断 Agent 运行**，但**绝不静默** —— 记一条 ERROR 日志
      （明确说明本次运行的 Trace 不完整），并把该 sink 标记为 degraded，
      后续事件直接跳过（不反复失败、不刷日志）。

    为什么要分开：Trace 是**显式开启的观测能力**（默认关闭），不是业务事实源
    —— 业务事实（线程消息、确认请求）走 ThreadStore / ConfirmationStore。
    已经完成的用户工作不应因为「调试轨迹写不进去」而作废；
    但「轨迹悄悄少了一半」比失败更难排查，所以运行期失败必须留下 ERROR 记录。
    """

    def __init__(self, path: str) -> None:
        import json
        import os

        self._path = path
        self._file = None
        self._lock = Lock()
        self._json = json
        self._degraded = False
        try:
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            self._file = open(path, "a", encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(
                f"Trace 文件不可写：{path}（{exc.strerror or exc}）。"
                "请确认目录存在且当前用户有写权限，或改用其它 trace_path。"
            ) from exc

    def emit(self, event: TraceEvent) -> None:
        """写一条事件。写入失败**不抛异常**（不中断 Agent 运行），但记 ERROR。"""
        with self._lock:
            if self._degraded or self._file is None:
                return
            try:
                line = self._json.dumps(event.to_dict(), ensure_ascii=False)
                self._file.write(line + "\n")
            except (OSError, ValueError) as exc:
                # ValueError：文件已被外部关闭（此时 write 会抛 ValueError）。
                self._degraded = True
                _LOGGER.error(
                    "Trace 写入失败，本次运行的 Trace 不完整"
                    "（后续事件不再写入）：path=%s errno=%s err=%s",
                    self._path,
                    getattr(exc, "errno", None),
                    getattr(exc, "strerror", None) or exc,
                )

    def flush(self) -> None:
        with self._lock:
            if self._degraded or self._file is None:
                return
            try:
                self._file.flush()
            except (OSError, ValueError) as exc:
                self._degraded = True
                _LOGGER.error(
                    "Trace 刷新失败，本次运行的 Trace 可能不完整：path=%s err=%s",
                    self._path,
                    exc,
                )

    def close(self) -> None:
        """关闭文件。关闭失败只记 ERROR，不抛异常（不打断运行时关闭流程）。"""
        with self._lock:
            file = self._file
            self._file = None
            if file is None:
                return
            try:
                file.flush()
                file.close()
            except (OSError, ValueError) as exc:
                _LOGGER.error(
                    "Trace 文件关闭失败：path=%s err=%s", self._path, exc
                )


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


def classify_terminal_status(ctx: ExecutionContext, exc: BaseException | None) -> str:
    """把一次调用的失败收口成终态：`error` / `cancelled` / `timeout`。

    判定顺序（TRACE-01 返修：**已确认取消优先**）：

    1. 取消令牌已置位、且原因**不是**模型超时 → `cancelled`
       —— 用户主动停止。模型超时在 Agent 层也是「置位同一个令牌」，
       所以不能只看 `is_cancelled()`，必须看首次取消的原因。
    2. 取消原因是 `model_timeout` → `timeout`（真正超时）。
    3. `BudgetExceededError("model_call_timeout")` → `timeout`
       —— 没有令牌时的模型超时（运行未带取消令牌）。
    4. `TimeoutError` 家族（含 `ModelTimeoutError`）→ `timeout`。
    5. 其余 → `error`。

    与 API 层（`api/services/agent_runs.py`）的关系：
    两者读**同一个**底层事实 —— `CancellationToken` 记录的首次取消原因，
    各自映射到自己的公开状态（不再各判各的）：

    - 内部事实 `model_timeout` → Trace 记 `timeout`；
      SSE 本轮**保持兼容**记 `run.cancelled`（不改 SSE 契约）。
    - 内部事实 `cancelled`（用户停止）→ 两边都记 `cancelled`。

    SSE 兼容映射只影响**表示层**（终态事件名）：清理 / 恢复 / 持久化
    路径由取消令牌本身驱动（`unregister_run(cancelled=token.is_cancelled())`
    → `_cancelled_runs` → `_is_run_alive`），停止与超时两种情况行为一致，
    不依赖终态事件的命名。
    """
    from mini_deerflow.model_errors import ModelTimeoutError

    token = ctx.cancellation_token
    # 一次加锁取到「是否取消 + 首次原因」的一致快照：
    # 分两次读可能读到「没取消但有原因」的矛盾组合，把用户停止误判成 error。
    cancelled, reason = token.cancel_state() if token is not None else (False, None)

    if cancelled and reason != "model_timeout":
        # 已确认的取消（用户主动停止）优先于异常类型。
        return "cancelled"
    if reason == "model_timeout":
        return "timeout"
    if getattr(exc, "resource", None) == "model_call_timeout":
        return "timeout"
    if isinstance(exc, (TimeoutError, ModelTimeoutError)):
        return "timeout"
    if cancelled:
        return "cancelled"
    return "error"


class TraceMiddleware(Middleware[T]):
    """Trace 中间件。

    TRACE-01：span 状态挂在 `ExecutionContext` 上（`ctx.trace_event`），
    **不再挂在 middleware 实例上**。实例被并发运行共享时，实例级单槽位
    会张冠李戴 —— 有的事件永远停在 running（悬空），有的被重复写终态。

    每个 span 发两条记录：一条 running、一条终态，两条共用同一个
    `event_id`。running 那条**不被改写** —— 改写会让「按 event_id 分组」
    看到两条终态。终态记录是**新对象**。
    """

    def __init__(self, sink: TraceSink) -> None:
        self._sink = sink

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
            kind="span",
            metadata={"attempt": ctx.attempt},
        )
        ctx.trace_event = event
        self._sink.emit(event)

    def success(self, ctx: ExecutionContext, result: T) -> T:
        self._finish(ctx, status="success")
        return result

    def error(self, ctx: ExecutionContext, exc: Exception) -> Exception:
        self._finish(
            ctx,
            status=classify_terminal_status(ctx, exc),
            error_type=type(exc).__name__,
            error_detail=safe_trace_detail(exc),
        )
        return exc

    def finally_(self, ctx: ExecutionContext) -> None:
        event = ctx.trace_event
        if event is None:
            return
        if event.status != "running":
            # 已有终态（success / error 已写过）—— 不重复写。
            ctx.trace_event = None
            return
        # 兜底：`MiddlewarePipeline.execute` 只捕获 `Exception`，
        # `BaseException` 逃逸时不会经过 error 钩子 —— 这里必须收口，
        # 否则 span 永远停在 running（悬空）。
        exc = sys.exc_info()[1]
        self._finish(
            ctx,
            status=classify_terminal_status(ctx, exc),
            error_type=type(exc).__name__ if exc is not None else None,
            error_detail=safe_trace_detail(exc),
        )
        ctx.trace_event = None

    def _finish(
        self,
        ctx: ExecutionContext,
        *,
        status: str,
        error_type: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        """写终态记录（每个 span 至多一条）。"""
        running = ctx.trace_event
        if running is None or running.status != "running":
            # 已经被收口过（重复调用 / 其它中间件已处理）—— 不重复写。
            return
        terminal = TraceEvent(
            event_id=running.event_id,
            parent_id=running.parent_id,
            run_id=running.run_id,
            thread_id=running.thread_id,
            task_id=running.task_id,
            operation=running.operation,
            name=running.name,
            start=running.start,
            end=time.monotonic(),
            status=status,
            error_type=error_type,
            metadata=dict(running.metadata),
            kind=running.kind,
            error_detail=error_detail,
        )
        ctx.trace_event = terminal
        self._sink.emit(terminal)


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
