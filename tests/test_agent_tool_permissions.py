"""WM-3A：工具上下文与按会话权限。

覆盖四件事，全部走**真实 Agent.run 循环**（不用 prompt、不看前端传参）：

1. `needs_context` 工具在真实循环中拿得到正确的 thread_id（此前
   `registry.execute` 漏传 context，这条通道在主路径是断的）。
2. 晴通会话：`project_*` 工具既不出现在模型工具列表（暴露限制），
   模型强行返回调用时也在 dispatch 前被拒（执行限制）。
3. 项目会话：白名单是显式枚举的 frozenset（不是 None），
   `project_*` 工具可见且可执行。
4. 原有工具（calculator）与旧数据（无 project_id 键）行为无回归。

测试里注册的 `project_probe` 是纯记录型假工具：不启动 harness、
不触碰任何项目文件。
"""

import pytest

from mini_deerflow.agent import (
    PROJECT_TOOL_PREFIX,
    Agent,
    compute_thread_tool_allow_list,
)
from mini_deerflow.context_shared import context_attr
from mini_deerflow.models import FinalAnswer, Message, ToolCall
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tools import (
    CALCULATOR_TOOL,
    Tool,
    ToolNotAllowedError as ToolNotAllowedError_,
    ToolNotFoundError,
    create_default_registry,
)

# ---------------------------------------------------------------------------
# 测试工具：纯记录，不碰文件系统
# ---------------------------------------------------------------------------

_ctx_probe_calls: list[str | None] = []
_probe_calls: list[dict[str, object]] = []


def _ctx_probe_handler(arguments: dict[str, object], context: object) -> str:
    """needs_context 工具：记录执行时真实拿到的 thread_id。"""
    _ctx_probe_calls.append(context_attr(context, "thread_id"))
    return "ctx-ok"


def _probe_handler(arguments: dict[str, object]) -> str:
    """假的 project 工具：只记录被调用过，不做任何实际操作。"""
    _probe_calls.append(dict(arguments))
    return "probe-ok"


CTX_PROBE_TOOL = Tool(
    name="ctx_probe",
    description="记录执行上下文中的 thread_id（测试用）。",
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
    handler=_ctx_probe_handler,
    needs_context=True,
)

PROJECT_PROBE_TOOL = Tool(
    name=f"{PROJECT_TOOL_PREFIX}probe",
    description="假的项目工具：只记录调用，不做任何实际操作（测试用）。",
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
    handler=_probe_handler,
)


class RecordingModel:
    """按脚本返回决策，并记录每轮收到的工具名列表。"""

    def __init__(self, decisions: list[FinalAnswer | ToolCall]) -> None:
        self._decisions = list(decisions)
        self.seen_tool_names: list[set[str]] = []

    def decide(
        self, messages: list[Message], tools=None
    ) -> FinalAnswer | ToolCall:
        self.seen_tool_names.append(
            {spec.name for spec in (tools or [])}
        )
        return self._decisions.pop(0)


def _make_agent(
    decisions: list[FinalAnswer | ToolCall],
    *,
    thread_id: str = "t-normal",
    project_id: str | None = None,
) -> tuple[Agent, RecordingModel, InMemoryThreadStore]:
    """构造带假项目工具与上下文探针的真实 Agent。"""
    registry = create_default_registry()
    registry.register_many([CTX_PROBE_TOOL, PROJECT_PROBE_TOOL])
    store = InMemoryThreadStore()
    store.create(thread_id, project_id)
    model = RecordingModel(decisions)
    return Agent(model=model, registry=registry, store=store), model, store


@pytest.fixture(autouse=True)
def _reset_probe_calls():
    _ctx_probe_calls.clear()
    _probe_calls.clear()
    yield


# ---------------------------------------------------------------------------
# 1. needs_context 工具在真实循环拿到正确 thread_id
# ---------------------------------------------------------------------------


def test_真实循环_needs_context_工具拿到_thread_id():
    agent, _, _ = _make_agent(
        [
            ToolCall(name="ctx_probe", arguments={}),
            FinalAnswer(content="done"),
        ],
        thread_id="t-ctx-1",
    )
    answer, trace = agent.run("t-ctx-1", "hi")
    assert answer == "done"
    assert _ctx_probe_calls == ["t-ctx-1"]


def test_真实循环_项目会话_needs_context_工具同样拿到_thread_id():
    agent, _, _ = _make_agent(
        [
            ToolCall(name="ctx_probe", arguments={}),
            FinalAnswer(content="done"),
        ],
        thread_id="t-ctx-2",
        project_id="p1",
    )
    agent.run("t-ctx-2", "hi")
    assert _ctx_probe_calls == ["t-ctx-2"]


# ---------------------------------------------------------------------------
# 2. 普通会话：暴露限制 + 执行限制
# ---------------------------------------------------------------------------


