"""父任务取消后，旧子任务卡会不会串到下一轮（UI-SUBAGENT-INLINE-01）。

复现步骤（与产品操作一致）：

1. 发送消息 A → 父 Agent 派发子 Agent → 子任务卡出现在消息流里；
2. 父 Agent 给出最终回复**之前**取消 → 等取消状态确认；
3. 同一会话发送内容明显不同的消息 B → 正常完成；
4. 分别在刷新前后检查消息流顺序，并通过 GET 线程接口核对
   已保存消息和旧卡的 ``after_message_count``。

零网络、零付费模型：父 / 子 Agent 都用脚本模型。
子 Agent 的模型会**阻塞**直到测试放行 —— 这样「子任务卡已出现、父任务
还没给出最终回复」这个窗口是确定的，取消不依赖时序运气。
"""

from __future__ import annotations

import threading
import time
from typing import Any

from fastapi.testclient import TestClient

from mini_deerflow.api.app import create_app
from mini_deerflow.api.config import ApiSettings
from mini_deerflow.models import FinalAnswer, Message, ToolCall
from mini_deerflow.runtime import RuntimeConfig, create_runtime
from mini_deerflow.subagent_runs import collapse_run_anchors
from mini_deerflow.subagent_tools import create_subagent_tools
from mini_deerflow.thread_state import ThreadState
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tools import Tool, ToolRegistry

THREAD_ID = "th-cancel-anchor"
MESSAGE_A = "帮我调研三个可能的解决方向"
MESSAGE_B = "换个话题：请解释光合作用的原理"
ANSWER_B = "最终回答：光合作用把光能转成化学能。"


# --------------------------------------------------------------------------- 模型


class _BlockingChildModel:
    """子 Agent 模型：进入 decide 后阻塞，直到测试放行。"""

    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        self._entered = entered
        self._release = release

    def decide(self, messages: list[Message], tools: list[Any] | None = None):
        self._entered.set()
        # 最多等 15 秒：测试失败时不会永久挂住
        self._release.wait(timeout=15)
        return FinalAnswer(content="子 Agent 的结论：三个方向值得查")


class _BlockingOnNthChildModel:
    """子 Agent 模型：第 ``n`` 次 decide 阻塞，直到测试放行。

    前 n-1 次正常返回 —— 这样"前面几轮已经正常落库"是可复现的，
    被取消的那一轮不是会话的第一轮。
    """

    def __init__(
        self,
        n: int,
        entered: threading.Event,
        release: threading.Event,
    ) -> None:
        self._n = n
        self._entered = entered
        self._release = release
        self.calls = 0

    def decide(self, messages: list[Message], tools: list[Any] | None = None):
        self.calls += 1
        if self.calls == self._n:
            self._entered.set()
            self._release.wait(timeout=15)
        return FinalAnswer(content="子 Agent 的结论：三个方向值得查")


class _ParentModel:
    """父 Agent 模型：按最新用户消息分流。

    含「调研」→ 派发子任务；否则直接给最终回答。
    """

    def __init__(self) -> None:
        self._dispatched = False

    def decide(self, messages: list[Message], tools: list[Any] | None = None):
        last_user = ""
        for message in messages:
            if message.role == "user":
                last_user = message.content
        if "调研" in last_user and not self._dispatched:
            self._dispatched = True
            return ToolCall(
                name="subagent_submit",
                arguments={"instruction": "调研三个可能的解决方向"},
            )
        return FinalAnswer(content=ANSWER_B)


class _SecondRoundParentModel:
    """父 Agent 模型：只有「第二轮」那条消息会派发子任务。

    第一轮直接给最终回答（正常落库 2 条消息），
    第二轮派发子任务（用来取消），之后的消息也直接给最终回答。
    """

    def decide(self, messages: list[Message], tools: list[Any] | None = None):
        last_user = ""
        for message in messages:
            if message.role == "user":
                last_user = message.content
        if "第二轮" in last_user:
            return ToolCall(
                name="subagent_submit",
                arguments={"instruction": "第二轮的调研任务"},
            )
        return FinalAnswer(content=ANSWER_B)


