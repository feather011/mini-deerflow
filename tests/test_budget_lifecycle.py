"""BUDGET-01：运行级预算与停止生命周期。

覆盖五项验收：

1. 每次 run 新建预算，不同运行互不消耗（同一会话连续两轮、不同会话并发）
2. 同一 run 的主／子 Agent 共享额度上限（子代理消耗计入父运行，不能扩额）
3. 流式、非流式模型调用都计数，避免重复计数（脚本模型记录实际调用次数）
4. 额度耗尽后不再启动模型或工具（后续调用次数为零，终态与原因准确）
5. 取消、超时后释放资源，下一轮可正常开始

不调真实 Provider：全部用脚本模型 + 假客户端。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest

from mini_deerflow.agent import Agent
from mini_deerflow.middleware import (
    Budget,
    BudgetExceededError,
    CancellationToken,
    ChildBudget,
    ExecutionContext,
    build_child_budget,
)
from mini_deerflow.models import FinalAnswer, Message, ToolCall, ToolCalls, ToolSpec
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tools import Tool, ToolRegistry, create_default_registry


# ---------------------------------------------------------------------------
# 脚本模型：记录实际调用次数
# ---------------------------------------------------------------------------


class ScriptModel:
    """按脚本返回决策，并记录**实际被调用的次数**。

    只实现 `decide`（非流式路径）。流式路径由另一个类覆盖。
    """

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls = 0
        self.seen_messages: list[list[Message]] = []

    def decide(self, messages: list[Message], tools=None) -> Any:
        self.calls += 1
        self.seen_messages.append(list(messages))
        if not self._script:
            return FinalAnswer(content="没有更多脚本")
        return self._script.pop(0)


class ScriptStreamModel(ScriptModel):
    """实现 `stream_decide` 的脚本模型（走流式路径）。

    流式路径的额度语义：**一次模型决策 = 一次额度消耗**。
    本类不内部回调 decide()，因此其 `calls` 与额度计数一一对应。
    """

    def stream_decide(self, messages: list[Message], tools=None):
        self.calls += 1
        self.seen_messages.append(list(messages))
        decision = (
            self._script.pop(0) if self._script else FinalAnswer(content="没有更多脚本")
        )
        yield ("token", "")
        yield ("decision", decision)


def _agent(model, *, budget=None, registry=None, store=None):
    store = store or InMemoryThreadStore()
    store.create("t1")
    return Agent(
        model=model,
        registry=registry or create_default_registry(),
        store=store,
        budget=budget,
    )


def _budget(**kw) -> Budget:
    return Budget(max_steps=10, **kw)


# ---------------------------------------------------------------------------
# 1. 每次 run 新建预算，不同运行互不消耗
# ---------------------------------------------------------------------------


def test_同一会话连续两轮_第二轮额度从头算():
    """同一 Agent 连续跑两轮，第二轮不继承第一轮已消耗的额度。"""
    budget = _budget(max_model_calls=2)
    model = ScriptModel([
        FinalAnswer(content="第一轮"),
        FinalAnswer(content="第二轮"),
    ])
    agent = _agent(model, budget=budget)

    agent.run("t1", "第一轮")
    first_used = model.calls
    assert first_used == 1

    agent.run("t1", "第二轮")
    assert model.calls == 2, "两轮各调一次模型"

    # 模板 budget 自身**从未**被消耗（它只是上限模板）
    assert budget.model_calls() == 0


def test_不同会话并发_各自独立计数():
    """两个线程各跑一轮 → 各自持有独立计数器，互不消耗。"""
    budget = _budget(max_model_calls=1)
    model = ScriptModel([
        FinalAnswer(content="A"),
        FinalAnswer(content="B"),
    ])
    agent = _agent(model, budget=budget)
    agent._store.create("t2")

    errors: list[BaseException] = []

    def _run(thread_id: str) -> None:
        try:
            agent.run(thread_id, "并发")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=_run, args=("t1",)),
        threading.Thread(target=_run, args=("t2",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == [], f"并发两轮都不应因额度失败: {errors}"
    assert model.calls == 2, "两轮各调一次模型"


def test_每轮派生独立计数器_计数互不影响():
    """两次 run 派生出的计数器是两个独立对象。"""
    budget = _budget(max_model_calls=3)
    agent = _agent(ScriptModel([FinalAnswer(content="a")]), budget=budget)
    b1 = agent.new_run_budget()
    b2 = agent.new_run_budget()
    assert b1 is not b2
    b1.consume_model_call()
    assert b1.model_calls() == 1
    assert b2.model_calls() == 0
    assert budget.model_calls() == 0


# ---------------------------------------------------------------------------
# 2. 同一 run 的主／子 Agent 共享额度上限
# ---------------------------------------------------------------------------


def test_子代理消耗计入父运行():
    """子代理的模型调用必须记到**父运行**的同一个计数器上。"""
    parent = _budget(max_model_calls=5)
    child = build_child_budget(parent, None)
    assert child is parent, "没有局部上限时，父子共用同一个实例"

    child.consume_model_call()
    assert parent.model_calls() == 1


def test_模型自填局部上限_不能扩额():
    """模型给子代理填更大的额度 → 父额度仍然封顶。"""
    parent = _budget(max_model_calls=2)
    local = Budget(max_steps=5, max_model_calls=100, max_tool_calls=100)
    child = build_child_budget(parent, local)
    assert isinstance(child, ChildBudget)

    # 局部上限更宽松 → 实际生效的是父的 2 次
    child.consume_model_call()
    child.consume_model_call()
    assert parent.model_calls() == 2
    with pytest.raises(BudgetExceededError):
        child.consume_model_call()
    assert parent.model_calls() == 2, "被父额度挡下时父计数不再增长"


def test_模型自填局部上限_更严时先挡():
    """局部上限比父更严 → 局部先挡下，且父计数不多增长。"""
    parent = _budget(max_model_calls=10)
    local = Budget(max_steps=5, max_model_calls=1, max_tool_calls=10)
    child = build_child_budget(parent, local)

    child.consume_model_call()
    assert parent.model_calls() == 1
    with pytest.raises(BudgetExceededError):
        child.consume_model_call()
    assert parent.model_calls() == 1, "局部挡下时父计数不应增长"


def test_子代理任务数计入父运行():
    """提交子任务也要消耗父运行的额度。"""
    parent = _budget(max_subagent_tasks=1, max_subagent_depth=3)
    child = build_child_budget(parent, None)
    child.consume_subagent_task(1)
    assert parent.subagent_tasks() == 1
    with pytest.raises(BudgetExceededError):
        child.consume_subagent_task(1)


def test_继续委派_ChildBudget链到根():
    """复审 B1：持 `ChildBudget`（带局部额度）的子代理**再次派活**，
    孙代理的模型消耗必须仍沿链计入根预算。

    修复前 handler 只接受 `isinstance(ctx_budget, Budget)`，`ChildBudget`
    不是其子类 → parent_budget 被丢弃 → 链条在第二层断裂，根计数为 0。
    """
    from mini_deerflow.subagent_executor import SubagentManager
    from mini_deerflow.subagent_tools import create_subagent_tools

    root = Budget(
        max_steps=20, max_model_calls=5, max_subagent_tasks=10, max_subagent_depth=3
    )
    local = Budget(max_steps=5, max_model_calls=100, max_tool_calls=100)
    # 第一层子代理持有的视图（带局部额度 → ChildBudget，不是 Budget）
    mid = build_child_budget(root, local)
    assert isinstance(mid, ChildBudget)

    manager = SubagentManager(max_concurrency=1)
    submit_tool = next(
        t
        for t in create_subagent_tools(manager, "run-1", "t1")
        if t.name == "subagent_submit"
    )
    ctx = ExecutionContext(run_id="run-1", thread_id="t1", budget=mid)

    try:
        # 孙代理：模型自填更宽的局部 max_model_calls=100（不能扩根额）
        out = json.loads(
            submit_tool.handler(
                {"instruction": "干活", "max_model_calls": 100}, context=ctx
            )
        )
        assert out["ok"] is True
        # 提交动作沿 ChildBudget 链计入根
        assert root.subagent_tasks() == 1, "继续委派的提交必须计入根预算"

        status = _wait_terminal(manager, out["task_id"])
        assert status.name == "SUCCEEDED"

        # 孙代理的模型调用沿链（local2 → local → root）计入根；
        # 修复前链条断裂，根计数为 0。
        assert root.model_calls() >= 1, (
            f"继续委派后孙代理的模型消耗未计入根预算: {root.model_calls()}"
        )
        # 局部 max_model_calls=100 不能扩根额（根上限 5，实际远小于）
        assert root.model_calls() <= root.max_model_calls
    finally:
        manager.shutdown()


# ---------------------------------------------------------------------------
# 2b. 端到端：子代理经真实 submit handler 提交，消耗计入父运行
# ---------------------------------------------------------------------------


def _wait_terminal(manager, task_id: str, timeout: float = 30.0):
    """等子任务到达终态，返回 TaskStatus。"""
    from mini_deerflow.subagent_models import TaskStatus

    terminal = {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.TIMED_OUT,
    }
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = manager.get_status(task_id)
        if status in terminal:
            return status
        time.sleep(0.02)
    raise AssertionError(f"子任务 {task_id} 未在 {timeout}s 内到达终态")


def test_端到端_父Agent派活_子代理消耗计入父运行():
    """父 Agent 通过**真实的 subagent_submit 工具**派活 → 子代理的模型消耗
    记到**同一个父运行计数器**上，而不是凭空多出一份额度。"""
    from mini_deerflow.subagent_executor import SubagentManager
    from mini_deerflow.subagent_tools import create_subagent_tools

    store = InMemoryThreadStore()
    store.create("t1")
    manager = SubagentManager(max_concurrency=1)
    registry = ToolRegistry()
    for tool in create_subagent_tools(
        manager, parent_run_id="run-1", parent_thread_id="t1"
    ):
        registry.register(tool)

    parent_model = ScriptModel([
        ToolCall(name="subagent_submit", arguments={"instruction": "干活"}),
        FinalAnswer(content="父代理完成"),
    ])
    run_budget = Budget(
        max_steps=20,
        max_model_calls=10,
        max_subagent_tasks=3,
        max_subagent_depth=3,
    )
    agent = Agent(
        model=parent_model, registry=registry, store=store, budget=run_budget
    )

    try:
        # 显式传入本次运行的计数器 → 计数可观测（默认会派生一个等价的独立计数器）
        answer, _ = agent.run("t1", "派活", budget=run_budget)
        assert answer == "父代理完成"
        assert run_budget.subagent_tasks() == 1, "提交子任务必须计入父运行"

        tasks = manager.list_tasks()
        assert len(tasks) == 1
        status = _wait_terminal(manager, tasks[0].task_id)
        assert status.name == "SUCCEEDED", "子任务应正常完成"

        # 父自己 2 次模型调用（派活 + 收尾）+ 子代理 1 次 = 3，全部记在同一计数器
        assert run_budget.model_calls() == 3, (
            f"子代理消耗未计入父运行：{run_budget.model_calls()}"
        )
    finally:
        manager.shutdown()


def test_端到端_父额度耗尽_子代理不再启动模型():
    """父运行额度已耗尽 → 子代理**不得**再发起模型调用（模型自填更大额度也不行）。"""
    from mini_deerflow.subagent_executor import SubagentManager
    from mini_deerflow.subagent_tools import create_subagent_tools

    manager = SubagentManager(max_concurrency=1)
    submit_tool = next(
        t
        for t in create_subagent_tools(manager, "run-1", "t1")
        if t.name == "subagent_submit"
    )

    run_budget = Budget(
        max_steps=20,
        max_model_calls=0,  # 父额度已耗尽
        max_subagent_tasks=3,
        max_subagent_depth=3,
    )
    ctx = ExecutionContext(run_id="run-1", thread_id="t1", budget=run_budget)

    out = json.loads(
        submit_tool.handler(
            {"instruction": "干活", "max_model_calls": 100}, context=ctx
        )
    )
    assert out["ok"] is True
    task_id = out["task_id"]

    status = _wait_terminal(manager, task_id)
    assert status.name == "FAILED", "额度耗尽时子代理应失败，而不是照常跑完"
    result = manager.collect(task_id, timeout=2)
    assert result.error_type == "BudgetExhaustedError", (
        f"失败原因应是额度耗尽，实际: {result.error_type}"
    )
    assert run_budget.model_calls() == 0, "额度耗尽后不得放行任何模型调用"
    assert run_budget.steps() == 1, "子代理的步数同样记到父运行"
    assert run_budget.subagent_tasks() == 1, "提交动作本身仍计入父运行"
    manager.shutdown()


# ---------------------------------------------------------------------------
# 3. 流式、非流式模型调用都计数
# ---------------------------------------------------------------------------


def test_非流式模型调用计数():
    """非流式路径过去完全不消费 model_calls —— 现在必须计。"""
    budget = _budget(max_model_calls=10)
    model = ScriptModel([FinalAnswer(content="ok")])
    agent = _agent(model, budget=budget)
    # 显式传入本次运行的计数器 → 计数可观测
    run_budget = _budget(max_model_calls=10)

    agent.run("t1", "问题", budget=run_budget)

    assert model.calls == 1, "脚本模型被调用一次"
    assert run_budget.model_calls() == 1, "非流式路径必须计入一次额度"
    assert budget.model_calls() == 0, "模板只是上限来源，不该被消耗"


def test_流式模型调用计数():
    """流式路径同样计一次（一次决策 = 一次额度消耗）。"""
    model = ScriptStreamModel([FinalAnswer(content="ok")])
    agent = _agent(model, budget=_budget(max_model_calls=10))
    run_budget = _budget(max_model_calls=10)

    agent.run("t1", "问题", budget=run_budget)

    assert model.calls == 1
    assert run_budget.model_calls() == 1, "流式路径必须计入一次额度"


def test_额度计数与脚本模型调用次数一致():
    """两条路径下，额度计数都应等于脚本模型的**实际调用次数**。"""
    for model_cls in (ScriptModel, ScriptStreamModel):
        model = model_cls([FinalAnswer(content="ok")])
        agent = _agent(model, budget=_budget(max_model_calls=10))
        run_budget = _budget(max_model_calls=10)

        agent.run("t1", "问题", budget=run_budget)

        assert run_budget.model_calls() == model.calls, (
            f"{model_cls.__name__}: 额度计数应等于脚本模型实际调用次数"
        )


# ---------------------------------------------------------------------------
# 4. 额度耗尽后不再启动模型或工具
# ---------------------------------------------------------------------------


def test_额度耗尽后不再启动模型():
    """max_model_calls=1 → 第二轮决策不发起模型调用。"""
    budget = _budget(max_model_calls=1)
    model = ScriptModel([
        ToolCall(name="calculator", arguments={"expression": "1"}),
        FinalAnswer(content="不该到这里"),
    ])
    agent = _agent(model, budget=budget)

    with pytest.raises(BudgetExceededError) as exc:
        agent.run("t1", "计算 1")

    assert exc.value.resource == "model_calls"
    assert model.calls == 1, "额度耗尽后不得再发起模型调用"


def test_额度耗尽后不再启动工具():
    """工具额度耗尽 → 工具不执行，终态原因准确。"""
    budget = _budget(max_model_calls=10, max_tool_calls=0)
    model = ScriptModel([
        ToolCall(name="calculator", arguments={"expression": "1"}),
        FinalAnswer(content="不该到这里"),
    ])
    executed: list[str] = []
    registry = ToolRegistry()
    registry.register(Tool(
        name="calculator",
        description="记录调用",
        parameters={"type": "object", "properties": {}},
        handler=lambda args: (executed.append("calculator"), "2")[1],
    ))
    agent = _agent(model, budget=budget, registry=registry)

    with pytest.raises(BudgetExceededError) as exc:
        agent.run("t1", "计算 1")

    assert exc.value.resource == "tool_calls"
    assert executed == [], "额度耗尽后工具不得执行"


# ---------------------------------------------------------------------------
# 5. 取消、超时后释放资源，下一轮可正常开始
# ---------------------------------------------------------------------------


def test_取消后下一轮可正常开始():
    """取消的那一轮耗掉的额度**不牵连**下一轮。"""
    budget = _budget(max_model_calls=1)
    token = CancellationToken()
    model = ScriptModel([
        ToolCall(name="calculator", arguments={"expression": "1"}),
        FinalAnswer(content="第二轮"),
    ])
    registry = ToolRegistry()
    registry.register(Tool(
        name="calculator",
        description="取消令牌",
        parameters={"type": "object", "properties": {}},
        handler=lambda args: (token.cancel(), "2")[1],
    ))
    agent = _agent(model, budget=budget, registry=registry)

    with pytest.raises(BudgetExceededError):
        agent.run("t1", "计算 1", cancellation_token=token)

    # 下一轮：新计数器，额度从头算
    model2 = ScriptModel([FinalAnswer(content="第二轮")])
    agent2 = Agent(model=model2, registry=create_default_registry(),
                   store=agent._store, budget=budget)
    answer, _ = agent2.run("t1", "再来一次")
    assert answer == "第二轮"
    assert model2.calls == 1


def test_取消后父运行的额度不被下一轮继承():
    """取消的那一轮消耗不留在**模板**上 → 下一轮额度完好。"""
    budget = _budget(max_model_calls=1)
    token = CancellationToken()
    model = ScriptModel([FinalAnswer(content="x")])
    agent = _agent(model, budget=budget)
    agent.run("t1", "第一轮", cancellation_token=token)
    assert budget.model_calls() == 0


# ---------------------------------------------------------------------------
# 5b. 实际 API 入口：停止后重试
# ---------------------------------------------------------------------------


def test_API入口_停止后重试可正常开始():
    """走 API 服务层（`AgentRunService.run`）：停止的那一轮之后，下一轮正常。

    这是「取消、超时后释放资源，下一轮可正常开始」的**实际入口**验证：
    同一个 Agent、同一个额度模板，先跑一轮被停止，再跑一轮成功。
    """
    from mini_deerflow.api.services.agent_runs import (
        AgentRunCancelledError,
        AgentRunService,
    )
    from mini_deerflow.thread_run_coordinator import ThreadRunCoordinator

    store = InMemoryThreadStore()
    store.create("t1")

    token = CancellationToken()
    calls = {"n": 0}

    class _Model:
        def decide(self, messages, tools=None):
            calls["n"] += 1
            # 第一轮：执行途中被停止
            if calls["n"] == 1:
                token.cancel()
            return FinalAnswer(content="ok")

    budget = _budget(max_model_calls=1)
    agent = Agent(
        model=_Model(),
        registry=create_default_registry(),
        store=store,
        budget=budget,
    )
    service = AgentRunService(
        runner=agent.run,
        store=store,
        coordinator=ThreadRunCoordinator(),
    )

    # 第一轮：被取消 → 报"已取消"（不是预算耗尽）
    with pytest.raises(AgentRunCancelledError):
        service.run("t1", "第一轮", cancellation_token=token)

    # 第二轮：同一 Agent、同一额度模板 → 额度完好，正常完成
    result = service.run("t1", "第二轮", cancellation_token=CancellationToken())
    assert result.answer == "ok"
    assert calls["n"] == 2


def test_API入口_额度耗尽_终态与原因准确且下一轮可开始():
    """额度耗尽在 API 入口映射为**明确的终态与原因**（不是笼统失败），
    且下一轮用新的运行计数器，额度从头算，可以正常开始。"""
    from mini_deerflow.api.services.agent_runs import (
        AgentBudgetExceededError,
        AgentRunService,
    )
    from mini_deerflow.thread_run_coordinator import ThreadRunCoordinator

    store = InMemoryThreadStore()
    store.create("t1")
    calls = {"n": 0}

    class _Model:
        def decide(self, messages, tools=None):
            calls["n"] += 1
            if calls["n"] == 1:
                # 第一轮：先调一次工具，把额度用在模型调用上
                return ToolCall(name="calculator", arguments={"expression": "1"})
            return FinalAnswer(content="ok")

    budget = Budget(max_steps=10, max_model_calls=1, max_tool_calls=5)
    agent = Agent(
        model=_Model(),
        registry=create_default_registry(),
        store=store,
        budget=budget,
    )
    service = AgentRunService(
        runner=agent.run,
        store=store,
        coordinator=ThreadRunCoordinator(),
    )

    # 第一轮：额度耗尽 → 明确的「预算耗尽」终态，原因指向 model_calls
    with pytest.raises(AgentBudgetExceededError) as exc:
        service.run("t1", "第一轮")
    assert exc.value.resource == "model_calls"
    assert exc.value.limit == 1
    assert calls["n"] == 1, "额度耗尽后不得再发起模型调用"

    # 第二轮：新的运行计数器 → 额度从头算，正常完成
    result = service.run("t1", "第二轮")
    assert result.answer == "ok"
    assert calls["n"] == 2
    # 构造参数里的 budget 只是**额度上限模板** —— 两轮都不该消耗它
    assert budget.model_calls() == 0, "额度模板不应被任何一轮消耗"


# ---------------------------------------------------------------------------
# 6. 独立复审回归：B1 / B2 / B3
# ---------------------------------------------------------------------------


class _SlowModel:
    """模型调用耗时可控（用来制造「越过截止时间才返回」）。"""

    def __init__(self, delay: float, content: str = "迟到的答案") -> None:
        self._delay = delay
        self._content = content
        self.calls = 0

    def decide(self, messages, tools=None):
        self.calls += 1
        time.sleep(self._delay)
        return FinalAnswer(content=self._content)


def _子任务预算() -> Budget:
    return Budget(
        max_steps=10,
        max_model_calls=5,
        max_tool_calls=5,
        max_subagent_tasks=5,
        max_subagent_depth=3,
    )


def test_复审B2_模型越过截止时间_子任务不以成功收口():
    """复审 B2：模型在截止时间之后才返回 → 子任务**不得**是 SUCCEEDED。

    修复前：模型返回后直接采纳 `FinalAnswer`，越过 deadline 的子任务被标成
    SUCCEEDED —— 超时没有收口。现在以 `TIMED_OUT` 收口，迟到的答案不被采纳。
    """
    from mini_deerflow.subagent_executor import SubagentManager

    manager = SubagentManager(max_concurrency=1)
    model = _SlowModel(delay=1.0)
    try:
        task = manager.submit(
            instruction="干活",
            parent_run_id="run-1",
            parent_thread_id="t1",
            timeout_seconds=0.5,  # 截止时间 500ms
            budget=_子任务预算(),
            model=model,
        )
        status = _wait_terminal(manager, task.task_id, timeout=30)
        assert status.name == "TIMED_OUT", (
            f"越过截止时间返回的子任务应以超时收口，实际: {status.name}"
        )
        assert model.calls == 1, "模型确实被调用过（越过截止时间发生在调用期间）"

        result = manager.collect(task.task_id, timeout=5)
        assert result.error_type == "TimeoutError", (
            f"失败原因应是超时，实际: {result.error_type}"
        )
        assert result.output == "", "迟到的答案不得被采纳"
    finally:
        manager.shutdown()


def test_复审B2_宽松时限_子任务正常成功():
    """复审 B2 对照组：时限宽松时，同样的慢模型**照常成功**。"""
    from mini_deerflow.subagent_executor import SubagentManager

    manager = SubagentManager(max_concurrency=1)
    model = _SlowModel(delay=0.2, content="正常答案")
    try:
        task = manager.submit(
            instruction="干活",
            parent_run_id="run-1",
            parent_thread_id="t1",
            timeout_seconds=30.0,
            budget=_子任务预算(),
            model=model,
        )
        status = _wait_terminal(manager, task.task_id, timeout=30)
        assert status.name == "SUCCEEDED", f"时限宽松时应成功，实际: {status.name}"
        result = manager.collect(task.task_id, timeout=5)
        assert result.output == "正常答案"
    finally:
        manager.shutdown()


def test_复审B2_主动取消_仍是取消而非超时():
    """复审 B2：主动取消的语义**保留** —— 仍是 CANCELLED，不是 TIMED_OUT。"""
    from mini_deerflow.subagent_executor import SubagentManager

    manager = SubagentManager(max_concurrency=1)
    model = _SlowModel(delay=1.0)
    try:
        task = manager.submit(
            instruction="干活",
            parent_run_id="run-1",
            parent_thread_id="t1",
            timeout_seconds=30.0,
            budget=_子任务预算(),
            model=model,
        )
        assert manager.cancel(task.task_id) is True
        status = _wait_terminal(manager, task.task_id, timeout=30)
        assert status.name == "CANCELLED", f"主动取消应为 CANCELLED，实际: {status.name}"
        result = manager.collect(task.task_id, timeout=5)
        assert result.error_type == "CancelledError"
    finally:
        manager.shutdown()


def test_复审B3_预算耗尽_SSE终态带资源与上限():
    """复审 B3：额度耗尽后，SSE 终态事件必须带上**受控的**预算资源与上限。

    修复前：终态只有 `Run 失败 (AgentBudgetExceededError)`，
    `model_calls` 与上限 1 丢失，客户端消费链拿不到原因。
    """
    from concurrent.futures import ThreadPoolExecutor

    from mini_deerflow.api.schemas.streaming import RunEventType
    from mini_deerflow.api.services.agent_runs import AgentRunService
    from mini_deerflow.api.services.event_broker import RunEventBroker
    from mini_deerflow.api.services.run_stream import RunStreamManager
    from mini_deerflow.thread_run_coordinator import ThreadRunCoordinator

    store = InMemoryThreadStore()
    store.create("t1")

    class _Model:
        def __init__(self) -> None:
            self.n = 0

        def decide(self, messages, tools=None):
            self.n += 1
            if self.n == 1:
                # 先调一次工具，把额度用在模型调用上
                return ToolCall(name="calculator", arguments={"expression": "1"})
            return FinalAnswer(content="ok")

    budget = Budget(max_steps=10, max_model_calls=1, max_tool_calls=5)
    agent = Agent(
        model=_Model(),
        registry=create_default_registry(),
        store=store,
        budget=budget,
    )
    coordinator = ThreadRunCoordinator()
    service = AgentRunService(runner=agent.run, store=store, coordinator=coordinator)
    broker = RunEventBroker()
    executor = ThreadPoolExecutor(max_workers=1)
    manager = RunStreamManager(
        broker=broker,
        agent_run_service=service,
        coordinator=coordinator,
        executor=executor,
    )

    try:
        handle = manager.submit("t1", "跑一下")
        terminal = None
        while True:
            event = handle.queue.get(timeout=30)
            event_type = getattr(event, "event_type", None)
            if event_type is None:
                continue  # 队列里也可能有非事件对象（如溢出标记）
            if event_type in (
                RunEventType.RUN_FAILED,
                RunEventType.RUN_COMPLETED,
                RunEventType.RUN_TIMED_OUT,
                RunEventType.RUN_CANCELLED,
            ):
                terminal = event
                break

        assert terminal is not None, "未收到终态事件"
        assert terminal.event_type == RunEventType.RUN_FAILED, (
            f"额度耗尽应为 run.failed，实际: {terminal.event_type}"
        )
        error_text = terminal.error or ""
        assert "model_calls" in error_text, (
            f"终态事件应带出预算资源，实际: {error_text!r}"
        )
        assert "上限: 1" in error_text, (
            f"终态事件应带出预算上限，实际: {error_text!r}"
        )
    finally:
        executor.shutdown(wait=True)
        broker.close()


def test_复审B3_预算文案不含任意异常文本():
    """复审 B3 约束：只输出**受控属性**，不透传任意异常文本。"""
    from mini_deerflow.api.schemas.streaming import safe_failure_message
    from mini_deerflow.api.services.agent_runs import AgentBudgetExceededError
    from mini_deerflow.middleware import BudgetExceededError

    text = safe_failure_message(AgentBudgetExceededError("model_calls", 1))
    assert "model_calls" in text and "上限: 1" in text

    # 领域层的异常（未包装成 API 异常）同样可读
    text2 = safe_failure_message(BudgetExceededError("tool_calls", 7))
    assert "tool_calls" in text2 and "上限: 7" in text2

    # 未知异常不得泄漏原始 message
    text3 = safe_failure_message(RuntimeError("C:\\secret\\path\\token"))
    assert "secret" not in text3
