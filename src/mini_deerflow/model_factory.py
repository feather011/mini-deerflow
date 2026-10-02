"""统一 Model Factory（21A）。

CLI 与 FastAPI 都通过 `create_model()` 获得 Model，保证三条启动路径
用同一套 provider 选择与 fail-closed 规则。

约定：
- 默认 provider=fake → 返回 FakeModel，测试与本地开发稳定可预期
- provider=minimax / mimo 且缺少对应 API Key → 抛 ModelConfigError，
  **绝不静默降级 FakeModel**
- 构造 AnthropicCompatModel 不发起网络请求（真正的网络只发生在 decide()）
"""

from __future__ import annotations

from mini_deerflow.anthropic_compat_model import AnthropicCompatModel
from mini_deerflow.model_config import (
    PROVIDERS,
    ModelSettings,
    load_model_settings,
)
from mini_deerflow.model_errors import ModelConfigError
from mini_deerflow.models import FakeModel, Model


def create_model(settings: ModelSettings | None = None) -> Model:
    """按配置构造 Model。

    Args:
        settings: 为 None 时读取环境（含 .env）。

    Returns:
        FakeModel 或 AnthropicCompatModel。

    Raises:
        ModelConfigError: provider 非法，或真实 provider 但没有 API Key。
    """
    resolved = settings if settings is not None else load_model_settings()

    if resolved.provider == "fake":
        return FakeModel()

    if resolved.provider not in PROVIDERS:
        raise ModelConfigError(
            f"未知模型 provider: {resolved.provider!r}"
            f"（允许: {' / '.join(sorted(PROVIDERS))}）"
        )

    # fail-closed：缺少 Key 时启动失败，禁止降级到 FakeModel
    if not resolved.api_key:
        raise ModelConfigError(
            f"provider={resolved.provider} 但未配置 API Key。"
            "请设置 MINIMAX_API_KEY（minimax）或 MIMO_API_KEY（mimo），"
            "可在系统环境变量或 .env 中配置。"
        )

    # minimax 与 mimo 走同一套 Anthropic 兼容协议
    return AnthropicCompatModel(resolved)