"""Anthropic 兼容接口的模型适配器（21A）。

对接任意 **Anthropic 兼容**的 messages 接口，当前服务两个提供方：

| provider | 接口 | 默认模型 |
|---|---|---|
| `minimax` | `https://api.minimaxi.com/anthropic` | `MiniMax-M3` |
| `mimo` | `https://api.xiaomimimo.com/anthropic` | `mimo-v2.6-flash` |

具体哪个提供方由 `ModelSettings` 决定，本类只负责协议与错误映射，
因此类名不绑定任何单一供应商（早期叫 `MiniMaxModel`，加第二个提供方后会名不符实）。

调用方式与既有的 mimo_smoke_test 保持一致：
`anthropic.Anthropic(... default_headers={"X-Api-Key": ...})` + `messages.create()`。

21B 边界：
- 文本响应 → `FinalAnswer`
- `tool_use` 块 → `ToolCall`（**保留 provider 的 call_id**）
- **只支持串行单工具调用**：一次响应出现多个 tool_use 直接抛
  `ModelProtocolError`，绝不静默丢弃（丢了就会让模型以为的工具调用
  与实际执行的对不上，产生无法排查的结果）
- 严格校验：`tool_use.input` 必须是 JSON object，且 schema 声明的
  必填字段必须齐备
- 第二轮请求把上一轮的 `tool_use` 与 `tool_result` **按 call_id 配对**还原，
  否则供应商会拒绝该轮（`tool_result` 找不到对应 `tool_use`）

不做的事：
- 不在日志、repr、异常消息中输出 API Key
- 不在模块 import 期创建客户端或发起网络请求（客户端在 __init__ 创建，
  __init__ 由 create_model() 在 runtime 真正启动时调用）
"""

from __future__ import annotations

from typing import Any

import anthropic

from mini_deerflow.model_config import ModelSettings
from mini_deerflow.model_errors import (
    ModelAuthError,
    ModelEmptyResponseError,
    ModelError,
    ModelProtocolError,
    ModelRateLimitError,
    ModelServerError,
    ModelTimeoutError,
)
from mini_deerflow.models import FinalAnswer, Message, Model, ToolCall, ToolSpec

# 与 mimo_smoke_test 保持一致的单轮回答上限
DEFAULT_MAX_TOKENS = 1024

# Anthropic 兼容接口只有 user / assistant 两种角色。
# 内部额外的 "tool" 角色：只有与紧邻的 assistant tool_use 配对时才还原成
# tool_result 块；孤立的 tool 结果（历史遗留、只落库没落 tool_use）退回为
# 普通用户文本，避免发出找不到对应 tool_use 的 tool_result（供应商会拒收）。
_PROVIDER_ROLES: dict[str, str] = {
    "user": "user",
    "assistant": "assistant",
    "tool": "user",
}


def _paired_tool_indices(messages: list[Message]) -> set[int]:
    """找出可还原为 tool_result 的 tool 消息下标。

    判定条件：本条是 tool 且带 call_id，且**紧邻的上一条**是携带相同 call_id
    的 assistant。两者缺一都不成对——供应商要求 tool_result 必须紧跟它的 tool_use。
    """
    paired: set[int] = set()
    for index in range(1, len(messages)):
        current = messages[index]
        previous = messages[index - 1]
        if (
            current.role == "tool"
            and current.tool_call_id
            and previous.role == "assistant"
            and previous.tool_call_id == current.tool_call_id
        ):
            paired.add(index)
    return paired


def provider_messages_from(messages: list[Message]) -> list[dict[str, Any]]:
    """把内部 Message 列表转换为供应商 messages 格式（含工具往返）。

    三种形态：
    1. 携带 `tool_call_id` + `tool_name` 的 assistant
       → `[{"type":"tool_use","id","name","input"}]`
    2. 与上一条 assistant 成对的 tool
       → `user` + `[{"type":"tool_result","tool_use_id","content"}]`
    3. 其余（含历史里的孤立 tool 结果）
       → 普通文本消息

    Raises:
        ModelError: 消息为空，或出现不支持的角色。
    """
    if not messages:
        raise ModelError("消息列表为空，无法调用模型")

    paired = _paired_tool_indices(messages)
    converted: list[dict[str, Any]] = []

    for index, message in enumerate(messages):
        # 1) 还原 tool_use（只带文本的 assistant 走普通分支）
        if (
            message.role == "assistant"
            and message.tool_call_id
            and message.tool_name is not None
        ):
            converted.append({
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": message.tool_call_id,
                    "name": message.tool_name,
                    "input": dict(message.tool_arguments or {}),
                }],
            })
            continue

        # 2) 还原 tool_result（必须紧邻配对的 assistant）
        if message.role == "tool" and index in paired:
            converted.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": message.content,
                }],
            })
            continue

        # 3) 普通文本消息（含孤立的历史 tool 结果）
        role = _PROVIDER_ROLES.get(message.role)
        if role is None:
            raise ModelError(f"不支持的消息角色: {message.role!r}")
        converted.append({"role": role, "content": message.content})

    return converted


