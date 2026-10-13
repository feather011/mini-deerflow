"""RunStreamManager（19D 阶段）。

职责：
- 把 StreamRequest 拆解为：占位 → broker.subscribe → executor.submit(Agent.run)；
- 订阅必须在 Agent 任务启动前完成（保证 run.started 不丢）；
- 阻塞 Agent.run 在有界 ThreadPoolExecutor 中执行；
- 终态事件由本类统一发布到 broker（run.completed/failed/timed_out/cancelled）；
- 终态事件本身是流的结束标志；broker.publish 内部标记终态后拒绝后续事件；
- 客户端断开只停止 broker.publish；不取消正在跑的 Agent。

依赖：
- RunEventBroker：事件分发；
- AgentRunService：复用 19C 业务编排（thread 存在 / 并发占用 / 异常映射）；
- ThreadRunCoordinator：并发占用；
- ThreadPoolExecutor：bounded max_workers；
"""
from __future__ import annotations

import logging
import queue
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from mini_deerflow.api.schemas.streaming import (
    RunEvent,
    RunEventType,
    safe_failure_message,
    terminal_status_from_exception,
)
from mini_deerflow.api.services.agent_runs import (
    AgentRunError,
    AgentRunResult,
    AgentRunService,
    AgentRunTimeoutError,
)
from mini_deerflow.api.services.event_broker import RunEventBroker, iter_run_events
from mini_deerflow.middleware import CancellationToken
from mini_deerflow.thread_run_coordinator import ThreadRunCoordinator, ThreadRunConflictError


_LOGGER = logging.getLogger(__name__)


# --------------------------------------------------------------------------- result


@dataclass(frozen=True)
class StreamHandle:
    """Stream 启动返回：run_id + 订阅队列 + 提交 future。"""

    run_id: str
    queue: queue.Queue[RunEvent | object]
    future: Future[Any]
    thread_id: str


class StreamThreadNotFoundError(Exception):
    """Thread 不存在（路由层映射 404）。"""


class StreamThreadBusyError(Exception):
    """Thread 已被占用（路由层映射 409）。"""

    def __init__(self, thread_id: str) -> None:
        super().__init__(f"线程 {thread_id} 正在运行")
        self.thread_id = thread_id


class StreamValidationError(Exception):
    """message 非法（路由层映射 422）。"""


# --------------------------------------------------------------------------- manager


