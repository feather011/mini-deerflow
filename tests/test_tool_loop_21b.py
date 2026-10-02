"""Phase 21B 测试：真实模型 ToolCall 闭环。

全部使用假客户端与假模型，**不访问真实模型、不消耗额度**。

覆盖：
- ToolDefinition（ToolSpec）Schema 转换
- tool_use / tool_result 往返与 call_id 保留
- 非法参数、未知工具、工具异常
- 多 tool_use 拒绝执行
- tool → result → final 两轮闭环
- Budget / max_steps
- 失败不提交 Thread
- FakeModel 回归
"""

from __future__ import annotations

from typing import Any

import pytest

from mini_deerflow.agent import Agent
from mini_deerflow.anthropic_compat_model import (
    AnthropicCompatModel,
    anthropic_tools_from,
    provider_messages_from,
)
from mini_deerflow.middleware import Budget
from mini_deerflow.model_config import ModelSettings
from mini_deerflow.model_errors import ModelProtocolError
from mini_deerflow.models import (
    FinalAnswer,
    FakeModel,
    Message,
    ToolCall,
    ToolSpec,
)
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tools import (
    ToolExecutionError,
    ToolNotFoundError,
    ToolRegistry,
    create_default_registry,
)

KEY = "sk-UNIT-TEST-KEY"


# ---------------------------------------------------------------------------
# 假供应商响应
# ---------------------------------------------------------------------------


class Block:
    """模拟 anthropic SDK 的内容块（SDK 侧是 pydantic 对象，测试只需同名属性）。"""

    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


def text_response(text: str) -> Block:
    return Block(content=[Block(type="text", text=text)])


def tool_use_response(name: str, arguments: dict[str, Any], call_id: str) -> Block:
    return Block(
        content=[
            Block(type="text", text="我来计算一下"),
            Block(type="tool_use", id=call_id, name=name, input=arguments),
        ]
    )


class FakeMessages:
    """按脚本依次返回响应的假 client.messages。"""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, script: list[Any]) -> None:
        self.messages = FakeMessages(script)


def make_model(script: list[Any]) -> tuple[AnthropicCompatModel, FakeMessages]:
    client = FakeClient(script)
    model = AnthropicCompatModel(
        ModelSettings(provider="minimax", api_key=KEY), client=client
    )
    return model, client.messages


CALCULATOR_SPEC = ToolSpec(
    name="calculator",
    description="计算四则运算",
    input_schema={
        "type": "object",
        "properties": {"expression": {"type": "string"}},
        "required": ["expression"],
        "additionalProperties": False,
    },
)

USER = Message(role="user", content="计算 (17 + 25) * 3")


# ---------------------------------------------------------------------------
# 1. 工具 Schema 转换
# ---------------------------------------------------------------------------


class TestToolSchema转换:
    def test_注册表产出_provider_neutral描述(self) -> None:
        specs = create_default_registry().tool_specs()
        assert [s.name for s in specs] == ["calculator"]
        spec = specs[0]
        assert spec.input_schema["type"] == "object"
        assert spec.input_schema["required"] == ["expression"]
        # ToolSpec 不含 handler，无法从中执行任何东西
        assert not hasattr(spec, "handler")

    def test_tool_spec_to_dict_字段齐全(self) -> None:
        data = CALCULATOR_SPEC.to_dict()
        assert set(data) == {"name", "description", "input_schema"}
        assert data["name"] == "calculator"

    def test_anthropic_tools_from_生成供应商字段(self) -> None:
        tools = anthropic_tools_from([CALCULATOR_SPEC])
        assert tools == [
            {
                "name": "calculator",
                "description": "计算四则运算",
                "input_schema": CALCULATOR_SPEC.input_schema,
            }
        ]

    def test_无工具时不带_tools_字段(self) -> None:
        assert anthropic_tools_from(None) is None
        assert anthropic_tools_from([]) is None

    def test_schema_不是对象_抛协议错误(self) -> None:
        bad = ToolSpec(name="x", description="d", input_schema={"type": "array"})
        with pytest.raises(ModelProtocolError):
            anthropic_tools_from([bad])

    def test_请求确实带上_tools(self) -> None:
        model, messages = make_model([text_response("ok")])
        model.decide([USER], [CALCULATOR_SPEC])
        assert messages.calls[0]["tools"][0]["name"] == "calculator"

    def test_不传工具时请求不带_tools(self) -> None:
        model, messages = make_model([text_response("ok")])
        model.decide([USER])
        assert "tools" not in messages.calls[0]


