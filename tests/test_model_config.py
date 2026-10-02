"""模型配置与加载测试（21A）。

全部通过显式注入 env 映射，不修改真实进程环境、不发网络请求。
"""

from __future__ import annotations

import pytest

from mini_deerflow.model_config import (
    MIMO_DEFAULT_BASE_URL,
    MIMO_DEFAULT_MODEL,
    PROVIDERS,
    REAL_PROVIDERS,
    provider_key_var,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    ModelSettings,
    load_model_settings,
)
from mini_deerflow.model_errors import ModelConfigError


def _env(**kwargs: str) -> dict[str, str]:
    return {k: v for k, v in kwargs.items() if v is not None}


class TestDefaults:
    def test_未配置时使用默认值(self) -> None:
        s = load_model_settings(env={})
        assert s.provider == "fake"
        assert s.api_key is None
        assert s.base_url == DEFAULT_BASE_URL
        assert s.model == DEFAULT_MODEL
        assert s.timeout_seconds == 60.0
        assert s.max_retries == 2

    def test_model_settings_默认即_fake(self) -> None:
        assert ModelSettings().provider == "fake"
        assert ModelSettings().api_key is None


class TestProvider选择:
    def test_显式选择_fake(self) -> None:
        s = load_model_settings(
            env=_env(MINI_DEERFLOW_MODEL_PROVIDER="fake", MINIMAX_API_KEY="k")
        )
        assert s.provider == "fake"

    def test_选择_minimax_带_key(self) -> None:
        s = load_model_settings(
            env=_env(MINI_DEERFLOW_MODEL_PROVIDER="minimax", MINIMAX_API_KEY="k")
        )
        assert s.provider == "minimax"
        assert s.api_key == "k"

    def test_provider_大小写与空格被规范化(self) -> None:
        s = load_model_settings(
            env=_env(MINI_DEERFLOW_MODEL_PROVIDER="  MiniMax ", MINIMAX_API_KEY="k")
        )
        assert s.provider == "minimax"

    def test_未知_provider_抛配置错误(self) -> None:
        with pytest.raises(ModelConfigError) as exc:
            load_model_settings(env=_env(MINI_DEERFLOW_MODEL_PROVIDER="openai"))
        assert "openai" in str(exc.value)


class TestFailClosed:
    def test_选_minimax_但缺_key_立即失败(self) -> None:
        with pytest.raises(ModelConfigError) as exc:
            load_model_settings(env=_env(MINI_DEERFLOW_MODEL_PROVIDER="minimax"))
        assert "MINIMAX_API_KEY" in str(exc.value)

    def test_空白_key_视为缺失(self) -> None:
        with pytest.raises(ModelConfigError):
            load_model_settings(
                env=_env(MINI_DEERFLOW_MODEL_PROVIDER="minimax", MINIMAX_API_KEY="   ")
            )

    def test_缺_key_不会返回_fake_兜底(self) -> None:
        """禁止静默降级：抛错而不是返回 provider=fake 的配置。"""
        with pytest.raises(ModelConfigError):
            load_model_settings(env=_env(MINI_DEERFLOW_MODEL_PROVIDER="minimax"))


class Test旧变量不参与配置:
    """DASHSCOPE/MIMO/BASE_URL/MODEL_NAME 一律被忽略，不作为别名。"""

    def test_dashscope_key_不能充当_minimax_key(self) -> None:
        with pytest.raises(ModelConfigError) as exc:
            load_model_settings(
                env=_env(
                    MINI_DEERFLOW_MODEL_PROVIDER="minimax",
                    DASHSCOPE_API_KEY="OLD_KEY",
                )
            )
        assert "MINIMAX_API_KEY" in str(exc.value)

    def test_base_url_与_model_name_不被读取(self) -> None:
        s = load_model_settings(
            env=_env(
                MINI_DEERFLOW_MODEL_PROVIDER="minimax",
                MINIMAX_API_KEY="k",
                BASE_URL="https://old.example",
                MODEL_NAME="Old-Model",
            )
        )
        # 走默认值，而不是旧变量
        assert s.base_url == DEFAULT_BASE_URL
        assert s.model == DEFAULT_MODEL

    def test_旧变量与正式变量共存_只用正式变量(self) -> None:
        s = load_model_settings(
            env=_env(
                MINI_DEERFLOW_MODEL_PROVIDER="minimax",
                MINIMAX_API_KEY="NEW_KEY",
                DASHSCOPE_API_KEY="OLD_KEY",
                MIMO_API_KEY="MIMO_KEY",
                MINIMAX_BASE_URL="https://new.example",
                BASE_URL="https://old.example",
                MINIMAX_MODEL="New-Model",
                MODEL_NAME="Old-Model",
            )
        )
        assert s.api_key == "NEW_KEY"
        assert s.base_url == "https://new.example"
        assert s.model == "New-Model"

    def test_mimo_变量同样被忽略(self) -> None:
        s = load_model_settings(
            env=_env(MIMO_API_KEY="MIMO_KEY", MIMO_MODEL="mimo-v1")
        )
        assert s.api_key is None
        assert s.model == DEFAULT_MODEL