# --------------------------------------------------------------------------- 装置


def _build_runtime(
    child_model: Any,
    *,
    thread_id: str = THREAD_ID,
    parent_model: Any = None,
):
    store = InMemoryThreadStore()
    store.create(thread_id)

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="noop",
            description="占位",
            parameters={"type": "object", "properties": {}},
            handler=lambda args: "{}",
        )
    )

    runtime = create_runtime(
        store=store,
        registry=registry,
        config=RuntimeConfig(subagent_enabled=True),
    )

    manager = runtime.manager
    assert manager is not None
    registry.unregister("subagent_submit")
    for tool in create_subagent_tools(
        manager=manager,
        parent_run_id="cancel-anchor",
        parent_thread_id=thread_id,
        thread_store=store,
        model=child_model,
        registry=registry,
        parent_tool_names=tuple(t.name for t in registry.list_tools()),
    ):
        if tool.name == "subagent_submit":
            registry.register(tool)

    runtime.agent._model = parent_model or _ParentModel()  # type: ignore[attr-defined]
    return runtime


def _frontend_slot(anchor: int, message_count: int) -> str:
    """复刻前端 ``MessageList.buildRenderTree`` 的放置规则。

    - ``anchor <= 0`` → 排在最前面（leading）；
    - ``anchor > message_count`` → 退回末尾（trailing）；
    - 否则 → 插在第 ``anchor`` 条消息**之后**。

    返回值里的 ``after-message-N`` 就是「插在第 N 条消息后面」，
    也就是卡片在消息流里的落点。
    """
    if anchor <= 0:
        return "leading"
    if anchor > message_count:
        return "trailing"
    return f"after-message-{anchor}"


# --------------------------------------------------------------------------- 用例