# ---------------------------------------------------------------------------
# 2. tool_use / tool_result 往返
# ---------------------------------------------------------------------------


class Test工具消息往返:
    def test_成对消息还原为_tool_use_与_tool_result(self) -> None:
        messages = [
            USER,
            Message(
                role="assistant",
                content="调用工具 calculator",
                tool_call_id="toolu_1",
                tool_name="calculator",
                tool_arguments={"expression": "(17 + 25) * 3"},
            ),
            Message(role="tool", content="126", tool_call_id="toolu_1"),
        ]
        payload = provider_messages_from(messages)

        assert [m["role"] for m in payload] == ["user", "assistant", "user"]

        tool_use = payload[1]["content"][0]
        assert tool_use == {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "calculator",
            "input": {"expression": "(17 + 25) * 3"},
        }

        tool_result = payload[2]["content"][0]
        assert tool_result == {
            "type": "tool_result",
            "tool_use_id": "toolu_1",
            "content": "126",
        }
        # call_id 必须一一对应，否则供应商拒收该轮
        assert tool_use["id"] == tool_result["tool_use_id"]

    def test_孤立_tool_退回普通用户文本(self) -> None:
        """历史里只落库了 tool 结果、没有 assistant tool_use 时不得发孤儿 tool_result。"""
        messages = [
            Message(role="user", content="计算 1+1"),
            Message(role="tool", content="2", tool_call_id="toolu_orphan"),
            Message(role="assistant", content="计算结果: 2"),
        ]
        payload = provider_messages_from(messages)
        assert [m["role"] for m in payload] == ["user", "user", "assistant"]
        for item in payload:
            assert isinstance(item["content"], str)

    def test_call_id_不匹配则不成对(self) -> None:
        messages = [
            Message(
                role="assistant",
                content="x",
                tool_call_id="toolu_a",
                tool_name="calculator",
                tool_arguments={"expression": "1"},
            ),
            Message(role="tool", content="2", tool_call_id="toolu_other"),
        ]
        payload = provider_messages_from(messages)
        # 配对失败 → tool 结果退回文本，避免发出指向错误 id 的 tool_result
        assert payload[1]["content"] == "2"


# ---------------------------------------------------------------------------
# 3. 解析 tool_use 响应
# ---------------------------------------------------------------------------


class Test解析ToolCall:
    def test_单个_tool_use_转_ToolCall_并保留_call_id(self) -> None:
        model, _ = make_model([
            tool_use_response("calculator", {"expression": "1+1"}, "toolu_abc")
        ])
        decision = model.decide([USER], [CALCULATOR_SPEC])

        assert isinstance(decision, ToolCall)
        assert decision.name == "calculator"
        assert decision.arguments == {"expression": "1+1"}
        assert decision.id == "toolu_abc"

    def test_多个_tool_use_拒绝执行且不静默丢弃(self) -> None:
        model, _ = make_model([
            Block(content=[
                Block(type="tool_use", id="toolu_1", name="calculator",
                      input={"expression": "1"}),
                Block(type="tool_use", id="toolu_2", name="calculator",
                      input={"expression": "2"}),
            ])
        ])
        with pytest.raises(ModelProtocolError) as exc:
            model.decide([USER], [CALCULATOR_SPEC])
        assert "2" in str(exc.value)
        assert "单工具" in str(exc.value)

    def test_tool_use_缺少_call_id(self) -> None:
        model, _ = make_model([
            Block(content=[Block(type="tool_use", id="", name="calculator",
                                 input={"expression": "1"})])
        ])
        with pytest.raises(ModelProtocolError) as exc:
            model.decide([USER], [CALCULATOR_SPEC])
        assert "id" in str(exc.value)

    def test_tool_use_缺少_name(self) -> None:
        model, _ = make_model([
            Block(content=[Block(type="tool_use", id="toolu_1", name="",
                                 input={"expression": "1"})])
        ])
        with pytest.raises(ModelProtocolError):
            model.decide([USER], [CALCULATOR_SPEC])

    def test_参数不是_json_object(self) -> None:
        model, _ = make_model([
            Block(content=[Block(type="tool_use", id="toolu_1",
                                 name="calculator", input="not-an-object")])
        ])
        with pytest.raises(ModelProtocolError) as exc:
            model.decide([USER], [CALCULATOR_SPEC])
        assert "JSON object" in str(exc.value)

    def test_缺少必填参数(self) -> None:
        model, _ = make_model([
            Block(content=[Block(type="tool_use", id="toolu_1",
                                 name="calculator", input={})])
        ])
        with pytest.raises(ModelProtocolError) as exc:
            model.decide([USER], [CALCULATOR_SPEC])
        assert "必填参数" in str(exc.value)
        assert "expression" in str(exc.value)

    def test_文本响应仍转_FinalAnswer(self) -> None:
        model, _ = make_model([text_response("直接回答")])
        decision = model.decide([USER])
        assert isinstance(decision, FinalAnswer)
        assert decision.content == "直接回答"

    def test_只带工具时文本被忽略_优先工具(self) -> None:
        model, _ = make_model([
            tool_use_response("calculator", {"expression": "1"}, "toolu_1")
        ])
        assert isinstance(model.decide([USER], [CALCULATOR_SPEC]), ToolCall)


