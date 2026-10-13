"""IntegratedAgent 上下文用量捕获测试。

覆盖：
- `Agent.last_assembled` property 暴露
- `IntegratedAgent._capture_last_usage` 在每次 run 后写入按线程字典
- 不同 Thread 互不串数据
- 压缩前后对照（compacted=True 时 tokens_before 有值）
- 未启用 ContextAssembler 时静默跳过（get_last_usage 永远返回 None）
"""
from __future__ import annotations

import pytest

from mini_deerflow.agent import Agent
from mini_deerflow.context_assembler import ContextAssembler
from mini_deerflow.context_state import ContextPolicy
from mini_deerflow.context_store import InMemoryContextStore
from mini_deerflow.middleware import Budget
from mini_deerflow.models import FinalAnswer, Message, ToolCall
from mini_deerflow.runtime import IntegratedAgent, RuntimeConfig
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.token_counter import HeuristicTokenCounter
from mini_deerflow.tools import ToolRegistry


THREAD_A = "thread-A"
THREAD_B = "thread-B"


def _policy(max_tokens: int = 8000) -> ContextPolicy:
    return ContextPolicy(
        max_context_tokens=max_tokens,
        reserved_output_tokens=1000,
        safety_margin_tokens=500,
        compaction_threshold=0.8,
    )


def _assembler(policy: ContextPolicy) -> ContextAssembler:
    """最小可用的 ContextAssembler（无 compactor）。"""
    return ContextAssembler(
        policy=policy,
        counter=HeuristicTokenCounter(),
        context_store=InMemoryContextStore(),
    )


class _ScriptedModel:
    """按队列返回决策的最小 Model 替身。

    不依赖 `models.FakeModel`（后者是固定规则的模拟器，无法注入响应）。
    """

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls: int = 0

    def decide(self, messages, tools=None):
        self.calls += 1
        if not self._responses:
            return FinalAnswer(content="ok")
        resp = self._responses.pop(0)
        if isinstance(resp, BaseException):
            raise resp
        return resp


def _make_integrated(
    assembler: ContextAssembler | None,
    *,
    model: _ScriptedModel | None = None,
) -> tuple[IntegratedAgent, InMemoryThreadStore]:
    """构造 IntegratedAgent 与预先创建好两个 Thread 的共享 store。"""
    store = InMemoryThreadStore()
    store.create(THREAD_A)
    store.create(THREAD_B)
    inner = Agent(
        model=model or _ScriptedModel([FinalAnswer(content="ok")]),
        registry=ToolRegistry(),
        store=store,
        context_assembler=assembler,
    )
    return IntegratedAgent(agent=inner, config=RuntimeConfig()), store


# ---------------------------------------------------------------------------
# Agent.last_assembled property
# ---------------------------------------------------------------------------


def test_agent_last_assembled_is_none_before_run() -> None:
    agent = Agent(
        model=_ScriptedModel([FinalAnswer(content="ok")]),
        registry=ToolRegistry(),
        store=InMemoryThreadStore(),
    )
    assert agent.last_assembled is None


def test_agent_last_assembled_is_set_after_run() -> None:
    store = InMemoryThreadStore()
    store.create(THREAD_A)
    inner = Agent(
        model=_ScriptedModel([FinalAnswer(content="ok")]),
        registry=ToolRegistry(),
        store=store,
        context_assembler=_assembler(_policy()),
    )
    ia = IntegratedAgent(agent=inner, config=RuntimeConfig())
    ia.run(THREAD_A, "hi")
    assert inner.last_assembled is not None
    assert inner.last_assembled.context.breakdown.total >= 0


# ---------------------------------------------------------------------------
# IntegratedAgent.get_last_usage
# ---------------------------------------------------------------------------


def test_no_assembler_returns_none() -> None:
    """未启用 ContextAssembler 时始终返回 None，不抛错。"""
    ia, _ = _make_integrated(None)
    ia.run(THREAD_A, "hi")
    assert ia.get_last_usage(THREAD_A) is None


def test_returns_snapshot_for_thread() -> None:
    store = InMemoryThreadStore()
    store.create(THREAD_A)
    inner = Agent(
        model=_ScriptedModel([FinalAnswer(content="ok")]),
        registry=ToolRegistry(),
        store=store,
        context_assembler=_assembler(_policy()),
    )
    ia = IntegratedAgent(agent=inner, config=RuntimeConfig())
    ia.run(THREAD_A, "hi")
    snap = ia.get_last_usage(THREAD_A)
    assert snap is not None
    assert snap.thread_id == THREAD_A
    assert snap.budget == _policy().hard_input_limit
    # 九分区之和 == total
    assert sum(snap.breakdown.values()) == snap.total


def test_unknown_thread_returns_none() -> None:
    ia, _ = _make_integrated(_assembler(_policy()))
    assert ia.get_last_usage("never-existed") is None


# ---------------------------------------------------------------------------
# 线程隔离
# ---------------------------------------------------------------------------


def test_thread_isolation_no_data_leak() -> None:
    """Thread A 跑过 run 后，Thread B 不能读到 A 的快照。"""
    store = InMemoryThreadStore()
    store.create(THREAD_A)
    store.create(THREAD_B)
    inner = Agent(
        model=_ScriptedModel([FinalAnswer(content="ok")]),
        registry=ToolRegistry(),
        store=store,
        context_assembler=_assembler(_policy()),
    )
    ia = IntegratedAgent(agent=inner, config=RuntimeConfig())
    ia.run(THREAD_A, "first")
    assert ia.get_last_usage(THREAD_B) is None
    assert ia.get_last_usage(THREAD_A) is not None


