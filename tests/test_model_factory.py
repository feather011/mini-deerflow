"""统一 Model Factory 与注入链路测试（21A）。

覆盖：
- provider 选择（fake / minimax）
- minimax 缺 Key fail-closed
- create_runtime 走 create_model
- CLI 与 FastAPI 走同一条 load_model_settings → create_model 链路

全部不联网：minimax 分支注入的是真实 AnthropicCompatModel，但构造不发请求；
需要断言行为的地方用假客户端。
"""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from mini_deerflow.model_config import ModelSettings, load_model_settings
from mini_deerflow.model_errors import ModelConfigError
from mini_deerflow.model_factory import create_model
from mini_deerflow.anthropic_compat_model import AnthropicCompatModel
from mini_deerflow.models import FakeModel
from mini_deerflow.runtime import RuntimeConfig, create_runtime
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tools import ToolRegistry

KEY = "sk-UNIT-TEST-SECRET-KEY"


@contextmanager
def _temp_db():
    """临时 SQLite 路径（CLI 测试不写项目目录）。"""
    with tempfile.TemporaryDirectory() as tmp:
        yield str(Path(tmp) / "cli.db")


def _runtime(config: RuntimeConfig | None = None):
    return create_runtime(
        store=InMemoryThreadStore(),
        registry=ToolRegistry(),
        config=config or RuntimeConfig(),
    )


class TestFactory选择:
    def test_默认返回_FakeModel(self) -> None:
        assert isinstance(create_model(), FakeModel)

    def test_显式_fake_返回_FakeModel(self) -> None:
        m = create_model(ModelSettings(provider="fake"))
        assert isinstance(m, FakeModel)

    def test_minimax_带_key_返回_AnthropicCompatModel(self) -> None:
        m = create_model(
            ModelSettings(provider="minimax", api_key=KEY)
        )
        assert isinstance(m, AnthropicCompatModel)

    def test_未知_provider_抛配置错误(self) -> None:
        with pytest.raises(ModelConfigError):
            create_model(ModelSettings(provider="openai"))

    def test_无参数时读取环境_由conftest钉死为_fake(self) -> None:
        """默认 pytest 离线：环境里的 minimax 配置不应生效。"""
        assert isinstance(create_model(), FakeModel)


class TestFailClosed:
    def test_minimax_缺_key_抛_ModelConfigError(self) -> None:
        with pytest.raises(ModelConfigError) as exc:
            create_model(ModelSettings(provider="minimax"))
        assert "MINIMAX_API_KEY" in str(exc.value)

    def test_缺_key_不会降级为_FakeModel(self) -> None:
        """禁止静默降级：异常而不是返回 FakeModel。"""
        with pytest.raises(ModelConfigError):
            create_model(ModelSettings(provider="minimax"))

    def test_空字符串_key_同样失败(self) -> None:
        with pytest.raises(ModelConfigError):
            create_model(ModelSettings(provider="minimax", api_key=""))


class TestRuntime注入:
    def test_默认_runtime_使用_FakeModel(self) -> None:
        ctx = _runtime()
        try:
            assert isinstance(ctx.agent._model, FakeModel)
        finally:
            ctx.shutdown()

    def test_传入显式_model_优先于配置(self) -> None:
        sentinel = FakeModel()
        ctx = create_runtime(
            store=InMemoryThreadStore(),
            registry=ToolRegistry(),
            config=RuntimeConfig(model_settings=ModelSettings(provider="fake")),
            model=sentinel,
        )
        try:
            assert ctx.agent._model is sentinel
        finally:
            ctx.shutdown()

    def test_minimax_配置经_factory_产出_AnthropicCompatModel(self) -> None:
        ctx = _runtime(
            RuntimeConfig(
                model_settings=ModelSettings(provider="minimax", api_key=KEY)
            )
        )
        try:
            assert isinstance(ctx.agent._model, AnthropicCompatModel)
            assert KEY not in repr(ctx.agent._model)
        finally:
            ctx.shutdown()

    def test_minimax_缺_key_在_runtime启动时失败(self) -> None:
        with pytest.raises(ModelConfigError):
            _runtime(RuntimeConfig(model_settings=ModelSettings(provider="minimax")))


class TestFastAPI走统一Factory:
    def test_api_factory_读取配置并经_factory_产出_model(self, monkeypatch) -> None:
        from mini_deerflow.api import app as api_app

        sentinel = ModelSettings(provider="minimax", api_key=KEY)
        monkeypatch.setattr(api_app, "load_model_settings", lambda: sentinel)

        ctx = api_app._default_runtime_factory()
        try:
            assert isinstance(ctx.agent._model, AnthropicCompatModel)
        finally:
            ctx.shutdown()

    def test_api_settings_factory_同样读取配置(self, monkeypatch) -> None:
        from mini_deerflow.api import app as api_app
        from mini_deerflow.api.config import ApiSettings

        sentinel = ModelSettings(provider="minimax", api_key=KEY)
        monkeypatch.setattr(api_app, "load_model_settings", lambda: sentinel)

        ctx = api_app._settings_runtime_factory(ApiSettings())
        try:
            assert isinstance(ctx.agent._model, AnthropicCompatModel)
        finally:
            ctx.shutdown()

    def test_默认配置下_api_factory_产出_FakeModel(self) -> None:
        from mini_deerflow.api import app as api_app

        ctx = api_app._default_runtime_factory()
        try:
            assert isinstance(ctx.agent._model, FakeModel)
        finally:
            ctx.shutdown()


