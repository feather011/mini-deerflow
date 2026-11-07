"""AnthropicCompatModel 单元测试（21A）。

全部注入假客户端，**不发任何网络请求、不消耗额度**。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest

from mini_deerflow.anthropic_compat_model import (
    AnthropicCompatModel,
    extract_system_prompt,
    provider_messages_from,
)
from mini_deerflow.model_config import ModelSettings
from mini_deerflow.model_errors import (
    ModelAuthError,
    ModelEmptyResponseError,
    ModelError,
    ModelRateLimitError,
    ModelServerError,
    ModelTimeoutError,
)
from mini_deerflow.models import FinalAnswer, Message, Model, ToolCall

KEY = "sk-UNIT-TEST-SECRET-KEY"


# ---------------------------------------------------------------------------
# 假客户端
# ---------------------------------------------------------------------------


class FakeStream:
    """`client.messages.stream()` 的最小替身。

    `stream_decide()` 用 `with ... as stream:` 逐事件迭代；这里给一个
    只含 `message_stop` 的空流，让它立刻结束 —— 本组用例要断言的是
    **发出去的请求参数**，不是 token 增量。
    """

    def __init__(self, kwargs: dict[str, Any]) -> None:
        self._kwargs = kwargs

    def __enter__(self) -> "FakeStream":
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        return False

    def __iter__(self) -> Any:
        yield SimpleNamespace(type="message_stop")


class FakeMessages:
    def __init__(self, result: Any) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    def stream(self, **kwargs: Any) -> FakeStream:
        self.stream_calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return FakeStream(kwargs)


class FakeClient:
    def __init__(self, result: Any) -> None:
        self.messages = FakeMessages(result)


def make_model(result: Any) -> tuple[AnthropicCompatModel, FakeMessages]:
    client = FakeClient(result)
    settings = ModelSettings(provider="minimax", api_key=KEY)
    return AnthropicCompatModel(settings, client=client), client.messages


def text_response(*texts: str) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=t) for t in texts],
        model="MiniMax-M3",
    )


def _http_error(cls: type, status: int) -> Exception:
    request = httpx.Request("POST", "https://api.minimaxi.com/anthropic/v1/messages")
    response = httpx.Response(status, request=request, text="供应商错误体")
    return cls(f"Error code: {status} - 供应商错误体", response=response, body=None)


# ---------------------------------------------------------------------------
# Message 转换
# ---------------------------------------------------------------------------


class TestMessage转换:
    def test_user_映射为_user(self) -> None:
        out = provider_messages_from([Message(role="user", content="你好")])
        assert out == [{"role": "user", "content": "你好"}]

    def test_assistant_映射为_assistant(self) -> None:
        out = provider_messages_from([Message(role="assistant", content="好的")])
        assert out == [{"role": "assistant", "content": "好的"}]

    def test_tool_映射为_user(self) -> None:
        """Anthropic 兼容接口无 tool 角色；21A 不产生 ToolCall，属防御性映射。"""
        out = provider_messages_from([Message(role="tool", content="2")])
        assert out == [{"role": "user", "content": "2"}]

    def test_多条消息保持顺序(self) -> None:
        msgs = [
            Message(role="user", content="问题"),
            Message(role="assistant", content="思考"),
            Message(role="user", content="继续"),
        ]
        out = provider_messages_from(msgs)
        assert [m["role"] for m in out] == ["user", "assistant", "user"]
        assert [m["content"] for m in out] == ["问题", "思考", "继续"]

    def test_空消息列表_抛领域错误(self) -> None:
        with pytest.raises(ModelError):
            provider_messages_from([])

    def test_不支持的角色_抛领域错误(self) -> None:
        """真正不支持的角色仍要报错，不能默默变成别的角色发出去。"""
        with pytest.raises(ModelError) as exc:
            provider_messages_from([Message(role="system_admin", content="x")])
        assert "system_admin" in str(exc.value)


# ---------------------------------------------------------------------------
# system 消息 → 顶层 system 字段
# ---------------------------------------------------------------------------


class TestSystem消息:
    """`role="system"` 必须抽到 Anthropic 协议的顶层 `system=` 字段。

    背景：`ContextAssembler` 一直会生成 system 消息（摘要 / 任务笔记 /
    记忆 / Skill 上下文），而适配器过去把 system 当成"不支持的角色"直接
    抛错 —— 结果是：只要线程里有这些上下文，请求在**发送前**就失败，
    摘要、记忆与 Skill 上下文**从来没有真的发给过模型**。
    """

    def test_system_不出现在_messages_里(self) -> None:
        payload = provider_messages_from([
            Message(role="system", content="摘要段"),
            Message(role="user", content="问题"),
        ])
        assert [m["role"] for m in payload] == ["user"]
        assert payload[0]["content"] == "问题"

    def test_没有_system_时不产出_system_字段(self) -> None:
        """普通对话不带 system —— 不该改变既有的请求形状。"""
        payload = provider_messages_from([
            Message(role="user", content="你好"),
            Message(role="assistant", content="你好"),
        ])
        assert [m["role"] for m in payload] == ["user", "assistant"]

    def test_多条_system_按顺序拼接(self) -> None:
        """assembler 产出的摘要/笔记/记忆/Skill 是**多条**独立 system 消息。"""
        payload = provider_messages_from([
            Message(role="system", content="摘要段"),
            Message(role="system", content="记忆段"),
            Message(role="user", content="问题"),
        ])
        assert [m["role"] for m in payload] == ["user"]

    def test_extract_system_prompt_按原顺序拼接(self) -> None:
        """拼接顺序 = 产出顺序；空段被忽略；无 system 返回 None。"""
        assert extract_system_prompt([
            Message(role="system", content="摘要段"),
            Message(role="system", content="任务笔记段"),
            Message(role="system", content="记忆段"),
            Message(role="user", content="问题"),
        ]) == "摘要段\n\n任务笔记段\n\n记忆段"

        assert extract_system_prompt([
            Message(role="system", content="摘要段"),
            Message(role="system", content="   "),
            Message(role="user", content="问题"),
        ]) == "摘要段"

        assert extract_system_prompt([
            Message(role="user", content="问题"),
        ]) is None


# ---------------------------------------------------------------------------
# 顶层 system 字段的组装
# ---------------------------------------------------------------------------


class Test顶层System字段:
    """顶层 `system` 字段的组装：**实际调用 decide() / stream_decide()**，
    捕获真正发出去的请求参数来断言。

    不直接调 `provider_messages_from` —— 那样只能证明 messages 数组里
    没有 system，证明不了「顶层 system 字段真的带上了、且顺序正确」。
    """

    def _model(self) -> AnthropicCompatModel:
        return AnthropicCompatModel(
            ModelSettings(provider="minimax", api_key=KEY),
            client=FakeClient(text_response("ok")),
        )

    def test_decide_把_system_放进顶层字段(self) -> None:
        model = self._model()
        model.decide([
            Message(role="system", content="摘要段"),
            Message(role="user", content="问题"),
        ])
        sent = model._client.messages.calls[0]
        assert sent["system"] == "摘要段"
        assert all(m["role"] != "system" for m in sent["messages"])

    def test_没有_system_时_不传_system_字段(self) -> None:
        """不传 system 时请求形状与 21A 完全一致（旧的纯文本路径不受影响）。"""
        model = self._model()
        model.decide([Message(role="user", content="问题")])
        sent = model._client.messages.calls[0]
        assert "system" not in sent

    def test_多条_system_按原顺序拼接(self) -> None:
        """assembler 产出的摘要/笔记/记忆/Skill 是**多条**独立 system 消息。

        拼接顺序必须与产出顺序一致 —— 摘要在前、记忆在后，顺序错了
        模型看到的上下文优先级就反了。
        """
        model = self._model()
        model.decide([
            Message(role="system", content="摘要段"),
            Message(role="system", content="任务笔记段"),
            Message(role="system", content="记忆段"),
            Message(role="system", content="Skill段"),
            Message(role="user", content="问题"),
        ])
        sent = model._client.messages.calls[0]
        assert sent["system"] == "摘要段\n\n任务笔记段\n\n记忆段\n\nSkill段"

    def test_stream_路径_同样带上_system(self) -> None:
        """流式与非流式共用同一套 system 抽取，不能只修一条路径。"""
        model = self._model()
        list(model.stream_decide([
            Message(role="system", content="摘要段"),
            Message(role="user", content="问题"),
        ]))
        # 断言**流式请求本身**的参数（stream_decide 末尾还会调一次
        # decide()，那次的调用记录在 messages.calls 里，不是流式请求）
        sent = model._client.messages.stream_calls[0]
        assert sent["system"] == "摘要段"
        assert all(m["role"] != "system" for m in sent["messages"])

    def test_stream_路径_多条_system_按原顺序拼接(self) -> None:
        """流式路径的多段拼接顺序同样要正确。"""
        model = self._model()
        list(model.stream_decide([
            Message(role="system", content="摘要段"),
            Message(role="system", content="记忆段"),
            Message(role="user", content="问题"),
        ]))
        sent = model._client.messages.stream_calls[0]
        assert sent["system"] == "摘要段\n\n记忆段"

    def test_stream_路径_没有_system_时不传_system_字段(self) -> None:
        """无 system 时流式请求形状同样不变。"""
        model = self._model()
        list(model.stream_decide([Message(role="user", content="问题")]))
        sent = model._client.messages.stream_calls[0]
        assert "system" not in sent

    def test_空白的_system_段被忽略(self) -> None:
        """空白段不该在 system 里留下多余空行。"""
        model = self._model()
        model.decide([
            Message(role="system", content="摘要段"),
            Message(role="system", content="   "),
            Message(role="user", content="问题"),
        ])
        sent = model._client.messages.calls[0]
        assert sent["system"] == "摘要段"


# ---------------------------------------------------------------------------
# 文本响应 → FinalAnswer
# ---------------------------------------------------------------------------


class Test文本响应:
    def test_单块文本转_FinalAnswer(self) -> None:
        model, messages = make_model(text_response("连接成功"))
        answer = model.decide([Message(role="user", content="hi")])

        assert isinstance(answer, FinalAnswer)
        assert answer.content == "连接成功"
        assert isinstance(model, Model)

    def test_多块文本按序拼接并只strip整体(self) -> None:
        """逐块 strip 会吃掉块边界上的空格，故只对整体 strip。"""
        model, _ = make_model(text_response("第一段", " 第二段 "))
        answer = model.decide([Message(role="user", content="hi")])
        assert answer.content == "第一段 第二段"

    def test_单块首尾空白被去除(self) -> None:
        model, _ = make_model(text_response("  回答内容  "))
        answer = model.decide([Message(role="user", content="hi")])
        assert answer.content == "回答内容"

    def test_请求参数与配置一致(self) -> None:
        model, messages = make_model(text_response("ok"))
        model.decide([Message(role="user", content="hi")])

        call = messages.calls[0]
        assert call["model"] == "MiniMax-M3"
        assert call["max_tokens"] == 1024
        assert call["messages"] == [{"role": "user", "content": "hi"}]

    def test_只返回_FinalAnswer_不返回_ToolCall(self) -> None:
        """21A 明确边界：不伪造 ToolCall 支持。"""
        model, _ = make_model(text_response("ok"))
        answer = model.decide([Message(role="user", content="hi")])
        assert isinstance(answer, FinalAnswer)
        assert not isinstance(answer, ToolCall)


# ---------------------------------------------------------------------------
# 空响应
# ---------------------------------------------------------------------------


class Test空响应:
    def test_content_为空列表(self) -> None:
        model, _ = make_model(SimpleNamespace(content=[]))
        with pytest.raises(ModelEmptyResponseError):
            model.decide([Message(role="user", content="hi")])

    def test_content_为_None(self) -> None:
        model, _ = make_model(SimpleNamespace(content=None))
        with pytest.raises(ModelEmptyResponseError):
            model.decide([Message(role="user", content="hi")])

    def test_只有非文本块(self) -> None:
        """服务端只返回 thinking 之类非文本块时，不当作回答。"""
        resp = SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking="内心独白")]
        )
        model, _ = make_model(resp)
        with pytest.raises(ModelEmptyResponseError):
            model.decide([Message(role="user", content="hi")])

    def test_只有空白文本(self) -> None:
        model, _ = make_model(text_response("   \n  "))
        with pytest.raises(ModelEmptyResponseError):
            model.decide([Message(role="user", content="hi")])


# ---------------------------------------------------------------------------
# 错误映射
# ---------------------------------------------------------------------------


class Test错误映射:
    def test_超时映射为_ModelTimeoutError(self) -> None:
        request = httpx.Request("POST", "https://x.invalid/v1/messages")
        model, _ = make_model(anthropic.APITimeoutError(request=request))
        with pytest.raises(ModelTimeoutError):
            model.decide([Message(role="user", content="hi")])

    @pytest.mark.parametrize("status", [401, 403])
    def test_鉴权错误映射为_ModelAuthError(self, status: int) -> None:
        cls = (
            anthropic.AuthenticationError
            if status == 401
            else anthropic.PermissionDeniedError
        )
        model, _ = make_model(_http_error(cls, status))
        with pytest.raises(ModelAuthError) as exc:
            model.decide([Message(role="user", content="hi")])
        assert str(status) in str(exc.value)

    def test_限流映射为_ModelRateLimitError(self) -> None:
        model, _ = make_model(_http_error(anthropic.RateLimitError, 429))
        with pytest.raises(ModelRateLimitError) as exc:
            model.decide([Message(role="user", content="hi")])
        assert "429" in str(exc.value)

    @pytest.mark.parametrize("status", [500, 502, 503, 529])
    def test_5xx映射为_ModelServerError(self, status: int) -> None:
        model, _ = make_model(_http_error(anthropic.InternalServerError, status))
        with pytest.raises(ModelServerError) as exc:
            model.decide([Message(role="user", content="hi")])
        assert str(status) in str(exc.value)

    def test_其他状态码也映射为_ModelServerError(self) -> None:
        model, _ = make_model(_http_error(anthropic.BadRequestError, 400))
        with pytest.raises(ModelServerError) as exc:
            model.decide([Message(role="user", content="hi")])
        assert "400" in str(exc.value)

    def test_402_余额不足映射为可行动文案(self) -> None:
        """实测该接口以 insufficient_balance_error 返回 402。"""
        model, _ = make_model(_http_error(anthropic.APIStatusError, 402))
        with pytest.raises(ModelServerError) as exc:
            model.decide([Message(role="user", content="hi")])
        assert "余额不足" in str(exc.value)
        assert "402" in str(exc.value)

    def test_连接失败映射为_ModelServerError(self) -> None:
        request = httpx.Request("POST", "https://x.invalid/v1/messages")
        model, _ = make_model(
            anthropic.APIConnectionError(request=request, message="连接被拒绝")
        )
        with pytest.raises(ModelServerError):
            model.decide([Message(role="user", content="hi")])

    def test_领域异常都继承_ModelError(self) -> None:
        for exc_cls in (
            ModelTimeoutError,
            ModelAuthError,
            ModelRateLimitError,
            ModelServerError,
            ModelEmptyResponseError,
        ):
            assert issubclass(exc_cls, ModelError)


# ---------------------------------------------------------------------------
# 凭据不泄漏
# ---------------------------------------------------------------------------


class Test凭据不泄漏:
    def test_repr_不含_key(self) -> None:
        model, _ = make_model(text_response("ok"))
        assert KEY not in repr(model)
        assert "MiniMax-M3" in repr(model)

    def test_str_不含_key(self) -> None:
        model, _ = make_model(text_response("ok"))
        assert KEY not in str(model)

    @pytest.mark.parametrize(
        "factory",
        [
            lambda: _http_error(anthropic.AuthenticationError, 401),
            lambda: _http_error(anthropic.RateLimitError, 429),
            lambda: _http_error(anthropic.InternalServerError, 500),
        ],
    )
    def test_异常消息不含_key(self, factory) -> None:
        model, _ = make_model(factory())
        with pytest.raises(ModelError) as exc:
            model.decide([Message(role="user", content="hi")])
        assert KEY not in str(exc.value)
        assert KEY not in repr(exc.value)

    def test_鉴权错误不保留原始异常链(self) -> None:
        """from None：原始凭据相关上下文不得进入 traceback / 日志。"""
        model, _ = make_model(_http_error(anthropic.AuthenticationError, 401))
        with pytest.raises(ModelAuthError) as exc:
            model.decide([Message(role="user", content="hi")])
        assert exc.value.__cause__ is None


# ---------------------------------------------------------------------------
# 构造约束
# ---------------------------------------------------------------------------


class Test构造:
    def test_缺_key_且无客户端_构造失败(self) -> None:
        with pytest.raises(ModelError):
            AnthropicCompatModel(ModelSettings(provider="minimax", api_key=None))

    def test_构造模型不发起网络请求(self) -> None:
        """client 注入 + 仅 __init__：不应有任何 create 调用。"""
        client = FakeClient(text_response("ok"))
        AnthropicCompatModel(ModelSettings(provider="minimax", api_key=KEY), client=client)
        assert client.messages.calls == []

    def test_model_name_暴露供应商模型名(self) -> None:
        model, _ = make_model(text_response("ok"))
        assert model.model_name == "MiniMax-M3"