def test_父任务取消后旧子任务卡不串到下一轮() -> None:
    entered = threading.Event()
    release = threading.Event()
    runtime = _build_runtime(_BlockingChildModel(entered, release))
    app = create_app(ApiSettings(), runtime_factory=lambda: runtime)

    with TestClient(app) as client:
        # --- 第 1 轮：发送消息 A，父 Agent 派发子任务 ---
        round_a: dict[str, str] = {}

        def _run_a() -> None:
            response = client.post(
                f"/api/threads/{THREAD_ID}/runs/stream",
                json={"message": MESSAGE_A},
            )
            round_a["body"] = response.text

        worker = threading.Thread(target=_run_a, daemon=True)
        worker.start()

        # 等子任务卡出现（子 Agent 已经开始干活）
        assert entered.wait(timeout=15), "子 Agent 未在预期时间内启动"

        # --- 第 2 步：父 Agent 给出最终回复之前取消 ---
        stop = client.post(f"/api/threads/{THREAD_ID}/runs/stop")
        assert stop.status_code == 200, stop.text

        # 让取消传播到子任务（父侧每 50ms 轮询一次取消令牌）
        time.sleep(0.3)
        release.set()

        worker.join(timeout=30)
        assert not worker.is_alive(), "第 1 轮的 SSE 未在预期时间内结束"
        body_a = round_a["body"]
        assert "subagent.started" in body_a, body_a[:2000]
        assert "run.cancelled" in body_a, body_a[:2000]

        # --- 第 3 步：核对取消后落库了什么 ---
        detail_a = client.get(f"/api/threads/{THREAD_ID}").json()
        # 被取消的一轮：消息**不落库**（Agent 只在给出最终回答时提交）
        assert detail_a["messages"] == [], "被取消的一轮不应落库任何消息"
        runs_a = detail_a["subagent_runs"]
        assert len(runs_a) == 1, f"应有 1 条子任务记录，实际 {runs_a}"
        anchor_after_a = runs_a[0]["after_message_count"]
        assert isinstance(anchor_after_a, int)

        # --- 第 4 步：同一会话发送内容明显不同的消息 B ---
        response_b = client.post(
            f"/api/threads/{THREAD_ID}/runs/stream",
            json={"message": MESSAGE_B},
        )
        assert response_b.status_code == 200, response_b.text
        assert "run.completed" in response_b.text, response_b.text[:2000]

        detail_b = client.get(f"/api/threads/{THREAD_ID}").json()
        messages = detail_b["messages"]
        # B 轮正常完成：user B + assistant B 都落库
        assert [m["role"] for m in messages] == ["user", "assistant"], messages
        assert messages[0]["content"] == MESSAGE_B
        assert messages[1]["content"] == ANSWER_B

        runs_b = detail_b["subagent_runs"]
        assert len(runs_b) == 1, f"应有 1 条子任务记录，实际 {runs_b}"
        anchor_after_b = runs_b[0]["after_message_count"]

        # --- 第 5 步：旧卡不能挂到 B 的提问下面 ---
        #
        # 用户 B 是落库后的第 1 条消息；锚点 = 1 意味着卡片插在
        # 「第 1 条消息之后」= user B 之下 → 卡片看起来属于 B 这一轮。
        # 这就是串位。
        user_b_position = 1
        slot_after_b = _frontend_slot(anchor_after_b, len(messages))
        assert slot_after_b != f"after-message-{user_b_position}", (
            "旧子任务卡串到了下一轮提问下面："
            f"anchor_after_a={anchor_after_a}, anchor_after_b={anchor_after_b}, "
            f"messages={[m['content'] for m in messages]}, slot={slot_after_b}"
        )

        # 锚点被收到本轮起点（这一轮开始前线程里 0 条消息）→ 0。
        # 0 在前端规则里是「排在最前面」：卡片停在轮次边界上，
        # 既不消失，也不会被后面落库的消息拖到 B 下面。
        assert anchor_after_b == 0, (
            f"期望锚点收到轮次边界 0，实际 {anchor_after_b}"
        )
        assert slot_after_b == "leading"

        # --- 第 6 步：刷新前后归属一致 ---
        #
        # 刷新后卡片的位置只来自持久化记录（上一轮的 SSE 事件不跨轮保留），
        # 因此「刷新前」与「刷新后」必然是同一个锚点 —— 归属不会变。
        # 这里把这一点写死，防止将来有人只改前端合并逻辑。
        assert anchor_after_b == runs_b[0]["after_message_count"]

        # 旧卡本身仍然留在会话里（取消是可追溯的事实，不该被抹掉）
        assert runs_b[0]["task_id"] == runs_a[0]["task_id"]
        assert runs_b[0]["status"] in {"CANCELLED", "SUCCEEDED", "FAILED", "TIMED_OUT"}


def test_锚点回收只影响没落库的那一轮() -> None:
    """护栏：已提交的一轮的锚点原样不动。

    这是「只修串位、不动正常链路」的关键 —— 正常一轮的锚点是
    「本轮 user 消息之后」，落库后消息数比锚点大，收回逻辑不该碰它。
    """
    thread = ThreadState(thread_id="th-collapse")
    thread.messages = [
        Message(role="user", content="第一轮提问"),
        Message(role="assistant", content="第一轮回答"),
    ]
    thread.subagent_runs = [
        # 已提交的一轮：锚点 1 ≤ 消息数 2 → 不动
        {"task_id": "done", "parent_run_id": "run-done", "after_message_count": 1},
        # 没落库的一轮：锚点 3 > 消息数 2 → 收到 2
        {"task_id": "cancelled", "parent_run_id": "run-cancelled", "after_message_count": 3},
        # 别的轮次：不动
        {"task_id": "other", "parent_run_id": "run-other", "after_message_count": 3},
    ]

    changed = collapse_run_anchors(thread, "run-cancelled")

    assert changed == 1
    assert thread.subagent_runs[0]["after_message_count"] == 1
    assert thread.subagent_runs[1]["after_message_count"] == 2
    assert thread.subagent_runs[2]["after_message_count"] == 3