class Test数值解析:
    def test_timeout_可覆盖(self) -> None:
        s = load_model_settings(env=_env(MINI_DEERFLOW_MODEL_TIMEOUT_SECONDS="12.5"))
        assert s.timeout_seconds == 12.5

    def test_max_retries_可覆盖(self) -> None:
        s = load_model_settings(env=_env(MINI_DEERFLOW_MODEL_MAX_RETRIES="5"))
        assert s.max_retries == 5

    def test_timeout_非数字_抛配置错误(self) -> None:
        with pytest.raises(ModelConfigError) as exc:
            load_model_settings(env=_env(MINI_DEERFLOW_MODEL_TIMEOUT_SECONDS="abc"))
        assert "MINI_DEERFLOW_MODEL_TIMEOUT_SECONDS" in str(exc.value)

    def test_max_retries_非整数_抛配置错误(self) -> None:
        with pytest.raises(ModelConfigError):
            load_model_settings(env=_env(MINI_DEERFLOW_MODEL_MAX_RETRIES="x"))


class Test凭据不泄漏:
    KEY = "sk-TEST-SECRET-KEY-VALUE"

    def test_repr_不含_key(self) -> None:
        s = ModelSettings(provider="minimax", api_key=self.KEY)
        assert self.KEY not in repr(s)

    def test_str_不含_key(self) -> None:
        s = ModelSettings(provider="minimax", api_key=self.KEY)
        assert self.KEY not in str(s)

    def test_repr_仍展示其他配置项(self) -> None:
        s = ModelSettings(provider="minimax", api_key=self.KEY, model="MiniMax-M3")
        text = repr(s)
        assert "MiniMax-M3" in text
        assert "***" in text

    def test_加载后对象也不泄漏_key(self) -> None:
        s = load_model_settings(
            env=_env(MINI_DEERFLOW_MODEL_PROVIDER="minimax", MINIMAX_API_KEY=self.KEY)
        )
        assert self.KEY not in repr(s)
        assert self.KEY not in str(s)
        # 访问属性本身是必要的（工厂要用），但 repr 已脱敏
        assert s.api_key == self.KEY


# ---------------------------------------------------------------------------
# 三层优先级：系统环境变量 > .env > 默认值
# ---------------------------------------------------------------------------