# ---------------------------------------------------------------------------
# 4. Agent 闭环
# ---------------------------------------------------------------------------


class _ScriptedModel:
    """按脚本依次返回决策的 Model 替身（走真实 Agent 循环）。"""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls: list[list[Message]] = []
        self.tools_seen: list[Any] = []

    def decide(self, messages: list[Message], tools=None) -> FinalAnswer | ToolCall:
        self.calls.append(list(messages))
        self.tools_seen.append(tools)
        return self._script.pop(0)


class TestAgent闭环:
    def test_tool_result_final_两轮闭环(self) -> None:
        store = InMemoryThreadStore()
        store.create("t1")
        model = _ScriptedModel([
            ToolCall(name="calculator",
                     arguments={"expression": "(17 + 25) * 3"},
                     id="toolu_1"),
            FinalAnswer(content="结果是 126"),
        ])
        agent = Agent(model=model, registry=create_default_registry(), store=store)

        answer, trace = agent.run("t1", "请必须使用计算器计算 (17 + 25) * 3")

        assert answer == "结果是 126"
        # 两轮 decide
        assert len(model.calls) == 2
        # 每轮都收到工具描述
        assert all(
            specs and specs[0].name == "calculator" for specs in model.tools_seen
        )

        # 第二轮能看到完整的 tool_use / tool_result 配对
        second = model.calls[1]
        roles = [m.role for m in second]
        assert roles[-2:] == ["assistant", "tool"]
        assert second[-2].tool_call_id == "toolu_1"
        assert second[-1].tool_call_id == "toolu_1"
        assert second[-1].content == "126"

        # trace 两步 + 工具执行记录
        assert [e["action"] for e in trace] == ["model_decision", "tool_call",
                                               "final_answer"]

    def test_落库只含_user_与_final_assistant(self) -> None:
        """Thread 只提交 user + 最终 assistant。

        中间工具往返（assistant tool_use + tool 结果）**不进 ThreadState**：
        单独落一条不带 call_id 的 tool 消息，下一轮就是没有配对 tool_use 的
        孤儿 tool_result。工具往返只保留在 working_messages / Trace / SSE。
        """
        store = InMemoryThreadStore()
        store.create("t1")
        model = _ScriptedModel([
            ToolCall(name="calculator", arguments={"expression": "1+1"},
                     id="toolu_1"),
            FinalAnswer(content="2"),
        ])
        agent = Agent(model=model, registry=create_default_registry(), store=store)
        _, trace = agent.run("t1", "计算 1+1")

        persisted = [(m.role, m.content) for m in store.get("t1").snapshot()]
        assert persisted == [
            ("user", "计算 1+1"),
            ("assistant", "2"),
        ]
        # 工具结果没有丢：Trace 里仍然可查
        assert [t["action"] for t in trace] == ["model_decision", "tool_call",
                                                "final_answer"]
        assert trace[1]["detail"]["result"] == "2"

    def test_失败时_thread_不半提交(self) -> None:
        store = InMemoryThreadStore()
        store.create("t1")
        model = _ScriptedModel([
            ToolCall(name="no_such_tool", arguments={}, id="toolu_x"),
        ])
        agent = Agent(model=model, registry=create_default_registry(), store=store)

        with pytest.raises(ToolNotFoundError):
            agent.run("t1", "调用未知工具")

        # 工具在 commit 之前就抛出，Thread 必须保持原样
        assert store.get("t1").snapshot() == ()

    def test_工具执行异常导致整轮失败_不落库(self) -> None:
        def _boom(args: dict[str, object]) -> str:
            raise ValueError("工具内部炸了")

        registry = ToolRegistry()
        from mini_deerflow.tools import Tool

        registry.register(Tool(
            name="boom",
            description="总是失败",
            parameters={"type": "object", "properties": {}},
            handler=_boom,
        ))

        store = InMemoryThreadStore()
        store.create("t1")
        model = _ScriptedModel([
            ToolCall(name="boom", arguments={}, id="toolu_1"),
        ])
        agent = Agent(model=model, registry=registry, store=store)

        with pytest.raises(ToolExecutionError):
            agent.run("t1", "触发失败")

        assert store.get("t1").snapshot() == ()

    def test_未知工具抛稳定错误(self) -> None:
        registry = create_default_registry()
        with pytest.raises(ToolNotFoundError):
            registry.execute("ghost", {})