class TestCLI走统一Factory:
    def test_cli_把_load_model_settings_结果注入_runtime(self, monkeypatch) -> None:
        import mini_deerflow.cli as cli

        sentinel = ModelSettings(provider="minimax", api_key=KEY)
        monkeypatch.setattr(cli, "load_model_settings", lambda: sentinel)

        captured: dict = {}
        real_create_runtime = cli.create_runtime

        def spy(**kwargs):
            captured.update(kwargs)
            return real_create_runtime(**kwargs)

        monkeypatch.setattr(cli, "create_runtime", spy)

        with _temp_db() as db_path, \
                patch("builtins.input", side_effect=["exit"]), \
                patch("builtins.print"):
            cli.main(argv=["--db", db_path])

        assert "config" in captured
        assert captured["config"].model_settings is sentinel

    def test_cli_默认注入_fake_配置(self, monkeypatch) -> None:
        import mini_deerflow.cli as cli

        captured: dict = {}
        real_create_runtime = cli.create_runtime

        def spy(**kwargs):
            captured.update(kwargs)
            return real_create_runtime(**kwargs)

        monkeypatch.setattr(cli, "create_runtime", spy)

        with _temp_db() as db_path, \
                patch("builtins.input", side_effect=["exit"]), \
                patch("builtins.print"):
            cli.main(argv=["--db", db_path])

        settings = captured["config"].model_settings
        assert settings.provider == "fake"
        assert isinstance(create_model(settings), FakeModel)


class Test配置加载接口:
    def test_load_model_settings_可用于_runtime(self) -> None:
        """conftest 已钉死 fake，因此这条断言等价于「默认配置可用」。"""
        settings = load_model_settings()
        assert settings.provider == "fake"
        assert isinstance(create_model(settings), FakeModel)

class Test三者共用配置入口:
    """smoke / CLI / FastAPI 必须走同一个 load_model_settings。"""

    @staticmethod
    def _entrypoints():
        import mini_deerflow.mimo_smoke_test as smoke
        import mini_deerflow.cli as cli
        from mini_deerflow.api import app as api_app

        return smoke, cli, api_app

    def test_三者引用同一个函数对象(self) -> None:
        smoke, cli, api_app = self._entrypoints()
        targets = {
            smoke.load_model_settings,
            cli.load_model_settings,
            api_app.load_model_settings,
        }
        assert len(targets) == 1, "smoke / CLI / API 必须共用同一配置入口"

    def test_smoke_通过配置入口取配置并按其结果决策(self, monkeypatch) -> None:
        smoke, _, _ = self._entrypoints()
        seen: list = []

        def fake_load(*args, **kwargs):
            seen.append((args, kwargs))
            return ModelSettings(provider="fake")

        monkeypatch.setattr(smoke, "load_model_settings", fake_load)

        # provider=fake → 跳过，不联网、不消耗额度
        assert smoke.main() == 2
        assert seen, "smoke 必须调用 load_model_settings"

    def test_smoke_不自行加载或覆盖环境变量(self) -> None:
        import inspect

        smoke, _, _ = self._entrypoints()
        src = inspect.getsource(smoke)
        assert "load_dotenv" not in src, "smoke 不得自行加载 .env"
        assert "override=True" not in src, "smoke 不得覆盖环境变量"
        assert "os.environ" not in src, "smoke 不得直接读环境变量"
        assert not hasattr(smoke, "load_dotenv")


class TestMimoFactory路径:
    def test_mimo_带_key_返回_AnthropicCompatModel(self) -> None:
        m = create_model(ModelSettings(provider="mimo", api_key=KEY))
        assert isinstance(m, AnthropicCompatModel)
        assert m.model_name == "mimo-v2.6-flash"

    def test_mimo_缺_key_抛配置错误(self) -> None:
        with pytest.raises(ModelConfigError):
            create_model(ModelSettings(provider="mimo"))

    def test_mimo_配置经_runtime_产出_AnthropicCompatModel(self) -> None:
        ctx = _runtime(
            RuntimeConfig(model_settings=ModelSettings(provider="mimo", api_key=KEY))
        )
        try:
            assert type(ctx.agent._model).__name__ == "AnthropicCompatModel"
            assert KEY not in repr(ctx.agent._model)
        finally:
            ctx.shutdown()

    def test_mimo_不读取_minimax_key(self) -> None:
        with pytest.raises(ModelConfigError):
            create_model(ModelSettings(provider="mimo", api_key=None))