class Test配置优先级:
    """针对 env=None 的真实路径（会走 load_dotenv）。

    显式传 dotenv_path：python-dotenv 默认是「沿调用栈第一个非 dotenv 帧的
    文件目录向上查找」，在 pytest 下从测试文件出发会解析到完全不同的位置，
    因此测试必须显式指定 .env 才可复现。

    需要放开 conftest 钉死的 provider，才能验证 .env 的真实加载。
    """

    @staticmethod
    def _write_env(tmp_path, content: str) -> str:
        env_file = tmp_path / ".env"
        env_file.write_text(content, encoding="utf-8")
        return str(env_file)

    @staticmethod
    def _clear(monkeypatch, *names: str) -> None:
        for name in names:
            monkeypatch.delenv(name, raising=False)

    @staticmethod
    def _clear_model_env(monkeypatch) -> None:
        """清掉全部模型配置变量。

        必须这样做：任何走真实路径的 load_model_settings() 都会调用 load_dotenv，
        而 python-dotenv 会**永久写入 os.environ**（包括仓库 .env 里的真实 Key）。
        不清的话：
        1. 本用例的断言会读到上一个用例残留的真实 Key，而不是本用例 .env 的值；
        2. 断言失败时 pytest 会把真实 Key 打进测试输出。
        """
        for name in (
            "MINI_DEERFLOW_MODEL_PROVIDER",
            "MINIMAX_API_KEY",
            "MINIMAX_BASE_URL",
            "MINIMAX_MODEL",
            "MINI_DEERFLOW_MODEL_TIMEOUT_SECONDS",
            "MINI_DEERFLOW_MODEL_MAX_RETRIES",
        ):
            monkeypatch.delenv(name, raising=False)

    def test_系统环境变量覆盖_env(self, monkeypatch, tmp_path) -> None:
        path = self._write_env(
            tmp_path,
            "\n".join(
                [
                    "MINI_DEERFLOW_MODEL_PROVIDER=minimax",
                    "MINIMAX_API_KEY=FROM_DOTENV",
                    "MINIMAX_MODEL=FromDotenv",
                    "MINI_DEERFLOW_MODEL_TIMEOUT_SECONDS=11",
                    "",
                ]
            ),
        )
        self._clear_model_env(monkeypatch)
        monkeypatch.setenv("MINIMAX_MODEL", "FromSystem")
        monkeypatch.setenv("MINI_DEERFLOW_MODEL_TIMEOUT_SECONDS", "22")

        s = load_model_settings(dotenv_path=path)
        assert s.model == "FromSystem"  # 系统 > .env
        assert s.timeout_seconds == 22.0
        assert s.api_key == "FROM_DOTENV"  # 系统未设置 → 用 .env

    def test_系统变量缺失时_env_生效(self, monkeypatch, tmp_path) -> None:
        path = self._write_env(
            tmp_path,
            "\n".join(
                [
                    "MINI_DEERFLOW_MODEL_PROVIDER=minimax",
                    "MINIMAX_API_KEY=FROM_DOTENV",
                    "MINIMAX_BASE_URL=https://from-env-file",
                    "MINIMAX_MODEL=FromDotenv",
                    "",
                ]
            ),
        )
        self._clear_model_env(monkeypatch)

        s = load_model_settings(dotenv_path=path)
        assert s.provider == "minimax"
        assert s.api_key == "FROM_DOTENV"
        assert s.base_url == "https://from-env-file"
        assert s.model == "FromDotenv"

    def test_两者都缺失时使用默认值(self, monkeypatch, tmp_path) -> None:
        path = self._write_env(
            tmp_path, "MINI_DEERFLOW_MODEL_PROVIDER=fake\n"
        )
        self._clear(
            monkeypatch,
            "MINIMAX_API_KEY",
            "MINIMAX_BASE_URL",
            "MINIMAX_MODEL",
            "MINI_DEERFLOW_MODEL_TIMEOUT_SECONDS",
            "MINI_DEERFLOW_MODEL_MAX_RETRIES",
        )

        s = load_model_settings(dotenv_path=path)
        assert s.api_key is None
        assert s.base_url == DEFAULT_BASE_URL
        assert s.model == DEFAULT_MODEL
        assert s.timeout_seconds == 60.0
        assert s.max_retries == 2

    def test_load_dotenv_不会以_override_true_调用(self, monkeypatch, tmp_path) -> None:
        """禁止生产代码使用 override=True——那会让 .env 覆盖真实进程环境。"""
        path = self._write_env(
            tmp_path, "MINI_DEERFLOW_MODEL_PROVIDER=fake\n"
        )

        import dotenv

        calls: list[dict] = []
        real = dotenv.load_dotenv

        def spy(*args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return real(*args, **kwargs)

        monkeypatch.setattr(dotenv, "load_dotenv", spy)

        load_model_settings(dotenv_path=path)

        assert calls, "load_model_settings 应当调用 load_dotenv"
        for call in calls:
            assert call["kwargs"].get("override") is not True


# ---------------------------------------------------------------------------
# mimo provider 与去提供方化的传输层变量
# ---------------------------------------------------------------------------


class TestMimoProvider:
    def test_选择_mimo_读取_MIMO_三件套(self) -> None:
        s = load_model_settings(
            env=_env(
                MINI_DEERFLOW_MODEL_PROVIDER="mimo",
                MIMO_API_KEY="MIMO_KEY",
                MIMO_BASE_URL="https://mimo.example/anthropic",
                MIMO_MODEL="mimo-v1",
            )
        )
        assert s.provider == "mimo"
        assert s.api_key == "MIMO_KEY"
        assert s.base_url == "https://mimo.example/anthropic"
        assert s.model == "mimo-v1"

    def test_mimo_使用自己的默认值(self) -> None:
        """不指定时用 MiMo 端点与模型，不回落到 MiniMax 的默认。"""
        s = load_model_settings(
            env=_env(MINI_DEERFLOW_MODEL_PROVIDER="mimo", MIMO_API_KEY="K")
        )
        assert s.base_url == MIMO_DEFAULT_BASE_URL
        assert s.model == MIMO_DEFAULT_MODEL

    def test_直接构造也按_provider_补默认(self) -> None:
        """绕过 loader 直接构造，同样不得把 MiniMax 默认模型配到 MiMo 端点。"""
        s = ModelSettings(provider="mimo", api_key="K")
        assert s.base_url == MIMO_DEFAULT_BASE_URL
        assert s.model == MIMO_DEFAULT_MODEL

    def test_minimax_默认值不受_mimo_影响(self) -> None:
        s = ModelSettings(provider="minimax", api_key="K")
        assert s.base_url == DEFAULT_BASE_URL
        assert s.model == DEFAULT_MODEL

    def test_mimo_缺_key_立即失败(self) -> None:
        with pytest.raises(ModelConfigError) as exc:
            load_model_settings(env=_env(MINI_DEERFLOW_MODEL_PROVIDER="mimo"))
        assert "MIMO_API_KEY" in str(exc.value)

    def test_mimo_不读取_MINIMAX_key(self) -> None:
        """跨提供方串用凭据必须失败。"""
        with pytest.raises(ModelConfigError):
            load_model_settings(
                env=_env(
                    MINI_DEERFLOW_MODEL_PROVIDER="mimo",
                    MINIMAX_API_KEY="NOT_THIS_ONE",
                )
            )

    def test_minimax_不读取_MIMO_key(self) -> None:
        with pytest.raises(ModelConfigError):
            load_model_settings(
                env=_env(
                    MINI_DEERFLOW_MODEL_PROVIDER="minimax",
                    MIMO_API_KEY="NOT_THIS_ONE",
                )
            )


class Test传输层变量去提供方化:
    """timeout / retries 是传输属性，不挂在某个供应商名下。"""

    def test_新传输层变量生效(self) -> None:
        s = load_model_settings(
            env=_env(
                MINI_DEERFLOW_MODEL_TIMEOUT_SECONDS="33",
                MINI_DEERFLOW_MODEL_MAX_RETRIES="7",
            )
        )
        assert s.timeout_seconds == 33.0
        assert s.max_retries == 7

    def test_传输层变量对两个_provider_都生效(self) -> None:
        s = load_model_settings(
            env=_env(
                MINI_DEERFLOW_MODEL_PROVIDER="mimo",
                MIMO_API_KEY="K",
                MINI_DEERFLOW_MODEL_TIMEOUT_SECONDS="12",
            )
        )
        assert s.timeout_seconds == 12.0

    def test_旧传输层变量被忽略(self) -> None:
        s = load_model_settings(
            env=_env(
                MINIMAX_TIMEOUT_SECONDS="99",
                MINIMAX_MAX_RETRIES="99",
            )
        )
        assert s.timeout_seconds == 60.0
        assert s.max_retries == 2

    def test_提供方列表包含_mimo(self) -> None:
        assert PROVIDERS == frozenset({"fake", "minimax", "mimo"})
        assert REAL_PROVIDERS == frozenset({"minimax", "mimo"})
        assert provider_key_var("mimo") == "MIMO_API_KEY"
        assert provider_key_var("minimax") == "MINIMAX_API_KEY"
        assert provider_key_var("fake") is None
