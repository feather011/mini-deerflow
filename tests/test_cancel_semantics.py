"""取消（停止）语义回归测试。

来源：TOOL-07 人工体验（真实 uvicorn + 真实 SSE）发现两处缺陷：

1. `Agent.run` 在预算未启用（API 默认 `disable_budget=True` → `budget=None`）
   时走取消分支会解引用 `self._budget.max_steps` → AttributeError，
   "停止"退化成"运行失败"。
2. `AgentRunService.run` 把取消映射成 `AgentBudgetExceededError`，
   SSE 终态是 `run.failed`，前端无法区分"用户点了停止"与"任务真失败"。

本文件锁定这两条语义：取消必须是 `AgentRunCancelledError` / `run.cancelled`。
"""

from __future__ import annotations

import pytest

from mini_deerflow.agent import Agent
from mini_deerflow.api.schemas.streaming import RunEventType, terminal_status_from_exception
from mini_deerflow.api.services.agent_runs import (
    AgentRunCancelledError,
    AgentRunService,
)
from mini_deerflow.middleware import BudgetExceededError, CancellationToken
from mini_deerflow.models import FinalAnswer
from mini_deerflow.thread_run_coordinator import ThreadRunCoordinator
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tools import ToolRegistry


# ---------------------------------------------------------------------------
# 1) Agent 层：预算未启用时取消不得抛 AttributeError
# ---------------------------------------------------------------------------

def test_取消_预算未启用时抛BudgetExceededError而非AttributeError():
    """budget=None（API 默认 disable_budget=True）时取消必须干净退出。"""
    store = InMemoryThreadStore()
    store.create("t1")

    class FakeModel:
        def decide(self, messages, tools=None):
            raise AssertionError("已取消的 run 不应再调用模型")

    agent = Agent(
        model=FakeModel(),
        registry=ToolRegistry(),
        store=store,
        budget=None,  # 复现 API 默认 disable_budget=True
    )

    token = CancellationToken()
    token.cancel()

    with pytest.raises(BudgetExceededError) as excinfo:
        agent.run("t1", "任意任务", cancellation_token=token)

    assert excinfo.value.resource == "steps"


# ---------------------------------------------------------------------------
# 2) API 服务层：已取消的 run 必须报"已取消"，SSE 终态为 run.cancelled
# ---------------------------------------------------------------------------

def _service(runner):
    store = InMemoryThreadStore()
    store.create("t1")
    return AgentRunService(
        runner=runner,
        store=store,
        coordinator=ThreadRunCoordinator(),
    )


def test_取消_服务层映射为AgentRunCancelledError():
    """取消令牌置位时，Agent 抛的 BudgetExceededError 必须映射为"已取消"。"""
    def runner(thread_id, user_input, run_id=None, cancellation_token=None):
        # 复现 Agent.run 取消分支的实际异常
        raise BudgetExceededError("steps", None)

    service = _service(runner)
    token = CancellationToken()
    token.cancel()

    with pytest.raises(AgentRunCancelledError):
        service.run("t1", "任意任务", cancellation_token=token)


def test_取消_SSE终态为run_cancelled():
    """已取消 → SSE 终态事件必须是 run.cancelled（不是 run.failed）。"""
    event_type = terminal_status_from_exception(AgentRunCancelledError("t1", "r1"))
    assert event_type is RunEventType.RUN_CANCELLED


def test_未取消_预算耗尽仍是AgentBudgetExceededError():
    """没有取消信号时，预算耗尽语义不变（不能被误判成取消）。"""
    from mini_deerflow.api.services.agent_runs import AgentBudgetExceededError

    def runner(thread_id, user_input, run_id=None, cancellation_token=None):
        raise BudgetExceededError("steps", 10)

    service = _service(runner)

    with pytest.raises(AgentBudgetExceededError):
        service.run("t1", "任意任务", cancellation_token=CancellationToken())


def test_正常完成_不受取消映射影响():
    """正常返回不被误判为取消。"""
    def runner(thread_id, user_input, run_id=None, cancellation_token=None):
        return ("ok", [])

    service = _service(runner)
    result = service.run("t1", "任意任务", cancellation_token=CancellationToken())
    assert result.status == "succeeded"
    assert result.answer == "ok"