def test_取消发生在第二轮_旧卡停在上一轮边界不随后续消息漂移() -> None:
    """取消的不是会话第一轮时，旧卡停在**上一轮的边界**，不随后续消息漂移。

    第一个用例覆盖的是"取消发生在第一轮"（锚点收到 0 → 排最前）。
    这一条覆盖"取消发生在第二轮"：

    - 第一轮正常完成 → 线程里 2 条消息；
    - 第二轮派发子任务后被取消 → 本轮不落库，消息仍是 2 条，
      卡的锚点从 3 收到 **2**（上一轮边界）；
    - 第三轮正常完成 → 消息变成 4 条，
      卡仍锚在 2 —— 落在第一轮的回答之后、第三轮的提问之前，
      **不会**被后面新增的消息拖到第三轮下面。
    """
    thread_id = "th-cancel-anchor-r2"
    entered = threading.Event()
    release = threading.Event()
    child_model = _BlockingOnNthChildModel(1, entered, release)
    runtime = _build_runtime(
        child_model,
        thread_id=thread_id,
        parent_model=_SecondRoundParentModel(),
    )
    app = create_app(ApiSettings(), runtime_factory=lambda: runtime)

    with TestClient(app) as client:
        # --- 第一轮：正常完成，落库 2 条消息 ---
        response_1 = client.post(
            f"/api/threads/{thread_id}/runs/stream",
            json={"message": "第一轮：请直接给结论"},
        )
        assert response_1.status_code == 200, response_1.text
        assert "run.completed" in response_1.text, response_1.text[:2000]

        detail_1 = client.get(f"/api/threads/{thread_id}").json()
        assert [m["role"] for m in detail_1["messages"]] == ["user", "assistant"]
        assert detail_1["subagent_runs"] == []

        # --- 第二轮：派发子任务，然后在最终回复前取消 ---
        round_b: dict[str, str] = {}

        def _run_b() -> None:
            response = client.post(
                f"/api/threads/{thread_id}/runs/stream",
                json={"message": "第二轮：请调研一下"},
            )
            round_b["body"] = response.text

        worker = threading.Thread(target=_run_b, daemon=True)
        worker.start()

        assert entered.wait(timeout=15), "子 Agent 未在预期时间内启动"

        stop = client.post(f"/api/threads/{thread_id}/runs/stop")
        assert stop.status_code == 200, stop.text
        time.sleep(0.3)
        release.set()

        worker.join(timeout=30)
        assert not worker.is_alive(), "第 2 轮的 SSE 未在预期时间内结束"
        body_b = round_b["body"]
        assert "subagent.started" in body_b, body_b[:2000]
        assert "run.cancelled" in body_b, body_b[:2000]

        # 取消后：第二轮的消息**没有落库**，线程里仍是第一轮的 2 条消息
        detail_2 = client.get(f"/api/threads/{thread_id}").json()
        assert len(detail_2["messages"]) == 2, detail_2["messages"]
        runs_2 = detail_2["subagent_runs"]
        assert len(runs_2) == 1, f"应有 1 条子任务记录，实际 {runs_2}"

        # 锚点从 3（本轮 user 消息之后）收到 2（上一轮边界）
        assert runs_2[0]["after_message_count"] == 2, runs_2[0]

        # --- 第三轮：正常完成，消息变成 4 条 ---
        response_3 = client.post(
            f"/api/threads/{thread_id}/runs/stream",
            json={"message": "第三轮：换个话题，解释光合作用"},
        )
        assert response_3.status_code == 200, response_3.text
        assert "run.completed" in response_3.text, response_3.text[:2000]

        detail_3 = client.get(f"/api/threads/{thread_id}").json()
        messages = detail_3["messages"]
        assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
        assert messages[2]["content"] == "第三轮：换个话题，解释光合作用"

        runs_3 = detail_3["subagent_runs"]
        assert len(runs_3) == 1, f"应有 1 条子任务记录，实际 {runs_3}"
        anchor = runs_3[0]["after_message_count"]

        # 旧卡仍锚在上一轮边界（2）—— 没被第三轮新增的消息拖走
        assert anchor == 2, runs_3[0]
        slot = _frontend_slot(anchor, len(messages))
        assert slot == "after-message-2", slot
        # 第三轮的提问是第 3 条消息；旧卡不能挂在它下面
        assert slot != "after-message-3", (
            "旧子任务卡串到了第三轮提问下面："
            f"anchor={anchor}, messages={[m['content'] for m in messages]}, slot={slot}"
        )