class RunStreamManager:
    """SSE run 流管理器。

    设计原则：
    - subscribe 必须在 submit 之前；提交后客户端立刻消费 → run.started 不丢；
    - Agent.run 是同步阻塞 → 放入有界 ThreadPoolExecutor；
    - 终态事件由本类统一发（broker.publish）；终态本身即结束标志；
    - 客户端断开 → 仅停止 publish；不杀 Agent；
    - 关闭 executor + broker.close() 时应停止接受新请求并优雅退出。
    """

    def __init__(
        self,
        broker: RunEventBroker,
        agent_run_service: AgentRunService,
        coordinator: ThreadRunCoordinator,
        executor: ThreadPoolExecutor,
    ) -> None:
        self._broker = broker
        self._service = agent_run_service
        self._coordinator = coordinator
        self._executor = executor
        # 按 thread_id 存储取消令牌（一次只有一个 run）
        self._tokens: dict[str, CancellationToken] = {}

    # ---------------------------------------------------------------- entry

    def submit(self, thread_id: str, message: str) -> StreamHandle:
        """启动一次流式 run。

        顺序（严格）：
        1. 业务校验（message 非空）→ StreamValidationError；
        2. 目标 Thread 存在性校验 → StreamThreadNotFoundError；
        3. 生成 run_id；
        4. coordinator.try_acquire → 已被占用时抛 StreamThreadBusyError；
        5. broker.subscribe(run_id) → 客户端可立即消费（避免 run.started 丢失）；
        6. executor.submit(_execute) → 进入 worker 线程；
        7. 返回 StreamHandle。
        """
        self._validate_message(message)

        if not self._service_thread_exists(thread_id):
            raise StreamThreadNotFoundError(thread_id)

        run_id = AgentRunService.generate_run_id()

        # 4. 占位（同 thread 已有 run → 409 业务冲突）
        try:
            self._coordinator.try_acquire(thread_id, run_id)
        except ThreadRunConflictError as exc:
            raise StreamThreadBusyError(thread_id) from exc

        # 5. 订阅（必须在 executor.submit 之前）
        q = self._broker.subscribe(run_id)

        # 6. 创建取消令牌并注册
        token = CancellationToken()
        self._tokens[thread_id] = token

        # 7. 提交阻塞任务（传入取消令牌）
        future = self._executor.submit(
            self._execute, thread_id, message, run_id, token
        )

        return StreamHandle(run_id=run_id, queue=q, future=future, thread_id=thread_id)

    # ---------------------------------------------------------------- execution

    def _execute(
        self, thread_id: str, message: str, run_id: str, token: CancellationToken
    ) -> None:
        """在线程池 worker 中执行 Agent.run（含取消令牌链路）。

        异常分类：
        - 同步返回 AgentRunResult → 发出 run.completed；
        - AgentRunTimeoutError → run.timed_out；
        - AgentRunError 其他子类 → run.failed；
        - 其他异常 → 兜底 run.failed（安全文案）。
        """
        try:
            result = self._service.run(
                thread_id, message, run_id=run_id, cancellation_token=token
            )
        except AgentRunTimeoutError as exc:
            self._emit_terminal(
                run_id,
                thread_id,
                RunEventType.RUN_TIMED_OUT,
                error=safe_failure_message(exc),
            )
            return
        except AgentRunError as exc:
            _LOGGER.exception(
                "Stream run failed thread_id=%s run_id=%s", thread_id, run_id
            )
            event_type = terminal_status_from_exception(exc)
            self._emit_terminal(run_id, thread_id, event_type, error=safe_failure_message(exc))
            return
        except Exception as exc:  # noqa: BLE001 - 顶层兜底
            _LOGGER.exception("Stream run unhandled thread_id=%s run_id=%s", thread_id, run_id)
            self._emit_terminal(run_id, thread_id, RunEventType.RUN_FAILED, error=safe_failure_message(exc))
            return

        # 成功路径
        self._emit_terminal(
            run_id,
            thread_id,
            RunEventType.RUN_COMPLETED,
            answer=result.answer,
        )

    # ---------------------------------------------------------------- terminal

    def _emit_terminal(
        self,
        run_id: str,
        thread_id: str,
        event_type: RunEventType,
        error: str | None = None,
        answer: str | None = None,
    ) -> None:
        """发布终态事件。

        broker.publish 内部会 drain 队列剩余中间事件，
        将终态入队，并标记 run_id 为终态（后续事件全部丢弃）。
        """
        seq = self._broker.next_sequence(run_id)
        event = RunEvent(
            run_id=run_id,
            thread_id=thread_id,
            event_type=event_type,
            sequence=seq,
            error=error,
            answer=answer,
        )
        self._broker.publish_terminal(run_id, event)
        # 清理取消令牌
        self._tokens.pop(thread_id, None)

    # ---------------------------------------------------------------- stop

    def stop(self, thread_id: str) -> bool:
        """停止指定 Thread 上的运行中的 run。

        找到对应 token 并 cancel()：Agent.run 每步检查 is_cancelled()，
        project_execute 透传给 HarnessRunner，子进程被回收。

        Returns:
            True = 已找到并取消；False = 该 Thread 没有正在运行的 run。
        """
        token = self._tokens.get(thread_id)
        if token is None:
            return False
        token.cancel()
        return True

    # ---------------------------------------------------------------- shutdown

    def shutdown(self) -> None:
        """关闭 executor + broker。

        - executor.shutdown(wait=False)：不等待已完成任务；
        - broker.close()：向所有队列送入关闭哨兵，让消费方退出。
        """
        try:
            self._executor.shutdown(wait=False, cancel_futures=False)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Executor shutdown failed")
        self._broker.close()

    # ---------------------------------------------------------------- helpers

    def _validate_message(self, message: str) -> None:
        if not message or not message.strip():
            raise StreamValidationError("message 不能为空或纯空白")

    def _service_thread_exists(self, thread_id: str) -> bool:
        return self._service.store_exists(thread_id)


# 引用：避免未使用警告
_ = iter_run_events
