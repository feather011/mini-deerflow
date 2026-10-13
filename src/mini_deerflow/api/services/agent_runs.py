"""AgentRunService（19C 阶段）。

职责：
- 验证目标 Thread 存在；
- 控制同一 Thread 的并发运行（通过 ThreadRunCoordinator）；
- 调用现有 Agent（IntegratedAgent），复用其 Middleware / Budget / Trace；
- 将 Agent 异常与运行时错误转换为稳定领域异常；
- 不 import FastAPI；不返回 API Pydantic Schema。
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Protocol

from mini_deerflow.middleware import BudgetExceededError, CancellationToken
from mini_deerflow.thread_run_coordinator import ThreadRunCoordinator
from mini_deerflow.thread_store import ThreadNotFoundError, ThreadStore


_LOGGER = logging.getLogger(__name__)


# --- Runner Protocol ---


class Runner(Protocol):
    """Agent 执行的最小接口。

    接受可选 run_id；调用方提供时原样使用（贯通全链路），
    缺省时实现方自行生成。
    """

    def __call__(
        self,
        thread_id: str,
        user_input: str,
        run_id: str | None = None,
        cancellation_token: "CancellationToken | None" = None,
    ) -> tuple[str, list[dict]]: ...


# --- 领域异常 ---


class AgentRunError(Exception):
    """AgentRunService 领域异常基类。"""


class AgentThreadNotFoundError(AgentRunError):
    """目标 Thread 不存在。"""

    def __init__(self, thread_id: str) -> None:
        super().__init__(f"未知线程: {thread_id}")
        self.thread_id = thread_id


class AgentRunValidationError(AgentRunError):
    """请求体非法（message 为空 / 过长等）。

    注意：Pydantic 已在校验层抛出 422；此异常用于绕过 Schema 的额外业务校验。
    """


class AgentBudgetExceededError(AgentRunError):
    """预算耗尽；映射 HTTP 429。"""

    def __init__(self, resource: str, limit: int | None) -> None:
        super().__init__(f"预算耗尽: {resource}（上限: {limit}）")
        self.resource = resource
        self.limit = limit


class AgentRunTimeoutError(AgentRunError):
    """Run 超时；映射 HTTP 504。

    timeout 字段可能为 None：表示底层抛 TimeoutError 但未携带时长信息（当前阶段）。
    """

    def __init__(
        self,
        thread_id: str,
        run_id: str,
        timeout: float | None,
    ) -> None:
        if timeout is None:
            msg = f"Run 超时: thread_id={thread_id} run_id={run_id} (duration unknown)"
        else:
            msg = f"Run 超时: thread_id={thread_id} run_id={run_id} timeout={timeout}s"
        super().__init__(msg)
        self.thread_id = thread_id
        self.run_id = run_id
        self.timeout = timeout


class AgentRunCancelledError(AgentRunError):
    """Run 被取消；映射 HTTP 503（与 not ready 一致；非 500）。"""

    def __init__(self, thread_id: str, run_id: str) -> None:
        super().__init__(f"Run 被取消: thread_id={thread_id} run_id={run_id}")
        self.thread_id = thread_id
        self.run_id = run_id


class AgentRunFailedError(AgentRunError):
    """未分类的执行失败；映射 HTTP 500。

    message 是稳定的对外文案，不包含原始异常 / 路径 / 凭据；
    原始异常由 service 层记录到日志，路由层只把 message 透传给客户端。
    """

    DEFAULT_MESSAGE = "Internal execution error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.DEFAULT_MESSAGE)
        self.message = message or self.DEFAULT_MESSAGE


# --- 领域结果（与 API Schema 解耦） ---


@dataclass(frozen=True)
class AgentRunResult:
    """一次 run 的领域结果；路由层负责映射为 Pydantic Schema。"""

    run_id: str
    thread_id: str
    status: str  # RunStatus 值
    answer: str | None = None
    error: str | None = None


# --- Service ---


class AgentRunService:
    """Agent run 业务编排。

    Args:
        runner: 满足 Runner Protocol 的可调用对象；
                接受可选 run_id（不传时实现方自生成）。
        store: ThreadStore，用于在调用前确认 thread 存在。
        coordinator: 同进程 ThreadRunCoordinator。
    """

    def __init__(
        self,
        runner: Runner,
        store: ThreadStore,
        coordinator: ThreadRunCoordinator,
    ) -> None:
        self._runner = runner
        self._store = store
        self._coordinator = coordinator

    @staticmethod
    def generate_run_id() -> str:
        """生成 API 层 run_id。

        这是本阶段唯一的 run_id 生成入口；调用方拿到后会原样
        传递给 Runner 实现，保证全链路使用同一值。
        """
        return f"run-{uuid.uuid4().hex[:12]}"

    def store_exists(self, thread_id: str) -> bool:
        """判断 thread 是否存在（Stream 等其他调用方复用）。

        显式暴露为 service 方法，避免外部直接接触 store。
        """
        try:
            self._store.get(thread_id)
            return True
        except ThreadNotFoundError:
            return False

    def run(
        self,
        thread_id: str,
        message: str,
        *,
        run_id: str | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> AgentRunResult:
        """执行 Agent run；包含线程存在校验、并发占用、执行、释放。

        Args:
            thread_id: 目标 Thread。
            message: 用户输入。
            run_id: 可选；提供时原样使用（Stream 场景由上层生成保证贯通全链路）。
                缺省时由 service 自身生成。

        Raises:
            AgentThreadNotFoundError: thread 不存在。
            AgentRunValidationError: message 非法（理论上路由层 Schema 已校验）。
            ThreadRunConflictError: 同 Thread 已有 run 进行中。
            AgentBudgetExceededError: Budget 耗尽。
            AgentRunTimeoutError: 超时（timeout=None 表示时长未知）。
            AgentRunCancelledError: 被取消（保留扩展点）。
            AgentRunFailedError: 未分类失败。
        """
        # 1. 校验目标 Thread 存在
        try:
            self._store.get(thread_id)
        except ThreadNotFoundError as exc:
            raise AgentThreadNotFoundError(thread_id) from exc

        # 2. 业务二次校验
        if not message or not message.strip():
            raise AgentRunValidationError("message 不能为空或纯空白")

        # 3. 唯一 run_id 生成点；外部未提供时由 service 生成
        run_id = run_id or self.generate_run_id()
        self._coordinator.try_acquire(thread_id, run_id)

        try:
            try:
                # 把 cancellation_token 透传给 IntegratedAgent.run → Agent.run → 工具
                answer, _trace = self._runner(
                    thread_id, message, run_id=run_id, cancellation_token=cancellation_token
                )
            except BudgetExceededError as exc:
                # 根据 resource 区分"主动停止"与"模型超时"
                if exc.resource == "model_call_timeout":
                    raise AgentRunTimeoutError(
                        thread_id, run_id, timeout=None
                    ) from exc
                raise AgentBudgetExceededError(exc.resource, exc.limit) from exc
            except TimeoutError as exc:
                # 底层抛 TimeoutError 但未携带真实时长；保留 None 以诚实表达
                raise AgentRunTimeoutError(
                    thread_id, run_id, timeout=None
                ) from exc
            except AgentRunCancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 顶层兜底映射
                _LOGGER.exception(
                    "Agent run failed thread_id=%s run_id=%s", thread_id, run_id
                )
                raise AgentRunFailedError() from exc

            return AgentRunResult(
                run_id=run_id,
                thread_id=thread_id,
                status="succeeded",
                answer=answer,
            )
        finally:
            self._coordinator.release(thread_id, run_id=run_id)