def anthropic_tools_from(specs: list[ToolSpec] | None) -> list[dict[str, Any]] | None:
    """ToolDefinition（供应商无关）→ Anthropic `tools` 字段。

    两者结构同构（`name` / `description` / `input_schema`），但仍显式转换而不是
    直接透传：这样一旦某天字段名要改，只动这里，`ToolSpec` 与注册表不受影响。

    Raises:
        ModelProtocolError: schema 不是对象，或缺少 type=object。
    """
    if not specs:
        return None

    tools: list[dict[str, Any]] = []
    for spec in specs:
        schema = spec.input_schema
        if not isinstance(schema, dict):
            raise ModelProtocolError(
                f"工具 {spec.name} 的 input_schema 必须是对象"
            )
        if schema.get("type") != "object":
            raise ModelProtocolError(
                f"工具 {spec.name} 的 input_schema.type 必须是 object，"
                f"当前: {schema.get('type')!r}"
            )
        tools.append({
            "name": spec.name,
            "description": spec.description,
            "input_schema": schema,
        })
    return tools


def create_client(settings: ModelSettings) -> anthropic.Anthropic:
    """按配置构造 anthropic 客户端（构造不发网络请求）。

    `default_headers={"X-Api-Key": ...}` 是 MiniMax 兼容接口所需，
    与 mimo_smoke_test 中已验证可用的调用方式保持一致。
    """
    if not settings.api_key:
        raise ModelError("AnthropicCompatModel 需要 API Key")
    return anthropic.Anthropic(
        api_key=settings.api_key,
        base_url=settings.base_url,
        default_headers={"X-Api-Key": settings.api_key},
        timeout=settings.timeout_seconds,
        max_retries=settings.max_retries,
    )


