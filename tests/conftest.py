"""pytest 全局夹具（21A）。"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _pin_fake_model_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认整个测试套件必须离线。

    CLI / FastAPI / smoke 的启动路径都调用 load_model_settings()，
    该函数会读真实进程环境并加载 .env。若开发机上恰好设置了
    MINI_DEERFLOW_MODEL_PROVIDER=minimax，测试就可能真的发起网络请求。

    这里统一钉死为 fake——create_model() 见到 fake 必然返回 FakeModel，
    从而在唯一出口上保证零网络。

    需要验证 minimax 行为的测试直接构造 ModelSettings(provider="minimax")
    并注入假客户端，不依赖环境变量；需要验证配置优先级的测试会自行
    delenv 放开该变量，以便走真实的 load_dotenv 路径。
    """
    monkeypatch.setenv("MINI_DEERFLOW_MODEL_PROVIDER", "fake")
    # 23：Thread 持久化默认必须是内存实现。若开发机/容器里设了
    # MINI_DEERFLOW_DB_PATH，用例之间会共享同一个 SQLite 文件，
    # 出现跨用例串数据与"上个用例的 Thread 还在"这类假失败。
    monkeypatch.delenv("MINI_DEERFLOW_DB_PATH", raising=False)
    # 24：同理钉死 ContextState 的持久化。共享文件会让摘要、Task Notes、
    # 压缩水位跨用例残留，制造出与真实行为无关的假失败。
    monkeypatch.delenv("MINI_DEERFLOW_CONTEXT_DB_PATH", raising=False)
    monkeypatch.delenv("MINI_DEERFLOW_CONTEXT_ENABLED", raising=False)