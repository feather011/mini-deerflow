"""模型配置与加载（21A）。

### 提供方

| provider | Key | Base URL | Model |
|---|---|---|---|
| `minimax` | `MINIMAX_API_KEY` | `MINIMAX_BASE_URL` | `MINIMAX_MODEL` |
| `mimo` | `MIMO_API_KEY` | `MIMO_BASE_URL` | `MIMO_MODEL` |
| `fake`（默认） | — | — | — |

**只认上述提供方变量 + 2 个传输层变量**，没有别名：

    MINI_DEERFLOW_MODEL_PROVIDER   fake | minimax | mimo（默认 fake）
    MINI_DEERFLOW_MODEL_TIMEOUT_SECONDS   默认 60
    MINI_DEERFLOW_MODEL_MAX_RETRIES       默认 2

传输层变量**去提供方化**：`minimax` 与 `mimo` 走同一套 Anthropic 兼容协议，
timeout / 重试是传输属性，不该挂在某个供应商名字下
（旧名 `MINIMAX_TIMEOUT_SECONDS` 在只有 minimax 时尚可，加第二个提供方就会误导）。

### 配置优先级（严格三层）

    系统环境变量  >  .env  >  代码默认值

由 `load_dotenv()` 的**默认 `override=False`** 实现：已存在的进程环境变量不会被
`.env` 覆盖，因此系统环境变量占优；`.env` 中存在而系统环境没有的键会被写入，
优先于代码默认值。

生产代码与 smoke **禁止使用 `load_dotenv(override=True)`**（有测试把守）：
那会让 `.env` 覆盖真实进程环境，使容器/CI 注入的凭据失效。

### 已刻意移除的旧变量

| 旧变量 | 为什么不兼容 |
|---|---|
| `DASHSCOPE_API_KEY` | 属于阿里云 DashScope。继续兼容会在两个提供方之间**串用凭据** |
| `BASE_URL` / `MODEL_NAME` | 名字过于通用，容易与无关进程的环境变量撞名 |
| `MINIMAX_TIMEOUT_SECONDS` / `MINIMAX_MAX_RETRIES` | 已去提供方化，改名为 `MINI_DEERFLOW_MODEL_*` |

旧的 `MIMO_API_KEY` / `MIMO_BASE_URL` / `MIMO_MODEL` 曾被当作 MiniMax 的别名而移除；
现在它们是 **`mimo` provider 的正式变量**——语义是"提供方各自的配置"，
不再是"别人的别名"，这正是移除别名要达到的目的。

注：显式设置为空串的系统环境变量**会被视为存在**（python-dotenv 不覆盖它），
因此会挡住 `.env` 中的值并最终 fail-closed，而不是静默回落。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from mini_deerflow.model_errors import ModelConfigError

DEFAULT_BASE_URL = "https://api.minimaxi.com/anthropic"
DEFAULT_MODEL = "MiniMax-M3"
MIMO_DEFAULT_BASE_URL = "https://api.xiaomimimo.com/anthropic"
MIMO_DEFAULT_MODEL = "mimo-v2.6-flash"
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RETRIES = 2

# 合法 provider 取值
PROVIDERS: frozenset[str] = frozenset({"fake", "minimax", "mimo"})

# 正式配置项（唯一事实来源，不设别名）
PROVIDER_VAR = "MINI_DEERFLOW_MODEL_PROVIDER"
TIMEOUT_VAR = "MINI_DEERFLOW_MODEL_TIMEOUT_SECONDS"
RETRIES_VAR = "MINI_DEERFLOW_MODEL_MAX_RETRIES"


@dataclass(frozen=True)
class _ProviderEnv:
    """某个真实提供方对应的三个环境变量与其默认值。"""

    key_var: str
    base_url_var: str
    model_var: str
    default_base_url: str
    default_model: str


# 每个真实 provider 的配置三元组
_PROVIDER_ENV: dict[str, _ProviderEnv] = {
    "minimax": _ProviderEnv(
        key_var="MINIMAX_API_KEY",
        base_url_var="MINIMAX_BASE_URL",
        model_var="MINIMAX_MODEL",
        default_base_url=DEFAULT_BASE_URL,
        default_model=DEFAULT_MODEL,
    ),
    "mimo": _ProviderEnv(
        key_var="MIMO_API_KEY",
        base_url_var="MIMO_BASE_URL",
        model_var="MIMO_MODEL",
        default_base_url=MIMO_DEFAULT_BASE_URL,
        default_model=MIMO_DEFAULT_MODEL,
    ),
}

# 需要凭据的真实提供方（PROVIDERS 中除 fake 之外的）
REAL_PROVIDERS: frozenset[str] = frozenset(_PROVIDER_ENV)


def provider_key_var(provider: str) -> str | None:
    """返回该 provider 承载凭据的环境变量名；fake 返回 None。"""
    spec = _PROVIDER_ENV.get(provider)
    return spec.key_var if spec else None


@dataclass(frozen=True)
class ModelSettings:
    """模型运行配置（纯数据，读取环境见 load_model_settings）。

    `base_url` / `model` 默认 None，由 `__post_init__` 按 provider 补默认值。
    这样「直接构造 `ModelSettings(provider='mimo', api_key=...)`」与
    「走 `load_model_settings()`」得到一致的 provider 默认值，
    不会把 `MiniMax-M3` 这种默认模型名错配到 MiMo 接口上。
    """

    provider: str = "fake"
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES

    def __post_init__(self) -> None:
        spec = _PROVIDER_ENV.get(self.provider)
        default_base_url = spec.default_base_url if spec else DEFAULT_BASE_URL
        default_model = spec.default_model if spec else DEFAULT_MODEL
        if self.base_url is None:
            object.__setattr__(self, "base_url", default_base_url)
        if self.model is None:
            object.__setattr__(self, "model", default_model)

    def __repr__(self) -> str:
        """隐藏 API Key 的 repr —— 禁止凭据进入日志或异常信息。"""
        return (
            "ModelSettings("
            f"provider={self.provider!r}, "
            f"api_key={'***' if self.api_key else None}, "
            f"base_url={self.base_url!r}, "
            f"model={self.model!r}, "
            f"timeout_seconds={self.timeout_seconds!r}, "
            f"max_retries={self.max_retries!r})"
        )

    # 与 repr 保持一致，避免意外泄漏
    __str__ = __repr__


def _present(env: Mapping[str, str], name: str) -> str | None:
    """返回非空且去除首尾空白的值；未定义或空白返回 None。"""
    value = env.get(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def _read_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = _present(env, name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ModelConfigError(
            f"环境变量 {name} 必须是数字，当前值: {raw!r}"
        ) from None


def _read_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _present(env, name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ModelConfigError(
            f"环境变量 {name} 必须是整数，当前值: {raw!r}"
        ) from None


def _parse(env: Mapping[str, str]) -> ModelSettings:
    """纯解析：只认正式变量，不含任何别名回退。"""
    provider_raw = env.get(PROVIDER_VAR, "fake") or "fake"
    provider = provider_raw.strip().lower()
    if provider not in PROVIDERS:
        raise ModelConfigError(
            f"未知模型 provider: {provider_raw!r}"
            f"（允许: {' / '.join(sorted(PROVIDERS))}）"
        )

    # fake 不需要任何提供方凭据
    spec = _PROVIDER_ENV.get(provider)
    if spec is None:
        api_key: str | None = None
        base_url = DEFAULT_BASE_URL
        model = DEFAULT_MODEL
    else:
        api_key = _present(env, spec.key_var)
        base_url = _present(env, spec.base_url_var) or spec.default_base_url
        model = _present(env, spec.model_var) or spec.default_model

    return ModelSettings(
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        model=model,
        timeout_seconds=_read_float(env, TIMEOUT_VAR, DEFAULT_TIMEOUT_SECONDS),
        max_retries=_read_int(env, RETRIES_VAR, DEFAULT_MAX_RETRIES),
    )


def load_model_settings(
    env: Mapping[str, str] | None = None,
    dotenv_path: str | os.PathLike[str] | None = None,
) -> ModelSettings:
    """读取模型配置。

    这是 **CLI、FastAPI、smoke 三者共用的唯一配置入口**；调用方不得自行
    加载或覆盖环境变量。

    Args:
        env: 环境变量映射；为 None 时加载 .env 后读取 os.environ。
             传入 env 供测试注入，测试因此不需修改真实进程环境。
        dotenv_path: `.env` 文件路径；为 None 时按 python-dotenv 默认规则查找。
             显式传入是为了让「哪份 .env 参与解析」成为可控参数——
             默认规则是**沿调用栈第一个非 dotenv 帧的文件目录向上查找**，
             在 pytest 下从测试文件出发会解析到完全不同的位置，
             依赖它做断言既不可靠也不可复现。

    Returns:
        三层优先级解析后的 ModelSettings。

    Raises:
        ModelConfigError: provider 非法 / 数值格式非法 / 真实 provider 缺 Key。
    """
    if env is None:
        # override=False（python-dotenv 默认）= 系统环境变量优先。
        # 不使用 override=True：那会让 .env 覆盖真实进程环境，
        # 使容器/CI 注入的凭据失效，并掩盖"系统里已有另一个提供方的 key"这一事实。
        from dotenv import load_dotenv

        load_dotenv(dotenv_path)
        env = os.environ

    settings = _parse(env)

    # fail-closed：选了真实 provider 却没有 Key 时立刻失败，绝不静默降级 FakeModel
    if settings.provider != "fake" and not settings.api_key:
        spec = _PROVIDER_ENV[settings.provider]
        raise ModelConfigError(
            f"已设置 {PROVIDER_VAR}={settings.provider}，但未配置 API Key。"
            f"请在系统环境变量或 .env 中设置 {spec.key_var}。"
        )

    return settings