class AnthropicCompatModel:
    """通过 Anthropic 兼容接口调用模型的 Model 实现（minimax / mimo 共用）。"""

    def __init__(self, settings: ModelSettings, client: Any | None = None) -> None:
        """构建模型。**不发起网络请求**。

        Args:
            settings: 已校验过的模型配置（provider 为 minimax 或 mimo，且 api_key 非空）。
            client: 测试注入的假客户端；为 None 时按 settings 构造真实客户端。
        """
        if not settings.api_key and client is None:
            # 防御：绕过 factory 直接构造时同样 fail-closed
            raise ModelError("AnthropicCompatModel 需要 API Key")
        self._settings = settings
        self._client = client if client is not None else create_client(settings)

    def __repr__(self) -> str:
        """隐藏 API Key 的 repr —— 禁止凭据进入日志或异常信息。"""
        return (
            f"AnthropicCompatModel(model={self._settings.model!r}, "
            f"provider={self._settings.provider!r})"
        )

    __str__ = __repr__

    @property
    def model_name(self) -> str:
        """供应商模型名（供日志与 smoke 输出，不含凭据）。"""
        return self._settings.model

    def decide(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
    ) -> ToolCall | FinalAnswer:
        """请求模型给出决定；可能返回 FinalAnswer，也可能返回 ToolCall。

        Args:
            messages: 当前对话（含上一轮的 tool_use / tool_result 配对）。
            tools: 当前允许调用的工具描述；None 表示本轮不提供工具。

        Returns:
            FinalAnswer 或单个 ToolCall（带 provider 的 call_id）。

        Raises:
            ModelTimeoutError / ModelAuthError / ModelRateLimitError /
            ModelServerError / ModelEmptyResponseError / ModelProtocolError /
            ModelError
        """
        payload = provider_messages_from(messages)
        anthropic_tools = anthropic_tools_from(tools)

        extra: dict[str, Any] = {}
        if anthropic_tools:
            # 只有本轮提供工具时才带上 tools 字段；不传时行为与 21A 完全一致，
            # 因此旧的纯文本路径不受影响。
            extra["tools"] = anthropic_tools

        try:
            response = self._client.messages.create(
                model=self._settings.model,
                max_tokens=DEFAULT_MAX_TOKENS,
                messages=payload,
                **extra,
            )
        except anthropic.APITimeoutError as exc:
            raise ModelTimeoutError(
                f"模型请求超时（model={self._settings.model}）"
            ) from exc
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            status = getattr(exc, "status_code", "?")
            # 鉴权失败最贴近凭据本身：用 from None 断开 cause 链，
            # 保证 traceback 与日志不会带上原始凭据相关上下文。
            raise ModelAuthError(f"模型鉴权失败（HTTP {status}）") from None
        except anthropic.RateLimitError as exc:
            status = getattr(exc, "status_code", "?")
            raise ModelRateLimitError(f"模型请求被限流（HTTP {status}）") from exc
        except anthropic.APIStatusError as exc:
            status = getattr(exc, "status_code", "?")
            if status == 402:
                # 实测该接口用 insufficient_balance_error 表示余额不足。
                # 只按稳定状态码取文案，不依赖供应商 message 措辞。
                raise ModelServerError("模型账户余额不足（HTTP 402）") from exc
            if isinstance(status, int) and status >= 500:
                raise ModelServerError(
                    f"模型服务内部错误（HTTP {status}）"
                ) from exc
            raise ModelServerError(f"模型请求被拒绝（HTTP {status}）") from exc
        except anthropic.APIConnectionError as exc:
            raise ModelServerError("无法连接模型服务") from exc
        except anthropic.APIError as exc:
            # 兜底：只暴露异常类型名，不透传供应商原始 message
            raise ModelServerError(f"模型调用失败（{type(exc).__name__}）") from exc

        return self._parse_decision(response, tools)

    # ------------------------------------------------------------------ 响应解析

    @classmethod
    def _parse_decision(
        cls,
        response: Any,
        tools: list[ToolSpec] | None = None,
    ) -> ToolCall | FinalAnswer:
        """把供应商响应块解析为领域决策。

        有 tool_use 就走工具分支（且只允许一个）；否则要求至少有一段可读文本。
        """
        blocks = getattr(response, "content", None) or []
        tool_use_blocks = [
            block for block in blocks if getattr(block, "type", None) == "tool_use"
        ]

        if len(tool_use_blocks) > 1:
            # 关键：不静默取第一个。丢掉的 tool_use 对模型来说是"已发出的调用"，
            # 静默忽略会让模型以为工具执行了却拿不到结果。
            raise ModelProtocolError(
                f"模型一次返回了 {len(tool_use_blocks)} 个 tool_use；"
                "当前只支持串行单工具调用，拒绝执行以免静默丢弃"
            )

        if tool_use_blocks:
            tool_call = cls._tool_call_from_block(tool_use_blocks[0])
            cls._validate_arguments(tool_call, tools)
            return tool_call

        return FinalAnswer(content=cls._extract_text(response))

    @staticmethod
    def _tool_call_from_block(block: Any) -> ToolCall:
        """tool_use 块 → 领域 ToolCall；字段缺失或类型不符一律抛协议错误。"""
        call_id = getattr(block, "id", None)
        name = getattr(block, "name", None)
        if not isinstance(call_id, str) or not call_id:
            raise ModelProtocolError("tool_use 缺少 id，无法与后续 tool_result 配对")
        if not isinstance(name, str) or not name:
            raise ModelProtocolError("tool_use 缺少 name")

        arguments = getattr(block, "input", None)
        if not isinstance(arguments, dict):
            raise ModelProtocolError(
                f"tool_use.input 必须是 JSON object，收到: {type(arguments).__name__}"
            )
        return ToolCall(name=name, arguments=dict(arguments), id=call_id)

    @staticmethod
    def _validate_arguments(
        tool_call: ToolCall,
        tools: list[ToolSpec] | None,
    ) -> None:
        """按工具 schema 校验必填参数。

        只校验**已声明**的必填项：不在本轮工具列表里的名字交给
        ToolRegistry 抛 `ToolNotFoundError`（语义与既有一致）。
        """
        if not tools:
            return
        spec = next((t for t in tools if t.name == tool_call.name), None)
        if spec is None:
            return

        required = spec.input_schema.get("required")
        if not isinstance(required, list):
            return
        missing = [key for key in required if key not in tool_call.arguments]
        if missing:
            raise ModelProtocolError(
                f"工具 {tool_call.name} 缺少必填参数: {', '.join(map(str, missing))}"
            )

    @staticmethod
    def _extract_text(response: Any) -> str:
        """从响应中提取拼接后的文本；没有可读文本时抛 ModelEmptyResponseError。"""
        blocks = getattr(response, "content", None)
        if not blocks:
            raise ModelEmptyResponseError("模型返回了空响应")

        texts: list[str] = []
        for block in blocks:
            if getattr(block, "type", None) != "text":
                continue
            text = getattr(block, "text", None)
            if isinstance(text, str) and text.strip():
                texts.append(text)

        if not texts:
            raise ModelEmptyResponseError("模型响应中没有文本内容")

        combined = "".join(texts).strip()
        if not combined:
            raise ModelEmptyResponseError("模型返回了空白响应")
        return combined