# ---------------------------------------------------------------------------
# 5. Budget / max_steps
# ---------------------------------------------------------------------------


class Test预算与步数:
    def test_超过_max_steps_抛错且不落库(self) -> None:
        store = InMemoryThreadStore()
        store.create("t1")
        # 每一轮都要求调用工具，永远走不到 FinalAnswer
        model = _ScriptedModel([
            ToolCall(name="calculator", arguments={"expression": "1"}, id=f"t{i}")
            for i in range(5)
        ])
        agent = Agent(model=model, registry=create_default_registry(),
                      store=store, max_steps=2)

        # 与 agent.py 的既有文案一致（该文案早于 21B，不因测试而改动）
        with pytest.raises(RuntimeError, match="Agent 在 2 步内未完成"):
            agent.run("t1", "计算 1")

        assert store.get("t1").snapshot() == ()

    def test_budget_耗尽时抛出(self) -> None:
        from mini_deerflow.middleware import BudgetExceededError

        store = InMemoryThreadStore()
        store.create("t1")
        budget = Budget(max_steps=10, max_model_calls=1, max_tool_calls=10,
                        max_subagent_tasks=2, max_subagent_depth=3)
        model = _ScriptedModel([
            ToolCall(name="calculator", arguments={"expression": "1"},
                     id="toolu_1"),
            FinalAnswer(content="done"),
        ])
        agent = Agent(model=model, registry=create_default_registry(),
                      store=store, budget=budget)

        # 第一次 decide 消耗唯一一次 model 配额，第二次 decide 即超限
        with pytest.raises(BudgetExceededError):
            agent.run("t1", "计算 1")

        assert store.get("t1").snapshot() == ()

    def test_tool_call_计数进入_budget(self) -> None:
        from mini_deerflow.middleware import BudgetExceededError

        store = InMemoryThreadStore()
        store.create("t1")
        # max_tool_calls=0 → 第一次工具调用就超限
        budget = Budget(max_steps=10, max_model_calls=10, max_tool_calls=0,
                        max_subagent_tasks=2, max_subagent_depth=3)
        model = _ScriptedModel([
            ToolCall(name="calculator", arguments={"expression": "1"},
                     id="toolu_1"),
            FinalAnswer(content="done"),
        ])
        agent = Agent(model=model, registry=create_default_registry(),
                      store=store, budget=budget)

        with pytest.raises(BudgetExceededError):
            agent.run("t1", "计算 1")


# ---------------------------------------------------------------------------
# 6. FakeModel 回归
# ---------------------------------------------------------------------------


class TestFakeModel回归:
    def test_忽略_tools_仍按内置规则决定(self) -> None:
        specs = create_default_registry().tool_specs()
        decision = FakeModel().decide(
            [Message(role="user", content="计算 1+1")], specs
        )
        assert isinstance(decision, ToolCall)
        assert decision.name == "calculator"

    def test_无工具时也不受_tools_影响(self) -> None:
        assert FakeModel().decide([Message(role="user", content="你好")]) is not None
        assert FakeModel().decide([Message(role="user", content="你好")], []) is not None

    def test_fake_model_闭环仍可用(self) -> None:
        store = InMemoryThreadStore()
        store.create("t1")
        agent = Agent(model=FakeModel(), registry=create_default_registry(),
                      store=store)
        answer, _ = agent.run("t1", "计算 1+1")
        assert answer == "计算结果: 2"
        roles = [m.role for m in store.get("t1").snapshot()]
        assert roles == ["user", "assistant"]