def test_普通会话_模型工具列表不含_project_工具():
    agent, model, _ = _make_agent(
        [FinalAnswer(content="ok")],
        thread_id="t-norm-1",
    )
    agent.run("t-norm-1", "hi")
    assert len(model.seen_tool_names) == 1
    names = model.seen_tool_names[0]
    assert f"{PROJECT_TOOL_PREFIX}probe" not in names
    assert "calculator" in names


def test_普通会话_模型强行调用_project_工具被拒绝():
    agent, _, _ = _make_agent(
        [
            ToolCall(name=f"{PROJECT_TOOL_PREFIX}probe", arguments={}),
            FinalAnswer(content="unreachable"),
        ],
        thread_id="t-norm-2",
    )
    with pytest.raises(ToolNotAllowedError_):
        agent.run("t-norm-2", "hi")
    # 执行限制生效：假工具的 handler 一次都没跑
    assert _probe_calls == []


def test_普通会话_允许清单是_frozenset_且不含_project_工具():
    registry = create_default_registry()
    registry.register(PROJECT_PROBE_TOOL)
    allowed = compute_thread_tool_allow_list(registry, None)
    assert isinstance(allowed, frozenset)
    assert f"{PROJECT_TOOL_PREFIX}probe" not in allowed
    assert "calculator" in allowed


def test_旧数据无_project_id_键按普通会话处理():
    """SCHEMA_VERSION 不变的兼容性：旧线程没有该键，必须落回普通会话权限。"""
    from mini_deerflow.thread_state import SCHEMA_VERSION, ThreadState

    # 模拟 WM-2 之前的旧数据：有 schema_version，没有 project_id 键
    thread = ThreadState.from_dict(
        {
            "schema_version": SCHEMA_VERSION,
            "thread_id": "t-old",
            "messages": [],
        }
    )
    assert thread.project_id is None
    registry = create_default_registry()
    registry.register(PROJECT_PROBE_TOOL)
    assert f"{PROJECT_TOOL_PREFIX}probe" not in compute_thread_tool_allow_list(
        registry, thread.project_id
    )


# ---------------------------------------------------------------------------
# 3. 项目会话：允许全部工具（由工具内部边界检查负责隔离）
# ---------------------------------------------------------------------------


def test_项目会话_返回_None_表示不限制():
    """项目会话：None = 不限制（工具内部有 workspace 边界检查）。"""
    registry = create_default_registry()
    registry.register(PROJECT_PROBE_TOOL)
    allowed = compute_thread_tool_allow_list(registry, "p1")
    # None = 不限制，与 filter_tool_specs / ensure_tool_allowed 的语义一致
    assert allowed is None


def test_项目会话_模型工具列表包含_project_工具且可执行():
    agent, model, _ = _make_agent(
        [
            ToolCall(name=f"{PROJECT_TOOL_PREFIX}probe", arguments={"a": 1}),
            FinalAnswer(content="done"),
        ],
        thread_id="t-proj-1",
        project_id="p1",
    )
    answer, _ = agent.run("t-proj-1", "hi")
    assert answer == "done"
    assert f"{PROJECT_TOOL_PREFIX}probe" in model.seen_tool_names[0]
    assert _probe_calls == [{"a": 1}]


# ---------------------------------------------------------------------------
# 4. 回归：原有工具与会话行为不变
# ---------------------------------------------------------------------------


def test_普通会话_calculator_照常工作():
    agent, _, _ = _make_agent(
        [
            ToolCall(name="calculator", arguments={"expression": "1 + 1"}),
            FinalAnswer(content="答案是 2"),
        ],
        thread_id="t-norm-3",
    )
    answer, trace = agent.run("t-norm-3", "算一下")
    assert answer == "答案是 2"
    tool_steps = [t for t in trace if t.get("action") == "tool_call"]
    assert tool_steps and tool_steps[0]["detail"]["result"] == "2"


def test_项目会话_calculator_照常工作():
    agent, _, _ = _make_agent(
        [
            ToolCall(name="calculator", arguments={"expression": "2 * 3"}),
            FinalAnswer(content="答案是 6"),
        ],
        thread_id="t-proj-2",
        project_id="p1",
    )
    answer, _ = agent.run("t-proj-2", "算一下")
    assert answer == "答案是 6"


def test_未知工具名仍报_ToolNotFound_而非_NotAllowed():
    """区分"叫错名字"与"越权调用"：未注册的名字不进白名单判定。"""
    agent, _, _ = _make_agent(
        [
            ToolCall(name="hack", arguments={}),
            FinalAnswer(content="unreachable"),
        ],
        thread_id="t-norm-4",
    )
    with pytest.raises(ToolNotFoundError):
        agent.run("t-norm-4", "hi")


def test_普通会话_needs_context_基础工具不受影响():
    """ctx_probe 不带 project_ 前缀，普通会话照常可见可执行。"""
    agent, model, _ = _make_agent(
        [
            ToolCall(name="ctx_probe", arguments={}),
            FinalAnswer(content="ok"),
        ],
        thread_id="t-norm-5",
    )
    agent.run("t-norm-5", "hi")
    assert "ctx_probe" in model.seen_tool_names[0]
    assert _ctx_probe_calls == ["t-norm-5"]
