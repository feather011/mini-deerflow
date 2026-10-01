from dataclasses import FrozenInstanceError

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from mini_deerflow.api.app import create_app
from mini_deerflow.api.config import ApiSettings


def test_health_endpoint() -> None:
    app = create_app()
    client = TestClient(app)
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "mini-deerflow",
    }


def test_custom_api_prefix() -> None:
    app = create_app(ApiSettings(api_prefix="/v1"))
    client = TestClient(app)
    assert client.get("/v1/health").status_code == 200
    assert client.get("/api/health").status_code == 404


# ---------- 边界场景 ----------

def test_health_response_content_type_is_json() -> None:
    client = TestClient(create_app())
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")


def test_health_rejects_non_get_methods() -> None:
    client = TestClient(create_app())
    for method in ("post", "put", "patch", "delete"):
        response = getattr(client, method)("/api/health")
        assert response.status_code == 405, f"{method.upper()} 应返回 405"


def test_unknown_path_returns_404() -> None:
    client = TestClient(create_app())
    response = client.get("/api/unknown")
    assert response.status_code == 404


def test_health_response_matches_pydantic_schema() -> None:
    """响应字段必须符合 HealthResponse 约束：status 只能是 'ok'。"""
    client = TestClient(create_app())
    body = client.get("/api/health").json()
    # 直接构造 HealthResponse，验证 schema 兼容
    from mini_deerflow.api.routes.health import HealthResponse
    parsed = HealthResponse.model_validate(body)
    assert parsed.status == "ok"
    assert parsed.service == "mini-deerflow"


def test_health_response_rejects_invalid_status() -> None:
    """HealthResponse 必须拒绝非 'ok' 的 status 值。"""
    from mini_deerflow.api.routes.health import HealthResponse
    with pytest.raises(ValidationError):
        HealthResponse(status="down", service="x")  # type: ignore[arg-type]


# ---------- OpenAPI schema ----------

def test_openapi_includes_health_endpoint() -> None:
    client = TestClient(create_app())
    schema = client.get("/openapi.json").json()
    assert "/api/health" in schema["paths"]
    assert "get" in schema["paths"]["/api/health"]


def test_openapi_groups_health_under_tag() -> None:
    client = TestClient(create_app())
    schema = client.get("/openapi.json").json()
    op = schema["paths"]["/api/health"]["get"]
    assert op["tags"] == ["health"]


def test_openapi_uses_settings_title_and_version() -> None:
    settings = ApiSettings(title="Custom Title", version="9.9.9")
    client = TestClient(create_app(settings))
    schema = client.get("/openapi.json").json()["info"]
    assert schema["title"] == "Custom Title"
    assert schema["version"] == "9.9.9"


# ---------- Settings 不可变性 ----------

def test_api_settings_is_immutable() -> None:
    settings = ApiSettings()
    with pytest.raises(FrozenInstanceError):
        settings.api_prefix = "/v2"  # type: ignore[misc]


def test_api_settings_default_values() -> None:
    settings = ApiSettings()
    assert settings.title == "Mini DeerFlow API"
    assert settings.version == "0.1.0"
    assert settings.api_prefix == "/api"


# ---------- App 工厂隔离 ----------

def test_multiple_app_instances_are_isolated() -> None:
    app_a = create_app(ApiSettings(api_prefix="/a"))
    app_b = create_app(ApiSettings(api_prefix="/b"))
    client_a = TestClient(app_a)
    client_b = TestClient(app_b)
    assert client_a.get("/a/health").status_code == 200
    assert client_a.get("/b/health").status_code == 404
    assert client_b.get("/b/health").status_code == 200
    assert client_b.get("/a/health").status_code == 404


def test_module_level_app_uses_default_settings() -> None:
    """mini_deerflow.api.app.app 是默认设置的实例（uvicorn 入口）。"""
    from mini_deerflow.api.app import app as module_app
    client = TestClient(module_app)
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# ---------- 边界：不得初始化外部资源 ----------

def test_api_module_import_does_not_create_runtime() -> None:
    """import mini_deerflow.api.* 不得触发 Runtime 实例创建或重资源副作用。

    验证手段：导入前后对比 sys.modules 中的 RuntimeContext 实例数量，
    以及 mini_deerflow.runtime.RuntimeContext 没有产生任何实例。
    """
    import gc
    from mini_deerflow.runtime import RuntimeContext

    # 先记下当前 RuntimeContext 实例数
    gc.collect()
    before = sum(
        1 for obj in gc.get_objects() if isinstance(obj, RuntimeContext)
    )

    # 触发导入（每个模块必须真的被加载过才算覆盖）
    import mini_deerflow.api.app  # noqa: F401
    import mini_deerflow.api.config  # noqa: F401
    import mini_deerflow.api.dependencies  # noqa: F401
    import mini_deerflow.api.routes.health  # noqa: F401

    gc.collect()
    after = sum(
        1 for obj in gc.get_objects() if isinstance(obj, RuntimeContext)
    )

    assert after == before, (
        f"import 后 RuntimeContext 实例数从 {before} 涨到 {after}（应为 0）"
    )


def test_api_module_import_does_not_initialize_side_effects() -> None:
    """导入 api 子包不得触发 Docker 连接、SQLite 文件、模型凭据读取等副作用。

    通过 AST 扫描排除明显会拉起副作用的模块（sandbox_docker / memory 持久化层）。
    类型引用（runtime.RuntimeContext / thread_store.InMemoryThreadStore）允许，
    因为这些只是 dataclass / 类对象，本身没有副作用。
    """
    import ast
    from pathlib import Path

    api_root = Path("src/mini_deerflow/api").resolve()
    # 这些模块在 import 阶段就会产生副作用（Docker / SQLite / 模型凭据）
    forbidden_side_effect_modules = {
        "mini_deerflow.sandbox_docker",
        "mini_deerflow.memory_store_sqlite",
        "mini_deerflow.models",  # 可能读取 anthropic 凭据
    }
    violations: list[tuple[str, str]] = []
    for py in api_root.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            mod: str | None = None
            if isinstance(node, ast.Import):
                mod = node.names[0].name
            elif isinstance(node, ast.ImportFrom):
                mod = node.module
            if mod and (mod in forbidden_side_effect_modules or any(
                mod.startswith(f + ".") for f in forbidden_side_effect_modules
            )):
                violations.append((str(py), mod))
    assert not violations, (
        f"api 子包不得 import 副作用模块，违规：{violations}"
    )


def test_create_app_does_not_hold_external_resources() -> None:
    """create_app() 返回的 FastAPI app 不持有 Runtime / Manager / Model 实例。"""
    app = create_app()
    # FastAPI 自身的 state 仅有 starlette 默认属性，不应有 RuntimeContext
    # （lifespan 未启动，state 应为空）
    assert len(app.state) == 0
    # 没有 agent / runtime / model 等关键属性（避免误把资源塞进 App）
    for forbidden in ("agent", "runtime", "model", "manager", "store"):
        assert not hasattr(app, forbidden) or getattr(app, forbidden) is None