def test_thread_isolation_after_both_runs() -> None:
    """两个 Thread 都跑过 run 后，各自的快照独立。"""
    store = InMemoryThreadStore()
    store.create(THREAD_A)
    store.create(THREAD_B)
    inner = Agent(
        model=_ScriptedModel([FinalAnswer(content="ok")]),
        registry=ToolRegistry(),
        store=store,
        context_assembler=_assembler(_policy()),
    )
    ia = IntegratedAgent(agent=inner, config=RuntimeConfig())
    ia.run(THREAD_A, "alpha")
    ia.run(THREAD_B, "beta")
    snap_a = ia.get_last_usage(THREAD_A)
    snap_b = ia.get_last_usage(THREAD_B)
    assert snap_a is not None and snap_b is not None
    assert snap_a.thread_id == THREAD_A
    assert snap_b.thread_id == THREAD_B


# ---------------------------------------------------------------------------
# run 失败时仍然捕获（用于诊断）
# ---------------------------------------------------------------------------


def test_capture_after_failed_run() -> None:
    """run 抛错时，try/finally 也应保留已完成的 assemble 快照。"""
    store = InMemoryThreadStore()
    store.create(THREAD_A)
    # 模型发出 ToolCall 但没有对应工具 → Agent 在工具执行后抛错
    # 简化：直接让脚本模型返回 ToolCall，registry 是空的，会失败
    inner = Agent(
        model=_ScriptedModel([
            ToolCall(name="missing-tool", arguments={"x": 1}, id="c1"),
        ]),
        registry=ToolRegistry(),
        store=store,
        context_assembler=_assembler(_policy()),
    )
    ia = IntegratedAgent(agent=inner, config=RuntimeConfig())
    with pytest.raises(Exception):
        ia.run(THREAD_A, "hi")
    # 即便 run 失败，assemble 已经发生过——快查 get_last_usage 仍能拿到
    snap = ia.get_last_usage(THREAD_A)
    assert snap is not None


# ---------------------------------------------------------------------------
# 压缩前后
# ---------------------------------------------------------------------------


def test_no_compaction_when_below_threshold() -> None:
    """未触发压缩时，compacted=False / tokens_before=None。"""
    store = InMemoryThreadStore()
    store.create(THREAD_A)
    inner = Agent(
        model=_ScriptedModel([FinalAnswer(content="ok")]),
        registry=ToolRegistry(),
        store=store,
        context_assembler=_assembler(_policy()),
    )
    ia = IntegratedAgent(agent=inner, config=RuntimeConfig())
    ia.run(THREAD_A, "短消息")
    snap = ia.get_last_usage(THREAD_A)
    assert snap is not None
    assert snap.compacted is False
    assert snap.tokens_before is None


# ---------------------------------------------------------------------------
# 失败运行不覆盖上一轮快照
# ---------------------------------------------------------------------------


def test_failed_run_does_not_overwrite_previous_snapshot() -> None:
    """本轮若未完成 assemble（assemble() 抛错），上一轮的快照必须保留。

    实现要求（runtime.py 的 finally 块）：用对象身份 `is` 比较
    `_last_captured_snapshot` 与 `_agent.last_assembled`——
    Agent.run 只在 `assemble()` 成功返回后赋值；assemble 抛错时
    `_last_assembled` 引用不变，因此 `is` 比较为 True，跳过 capture。
    """
    store = InMemoryThreadStore()
    store.create(THREAD_A)
    counter = HeuristicTokenCounter()
    policy = ContextPolicy(
        max_context_tokens=120,
        reserved_output_tokens=0,
        safety_margin_tokens=0,
        compaction_threshold=0.8,
    )
    assembler = ContextAssembler(
        policy=policy,
        counter=counter,
        context_store=InMemoryContextStore(),
    )
    inner = Agent(
        model=_ScriptedModel([FinalAnswer(content="ok")]),
        registry=ToolRegistry(),
        store=store,
        context_assembler=assembler,
    )
    ia = IntegratedAgent(agent=inner, config=RuntimeConfig())

    # ---- Round 1：成功 → 捕获快照 ----
    ia.run(THREAD_A, "第一轮短消息")
    snap1 = ia.get_last_usage(THREAD_A)
    assert snap1 is not None
    last_assembled_ref = ia._agent.last_assembled  # 记录引用

    # ---- Round 2：让 assemble() 抛错（超硬上限），verify 上轮快照未变 ----
    # 极长输入：组装量必超硬上限 → assemble() 直接抛 ContextWindowExceededError
    # → Agent.run 的赋值语句不执行 → _last_assembled 仍是 round 1 的对象
    huge = "x" * 10_000  # ASCII：~3333 tokens，远超 120 上限
    from mini_deerflow.context_errors import ContextWindowExceededError

    with pytest.raises(ContextWindowExceededError):
        ia.run(THREAD_A, huge)

    # 上一轮的 AssembledContext 引用应保持不变（Agent 没被覆盖）
    assert ia._agent.last_assembled is last_assembled_ref
    # 上一轮的快照也应保留 —— **不**被本轮覆盖
    snap_after = ia.get_last_usage(THREAD_A)
    assert snap_after is not None
    assert snap_after is snap1, "本轮未完成 assemble，不应覆盖上一轮快照"