"""MiniMax API 连通性验证（Anthropic 兼容接口，21A）。

**独立、显式执行**，不纳入默认 pytest。

与旧版的区别：
- 旧版直接调用 `client.messages.create()`，只证明 SDK 能连通；
  新版走 `AnthropicCompatModel.decide()`，端到端验证 21A 的模型适配链路
  （Message 转换 → SDK 调用 → 响应解析 → FinalAnswer → 错误映射）。
- 只在**存在 API Key** 时才运行，缺少 Key 时直接跳过而不是报错。
- 输出模型名与非空回答；**绝不输出 API Key**。

执行方式：

    uv run python -m mini_deerflow.mimo_smoke_test
"""

from __future__ import annotations

import sys

from mini_deerflow.anthropic_compat_model import AnthropicCompatModel
from mini_deerflow.model_config import (
    PROVIDER_VAR,
    REAL_PROVIDERS,
    load_model_settings,
    provider_key_var,
)
from mini_deerflow.model_errors import ModelConfigError
from mini_deerflow.models import FinalAnswer, Message

_PROMPT = "只回复：连接成功"


def main() -> int:
    """返回进程退出码：0 成功、2 跳过、1 失败。"""
    try:
        settings = load_model_settings()
    except ModelConfigError as exc:
        # 配置本身非法（如 provider 拼错、数值格式错）
        print(f"配置错误: {exc}", file=sys.stderr)
        return 1

    if settings.provider not in REAL_PROVIDERS or not settings.api_key:
        # 未选择真实 provider 或没有 Key —— 不消耗额度，直接跳过
        print(
            "跳过："
            f"{PROVIDER_VAR} 需为 {'/'.join(sorted(REAL_PROVIDERS))} 之一，"
            f"并配置对应的 API Key（如 {provider_key_var('minimax')}）。",
            file=sys.stderr,
        )
        return 2

    # 只输出模型名与 base_url，不输出 API Key
    print(f"模型: {settings.model}")
    print(f"接口: {settings.base_url}")

    try:
        model = AnthropicCompatModel(settings)
        decision = model.decide([Message(role="user", content=_PROMPT)])
    except Exception as exc:  # noqa: BLE001 - smoke 需要把失败原样报给用户
        # str(exc) 只含状态码/类型名等非敏感信息（见 AnthropicCompatModel 的异常映射）
        print(f"请求失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if not isinstance(decision, FinalAnswer):
        print(
            f"失败：21A 预期 FinalAnswer，实际返回 {type(decision).__name__}",
            file=sys.stderr,
        )
        return 1

    if not decision.content.strip():
        print("失败：模型返回了空回答", file=sys.stderr)
        return 1

    print(f"回答: {decision.content}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())