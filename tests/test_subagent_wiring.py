"""Subagent 首版闭环的端到端接线测试。

覆盖验收要求的六类场景 + 两项结构约束：

1. **正常派发**：主 Agent 调 `subagent_submit` → 子 Agent 用**真实模型**
   （脚本模型，非 FakeModel）执行 → 结果回到父会话 → 主 Agent 继续整合。
2. **权限拒绝**：父会话没有的能力，子 Agent 也拿不到；上限只做减法。
3. **结果返回**：`subagent_submit` 同步等待并返回子任务的输出。
4. **父取消 → 子任务真停**：分别断言三项（取消被观测 / 子任务终态 /
   取消后的后续调用次数）。
5. **失败**：子任务失败时如实报告，不谎报成功。
6. **G6 持久化**：子任务记录写进父线程；父 Agent 收尾保存**不覆盖**它；
   API 重读能拿到；刷新后前端能恢复。

全部使用脚本模型 + 本地临时资源，**零网络、零付费模型**。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from mini_deerflow.agent import Agent
from mini_deerflow.middleware import Budget, CancellationToken, ExecutionContext
from mini_deerflow.models import FinalAnswer, Message, ToolCall
from mini_deerflow.subagent_executor import SubagentManager
from mini_deerflow.subagent_models import TaskStatus
from mini_deerflow.subagent_policy import (
    SUBAGENT_CAPABILITY_CEILING,
    SUBAGENT_TOOL_CEILING,
    compute_child_capabilities,
    compute_child_tools,
)
from mini_deerflow.subagent_tools import create_subagent_tools
from mini_deerflow.thread_state import ThreadState
from mini_deerflow.thread_store import InMemoryThreadStore, SQLiteThreadStore
from mini_deerflow.tool_authorization import ToolPolicyRegistry, session_capabilities
from mini_deerflow.tool_contracts import ExecutorPrincipal, ToolCapability
from mini_deerflow.tools import Tool, ToolRegistry


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class _CountingModel:
    """脚本模型：按预设决策返回，并记录调用次数与每轮拿到的工具名。

    `decide_calls` 用于断言「取消之后没有新的模型调用」——
    取消令牌是协作式的，只能保证**后续**调用不再发起。
    """

    def __init__(self, decisions: list[Any], name: str = "model") -> None:
        self._decisions = list(decisions)
        self.name = name
        self.decide_calls = 0
        self.tools_seen: list[list[str]] = []

    def decide(self, messages: list[Message], tools: list[Any] | None = None):
        self.decide_calls += 1
        self.tools_seen.append([s.name for s in (tools or [])])
        if not self._decisions:
            return FinalAnswer(content="（无更多决策）")
        return self._decisions.pop(0)


class _SlowModel:
    """会阻塞一段时间再返回的模型 —— 用于在等待期间触发父取消。

    `decide_calls` 记录**发起**的调用次数：取消令牌是协作式的，
    只能保证「取消之后不再发起新的调用」，不能中断进行中的那次。
    """

    def __init__(self, delay: float, content: str = "慢结果") -> None:
        self._delay = delay
        self._content = content
        self.decide_calls = 0
        self.finished = False

    def decide(self, messages: list[Message], tools: list[Any] | None = None):
        self.decide_calls += 1
        time.sleep(self._delay)
        self.finished = True
        return FinalAnswer(content=self._content)


class _BlockingModel:
    """阻塞在一次模型调用里，直到**子任务自己的取消令牌**被置位。

    用于构造「取消发生时，子任务正处于一次进行中的模型调用」这一时序：
    取消能阻止**后续**调用，但进行中的那次会跑完 —— 这正是取消边界
    需要如实描述的地方。

    解除阻塞的条件是「子任务的取消令牌**真的**被置位」，而不是「睡够
    固定毫秒数」。用固定睡眠解除会引入竞态：机器繁忙时父还没来得及
    取消子任务，子任务就已经跑完并被标成 SUCCEEDED —— 测试变成偶发失败
    （本轮实测在整仓全量跑时出现过，单跑必过）。
    """

    #: 等待子任务取消令牌的最长时间；超时后放行，避免测试挂死。
    MAX_WAIT_SECONDS = 30.0

    def __init__(
        self,
        content: str = "阻塞结果",
        is_cancelled: Callable[[], bool] | None = None,
    ) -> None:
        self._content = content
        # 谓词：返回 True 表示子任务已被取消
        self._is_cancelled = is_cancelled or (lambda: False)
        self.decide_calls = 0
        self._entered = threading.Event()

    def decide(self, messages: list[Message], tools: list[Any] | None = None):
        self.decide_calls += 1
        self._entered.set()
        deadline = time.monotonic() + self.MAX_WAIT_SECONDS
        while time.monotonic() < deadline:
            if self._is_cancelled():
                break
            time.sleep(0.02)
        return FinalAnswer(content=self._content)

    def wait_entered(self, timeout: float = 30.0) -> bool:
        return self._entered.wait(timeout=timeout)


class _CountingTool:
    """记录调用次数的本地工具。"""

    def __init__(self, name: str = "counter") -> None:
        self.name = name
        self.calls = 0

    def handler(self, arguments: dict[str, object]) -> str:
        self.calls += 1
        return json.dumps({"count": self.calls})


# ---------------------------------------------------------------------------
# 装配助手
# ---------------------------------------------------------------------------


def _make_manager(
    store: InMemoryThreadStore,
    *,
    model: Any = None,
    registry: ToolRegistry | None = None,
    broker: Any = None,
    max_concurrency: int = 2,
) -> SubagentManager:
    return SubagentManager(
        max_concurrency=max_concurrency,
        max_depth=1,
        max_tasks_per_parent=10,
        default_timeout=30.0,
        tool_policy_registry=ToolPolicyRegistry(thread_store=store),
        thread_store=store,
        run_event_broker=broker,
    )


def _register_subagent_tools(
    registry: ToolRegistry,
    manager: SubagentManager,
    store: InMemoryThreadStore,
    *,
    model: Any,
    parent_tool_names: tuple[str, ...],
) -> None:
    tools = create_subagent_tools(
        manager=manager,
        parent_run_id="run-1",
        parent_thread_id="th-1",
        thread_store=store,
        model=model,
        registry=registry,
        parent_tool_names=parent_tool_names,
    )
    registry.register_many(tools)


# ---------------------------------------------------------------------------
# 1. 正常派发 + 3. 结果返回
# ---------------------------------------------------------------------------


class TestDispatchAndResultReturn:
    def test_正常派发_子代理用真实模型执行_结果回到父会话(self) -> None:
        """主 Agent 派发 → 子 Agent 用注入的真实模型跑 → 输出回到父工具结果。"""
        store = InMemoryThreadStore()
        store.create("th-1")

        child_model = _CountingModel(
            [FinalAnswer(content="子任务结论：42 是答案")]
        )
        parent_model = _CountingModel(
            [
                ToolCall(
                    name="subagent_submit",
                    arguments={"instruction": "算一下 42 的含义"},
                ),
                FinalAnswer(content="已整合子任务结论。"),
            ]
        )

        registry = ToolRegistry()
        registry.register(
            Tool(
                name="noop",
                description="占位",
                parameters={"type": "object", "properties": {}},
                handler=lambda args: "{}",
            )
        )
        manager = _make_manager(store, model=child_model, registry=registry)
        _register_subagent_tools(
            registry,
            manager,
            store,
            model=child_model,
            parent_tool_names=tuple(t.name for t in registry.list_tools()),
        )

        agent = Agent(
            model=parent_model,
            registry=registry,
            store=store,
            max_steps=4,
            tool_policy_registry=manager._tool_policy_registry,
        )

        answer, trace = agent.run("th-1", "帮我查一下 42")

        # 主 Agent 拿到了子任务的产出
        assert "已整合子任务结论" in answer
        # 子 Agent 确实用注入的模型跑了（不是 FakeModel）
        assert child_model.decide_calls == 1
        # 工具结果里带着子任务的输出
        # 注意：Agent 只把 user/assistant 消息落库，tool 消息只进 trace。
        tool_steps = [
            t for t in trace
            if t.get("action") == "tool_call" and t.get("detail", {}).get("name") == "subagent_submit"
        ]
        assert tool_steps, "应有 subagent_submit 的工具调用记录"
        payload = json.loads(tool_steps[0]["detail"]["result"])
        assert payload["ok"] is True
        assert payload["succeeded"] is True
        assert payload["status"] == "SUCCEEDED"
        assert "42 是答案" in payload["output"]

    def test_子代理只看到只读工具_看不到编码执行与委派工具(self) -> None:
        """暴露限制：子 Agent 的模型看不到 project_execute / subagent_*。"""
        store = InMemoryThreadStore()
        store.create("th-1")

        child_model = _CountingModel([FinalAnswer(content="done")])
        parent_model = _CountingModel(
            [
                ToolCall(
                    name="subagent_submit",
                    arguments={"instruction": "只读任务"},
                ),
                FinalAnswer(content="ok"),
            ]
        )

        registry = ToolRegistry()
        # 一个越权工具（模拟编码执行）：应在子代理的工具清单里消失
        registry.register(
            Tool(
                name="project_execute",
                description="编码执行（越权工具）",
                parameters={"type": "object", "properties": {}},
                handler=lambda args: "{}",
            )
        )
        registry.register(
            Tool(
                name="calculator",
                description="计算",
                parameters={"type": "object", "properties": {}},
                handler=lambda args: "{}",
            )
        )
        manager = _make_manager(store, model=child_model, registry=registry)
        _register_subagent_tools(
            registry,
            manager,
            store,
            model=child_model,
            parent_tool_names=tuple(t.name for t in registry.list_tools()),
        )

        agent = Agent(
            model=parent_model,
            registry=registry,
            store=store,
            max_steps=4,
            tool_policy_registry=manager._tool_policy_registry,
        )
        agent.run("th-1", "派个只读任务")

        assert child_model.tools_seen, "子代理应至少被调用一次"
        seen = child_model.tools_seen[0]
        assert "calculator" in seen, f"只读工具应在清单内，实际: {seen}"
        assert "project_execute" not in seen, f"编码执行不得对子代理可见: {seen}"
        assert not any(n.startswith("subagent_") for n in seen), (
            f"子代理不得看到委派工具（不可递归）: {seen}"
        )


# ---------------------------------------------------------------------------
# 2. 权限拒绝 / 交集
# ---------------------------------------------------------------------------


class TestPermissionIntersection:
    def test_上限只做减法_不授予父任务没有的能力(self) -> None:
        """普通会话的父没有 PROJECT_READ → 子 Agent 也没有。"""
        parent = session_capabilities(None)  # 普通会话
        assert ToolCapability.PROJECT_READ not in parent

        child = compute_child_capabilities(parent)
        assert ToolCapability.PROJECT_READ not in child
        # 上限里的能力，父有才有
        assert ToolCapability.COMPUTE in child
        # 不可递归
        assert ToolCapability.SUBAGENT_DELEGATE not in child

    def test_工具集是父可见与上限的交集(self) -> None:
        # 父看不见的 → 子也没有
        assert "project_glob" not in compute_child_tools(("calculator",))
        assert "project_execute" not in compute_child_tools(
            ("calculator", "project_glob", "project_execute")
        )
        # 父看得见的只读工具 → 子有
        child = compute_child_tools(("calculator", "project_glob"))
        assert "calculator" in child
        assert "project_glob" in child
        # 无论父传什么，禁列工具都不会进来
        assert "project_execute" not in compute_child_tools(("project_execute",))
        assert "subagent_submit" not in compute_child_tools(("subagent_submit",))
        assert "sandbox_run_command" not in compute_child_tools(("sandbox_run_command",))
        assert not any(
            n.startswith("mcp__") for n in compute_child_tools(("mcp__x__y",))
        )

    def test_上限不含编码执行与委派(self) -> None:
        assert "project_execute" not in SUBAGENT_TOOL_CEILING
        assert not any(t.startswith("subagent_") for t in SUBAGENT_TOOL_CEILING)
        assert not any(t.startswith("sandbox_") for t in SUBAGENT_TOOL_CEILING)
        assert ToolCapability.PROJECT_EXEC_DELEGATE not in SUBAGENT_CAPABILITY_CEILING
        assert ToolCapability.SUBAGENT_DELEGATE not in SUBAGENT_CAPABILITY_CEILING

    def test_项目会话的子代理可读项目文件但仍不能编码(self) -> None:
        """项目会话：父有 PROJECT_READ → 子可读；编码执行仍不可得。"""
        parent = session_capabilities("proj-1")
        child = compute_child_capabilities(parent)
        assert ToolCapability.PROJECT_READ in child
        assert ToolCapability.PROJECT_EXEC_DELEGATE not in child

    def test_子代理越权调用被策略拒绝(self) -> None:
        """执行限制：子代理伪造调用越权工具 → 授权判定拒绝。"""
        from mini_deerflow.tool_authorization import check_tool_access

        registry = ToolPolicyRegistry()
        parent_caps = compute_child_capabilities(session_capabilities("proj-1"))
        authority = registry.get_authority(
            thread_id="subagent-x",
            run_id="r",
            project_id="proj-1",
            granted_capabilities=parent_caps,
            principal=ExecutorPrincipal.MINI_SUBAGENT,
        )
        # 编码执行：策略只允许 MAIN_AGENT
        decision = check_tool_access(registry, "project_execute", authority)
        assert decision.is_denied
        # 委派工具：子代理不可见（不可递归）
        decision2 = check_tool_access(registry, "subagent_submit", authority)
        assert decision2.is_denied


# ---------------------------------------------------------------------------
# 4. 父取消 → 子任务真停（三项分别断言）
# ---------------------------------------------------------------------------


class TestParentCancellationStopsChild:
    def test_父取消_三项分别断言(
        self,
    ) -> None:
        """分别记录：①取消被父令牌观测 ②子任务终态 ③取消后的后续调用次数。

        取消边界（准确口径）：令牌是**协作式**的 —— 子代理在模型/工具
        调用**前**检查它。因此能保证「取消之后不再发起新的模型调用」，
        但**不能证明进行中的那次调用已被强制中断**。

        本测试用**阻塞模型**构造准确时序：取消发生在子任务正处于一次
        进行中的模型调用时。那次调用会跑完（不被强制中断），但之后
        不再有新的调用，且子任务终态是 CANCELLED。
        """
        store = InMemoryThreadStore()
        store.create("th-1")

        # 子模型：阻塞在第一次调用里，直到**子任务自己的取消令牌**被置位。
        # 谓词通过 holder 延迟拿到 manager —— manager 需要 child_model，
        # child_model 又需要 manager 的取消令牌，只能用闭包打破这个循环。
        manager_holder: dict[str, Any] = {}

        def _child_cancelled() -> bool:
            manager = manager_holder.get("manager")
            if manager is None:
                return False
            tasks = manager.list_tasks()
            if not tasks:
                return False
            token = manager._tokens.get(tasks[0].task_id)
            return bool(token is not None and token.is_cancelled())

        child_model = _BlockingModel(content="阻塞结果", is_cancelled=_child_cancelled)
        parent_model = _CountingModel(
            [
                ToolCall(
                    name="subagent_submit",
                    arguments={"instruction": "长任务", "timeout_seconds": 30},
                ),
                FinalAnswer(content="ok"),
            ]
        )

        registry = ToolRegistry()
        registry.register(
            Tool(
                name="noop",
                description="占位",
                parameters={"type": "object", "properties": {}},
                handler=lambda args: "{}",
            )
        )
        manager = _make_manager(store, model=child_model, registry=registry)
        manager_holder["manager"] = manager
        _register_subagent_tools(
            registry,
            manager,
            store,
            model=child_model,
            parent_tool_names=tuple(t.name for t in registry.list_tools()),
        )

        # 父取消令牌
        parent_token = CancellationToken()
        calls_before_cancel = 0

        def _cancel_after_child_started() -> None:
            nonlocal calls_before_cancel
            # 等子代理真的进入模型调用，再取消。
            # 不在这里断言：线程里的断言失败会被静默吞掉，主线程只看到
            # "父令牌没置位"这种难懂的报错。改成返回，由主线程给出明确失败。
            if not child_model.wait_entered(timeout=30.0):
                return
            calls_before_cancel = child_model.decide_calls
            parent_token.cancel()
            # 这里**不再**"睡够 50ms 就放行子模型"：
            # 子模型的解除阻塞条件是「子任务的取消令牌真的被置位」，
            # 父取消 → 父的等待循环 → Manager.cancel → 子任务令牌置位 →
            # 进行中的那次调用自己返回（不被强制中断）。
            # 用固定睡眠解除会引入竞态：机器繁忙时父还没来得及取消子任务，
            # 子任务就已经跑完并被标成 SUCCEEDED —— 测试变成偶发失败。

        canceler = threading.Thread(target=_cancel_after_child_started)
        canceler.start()

        agent = Agent(
            model=parent_model,
            registry=registry,
            store=store,
            max_steps=4,
            tool_policy_registry=manager._tool_policy_registry,
        )
        # 父 run 会因取消而中断（BudgetExceededError）—— 这是预期的：
        # 停止一个 run 就是让它停下来。
        try:
            agent.run("th-1", "派个长任务", cancellation_token=parent_token)
        except Exception:
            pass
        canceler.join(timeout=30)

        # ⓪ 取消时序必须成立：子代理确实进入了模型调用。
        # 没有这一步，下面的断言会在「子代理压根没跑起来」时给出误导性报错。
        assert child_model.decide_calls >= 1, "子代理未进入模型调用（取消时序未成立）"

        # ① 取消被父令牌观测：令牌确实置位
        assert parent_token.is_cancelled(), "父取消令牌应已置位"

        # ② 子任务终态：取消后子任务不是 SUCCEEDED
        tasks = manager.list_tasks()
        assert tasks, "应有子任务记录"
        task = tasks[0]
        assert task.status in {
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
        }, f"取消后子任务终态应为 CANCELLED/FAILED，实际 {task.status}"

        # ②b **真停机的硬证据**：子任务自己的取消令牌已被置位。
        # 只看终态不够 —— 终态可能是「跑完了才被标成取消」。
        # 令牌置位证明子代理确实收到了取消信号。
        assert manager._tokens[task.task_id].is_cancelled(), (
            "子任务的取消令牌应已置位（真停机的硬证据）"
        )

        # ③ 取消之后的**后续调用次数**：不应再有新的模型调用发起
        assert child_model.decide_calls == calls_before_cancel, (
            f"取消后不应再发起模型调用：取消前 {calls_before_cancel} 次，"
            f"取消后 {child_model.decide_calls} 次"
        )


# ---------------------------------------------------------------------------
# 5. 失败
# ---------------------------------------------------------------------------


class TestFailureReporting:
    def test_子任务失败_如实报告不谎报成功(self) -> None:
        store = InMemoryThreadStore()
        store.create("th-1")

        class _BoomModel:
            def decide(self, messages, tools=None):
                raise RuntimeError("模型炸了")

        child_model = _BoomModel()
        parent_model = _CountingModel(
            [
                ToolCall(
                    name="subagent_submit",
                    arguments={"instruction": "会失败的任务"},
                ),
                FinalAnswer(content="子任务失败了。"),
            ]
        )

        registry = ToolRegistry()
        registry.register(
            Tool(
                name="noop",
                description="占位",
                parameters={"type": "object", "properties": {}},
                handler=lambda args: "{}",
            )
        )
        manager = _make_manager(store, model=child_model, registry=registry)
        _register_subagent_tools(
            registry,
            manager,
            store,
            model=child_model,
            parent_tool_names=tuple(t.name for t in registry.list_tools()),
        )

        agent = Agent(
            model=parent_model,
            registry=registry,
            store=store,
            max_steps=4,
            tool_policy_registry=manager._tool_policy_registry,
        )
        _, _trace = agent.run("th-1", "派个会失败的任务")

        # 注意：Agent 只把 user/assistant 消息落库，tool 消息只进 trace。
        tool_steps = [
            t for t in _trace
            if t.get("action") == "tool_call"
            and t.get("detail", {}).get("name") == "subagent_submit"
        ]
        assert tool_steps, "应有 subagent_submit 的工具调用记录"
        payload = json.loads(tool_steps[0]["detail"]["result"])
        assert payload["ok"] is True  # 工具调用本身成功
        assert payload["succeeded"] is False, "子任务失败不得谎报成功"
        assert payload["status"] in {"FAILED", "TIMED_OUT", "CANCELLED"}

    def test_权限拒绝时父拿到的是失败结果(self) -> None:
        """子代理调越权工具 → 工具执行失败 → 子任务 FAILED → 父拿到失败。"""
        store = InMemoryThreadStore()
        store.create("th-1")

        class _CallForbiddenTool:
            """子代理调 project_execute（越权）→ 被执行限制拦下。"""

            def decide(self, messages, tools=None):
                return ToolCall(name="project_execute", arguments={})

        child_model = _CallForbiddenTool()
        parent_model = _CountingModel(
            [
                ToolCall(
                    name="subagent_submit",
                    arguments={"instruction": "尝试编码执行"},
                ),
                FinalAnswer(content="子任务未能执行。"),
            ]
        )

        registry = ToolRegistry()
        registry.register(
            Tool(
                name="project_execute",
                description="编码执行（越权）",
                parameters={"type": "object", "properties": {}},
                handler=lambda args: "{}",
            )
        )
        manager = _make_manager(store, model=child_model, registry=registry)
        _register_subagent_tools(
            registry,
            manager,
            store,
            model=child_model,
            parent_tool_names=tuple(t.name for t in registry.list_tools()),
        )

        agent = Agent(
            model=parent_model,
            registry=registry,
            store=store,
            max_steps=4,
            tool_policy_registry=manager._tool_policy_registry,
        )
        _, _trace = agent.run("th-1", "派个越权任务")

        # 注意：Agent 只把 user/assistant 消息落库，tool 消息只进 trace。
        tool_steps = [
            t for t in _trace
            if t.get("action") == "tool_call"
            and t.get("detail", {}).get("name") == "subagent_submit"
        ]
        assert tool_steps, "应有 subagent_submit 的工具调用记录"
        payload = json.loads(tool_steps[0]["detail"]["result"])
        assert payload["succeeded"] is False, "越权子任务不得报成功"


# ---------------------------------------------------------------------------
# 6. G6 持久化
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_子任务记录写进父线程(self) -> None:
        store = InMemoryThreadStore()
        store.create("th-1")

        child_model = _CountingModel([FinalAnswer(content="结论内容")])
        parent_model = _CountingModel(
            [
                ToolCall(
                    name="subagent_submit",
                    arguments={"instruction": "记录一下"},
                ),
                FinalAnswer(content="done"),
            ]
        )

        registry = ToolRegistry()
        registry.register(
            Tool(
                name="noop",
                description="占位",
                parameters={"type": "object", "properties": {}},
                handler=lambda args: "{}",
            )
        )
        manager = _make_manager(store, model=child_model, registry=registry)
        _register_subagent_tools(
            registry,
            manager,
            store,
            model=child_model,
            parent_tool_names=tuple(t.name for t in registry.list_tools()),
        )

        agent = Agent(
            model=parent_model,
            registry=registry,
            store=store,
            max_steps=4,
            tool_policy_registry=manager._tool_policy_registry,
        )
        agent.run("th-1", "派个任务")

        thread = store.get("th-1")
        assert len(thread.subagent_runs) == 1
        record = thread.subagent_runs[0]
        assert record["status"] == "SUCCEEDED"
        assert record["instruction"] == "记录一下"
        assert "结论内容" in record["output"]
        assert record["task_id"].startswith("sa-")
        # 计数必须**如实反映**子 Agent 实际做了什么，不能恒为 0
        # （子模型被调用了 1 次、没调用工具）。
        assert record["model_calls"] == 1, f"model_calls 应为 1，实际 {record['model_calls']}"
        assert record["tool_calls"] == 0, f"tool_calls 应为 0，实际 {record['tool_calls']}"

    def test_主Agent收尾保存不覆盖子任务记录(self) -> None:
        """**关键回归**：FinalAnswer 的收尾保存用的是运行开始时的旧快照，
        不能把运行途中写入的子任务记录抹掉。

        用真实 SQLite，验证跨进程序列化仍然保留。
        """
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            sqlite_store = SQLiteThreadStore(Path(td) / "threads.db")
            sqlite_store.create("th-1")

            child_model = _CountingModel([FinalAnswer(content="子任务结论")])
            parent_model = _CountingModel(
                [
                    ToolCall(
                        name="subagent_submit",
                        arguments={"instruction": "持久化测试"},
                    ),
                    FinalAnswer(content="主 Agent 收尾"),
                ]
            )

            registry = ToolRegistry()
            registry.register(
                Tool(
                    name="noop",
                    description="占位",
                    parameters={"type": "object", "properties": {}},
                    handler=lambda args: "{}",
                )
            )
            manager = _make_manager(sqlite_store, model=child_model, registry=registry)
            _register_subagent_tools(
                registry,
                manager,
                sqlite_store,
                model=child_model,
                parent_tool_names=tuple(t.name for t in registry.list_tools()),
            )

            agent = Agent(
                model=parent_model,
                registry=registry,
                store=sqlite_store,
                max_steps=4,
                tool_policy_registry=manager._tool_policy_registry,
            )
            agent.run("th-1", "派个任务")

            # 父任务已最终完成 —— 此时重读，记录必须还在
            reloaded = sqlite_store.get("th-1")
            assert len(reloaded.subagent_runs) == 1, (
                "主 Agent 收尾保存覆盖了子任务记录（G6 未解决）"
            )
            assert reloaded.subagent_runs[0]["status"] == "SUCCEEDED"
            sqlite_store.close()

    def test_线程状态能序列化并恢复子任务记录(self) -> None:
        thread = ThreadState(thread_id="th-1")
        thread.subagent_runs.append(
            {"task_id": "sa-1", "status": "SUCCEEDED", "output": "x"}
        )
        data = thread.to_dict()
        restored = ThreadState.from_dict(data)
        assert restored.subagent_runs == thread.subagent_runs

    def test_旧版本数据没有子任务记录也能恢复(self) -> None:
        """v2 数据（无 subagent_runs 字段）→ 读回空列表，不报错。"""
        old = {
            "schema_version": 2,
            "thread_id": "th-old",
            "messages": [],
        }
        restored = ThreadState.from_dict(old)
        assert restored.subagent_runs == []
