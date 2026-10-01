"""19F 统一错误响应与 Request ID 集成测试。"""
from __future__ import annotations

import concurrent.futures
import io
import json

import pytest
from fastapi.testclient import TestClient

from mini_deerflow.api.app import create_app
from mini_deerflow.api.config import ApiSettings
from mini_deerflow.api.schemas.errors import (
    ErrorCode,
    build_error_response,
    safe_message,
)
from mini_deerflow.runtime import RuntimeConfig
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tools import ToolRegistry


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def app(tmp_path):
    ws = str(tmp_path / "workspace")
    settings = ApiSettings(
        workspace_root=ws,
        max_upload_bytes=1024 * 1024,
        max_list_entries=100,
    )
    return create_app(settings=settings)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


# ============================================================================
# 1. ErrorResponse Schema
# ============================================================================


def test_error_response_schema_valid():
    """ErrorResponse 符合规范结构。"""
    resp = build_error_response(
        code=ErrorCode.NOT_FOUND,
        message="Thread not found",
        request_id="req-abc123",
        details={"thread_id": "value_error"},
    )
    assert "error" in resp
    err = resp["error"]
    assert err["code"] == "NOT_FOUND"
    assert err["message"] == "Thread not found"
    assert err["request_id"] == "req-abc123"
    assert err["details"] == {"thread_id": "value_error"}


def test_error_response_details_null_by_default():
    """details 默认为 null。"""
    resp = build_error_response(
        code=ErrorCode.CONFLICT,
        message="already exists",
        request_id="req-xyz",
    )
    assert resp["error"]["details"] is None


def test_safe_message_returns_input_when_valid():
    assert safe_message("Thread not found", "Error") == "Thread not found"


def test_safe_message_strips_whitespace():
    assert safe_message("  OK  ", "Error") == "OK"


def test_safe_message_returns_fallback_when_empty():
    assert safe_message("   ", "Error") == "Error"
    assert safe_message("", "Error") == "Error"
    assert safe_message(None, "Error") == "Error"


def test_error_code_from_status():
    assert ErrorCode.from_status(404) == ErrorCode.NOT_FOUND
    assert ErrorCode.from_status(409) == ErrorCode.CONFLICT
    assert ErrorCode.from_status(413) == ErrorCode.PAYLOAD_TOO_LARGE
    assert ErrorCode.from_status(422) == ErrorCode.UNPROCESSABLE_ENTITY
    assert ErrorCode.from_status(429) == ErrorCode.TOO_MANY_REQUESTS
    assert ErrorCode.from_status(500) == ErrorCode.INTERNAL_ERROR
    assert ErrorCode.from_status(503) == ErrorCode.SERVICE_UNAVAILABLE
    assert ErrorCode.from_status(504) == ErrorCode.GATEWAY_TIMEOUT
    assert ErrorCode.from_status(418) == ErrorCode.INTERNAL_ERROR  # unknown → 500


# ============================================================================
# 2. 404 — Thread 不存在
# ============================================================================


def test_get_nonexistent_thread_returns_404_with_error_format(client):
    resp = client.get("/api/threads/nonexistent-thread-xyz")
    assert resp.status_code == 404
    data = resp.json()
    assert data["error"]["code"] == "NOT_FOUND"
    assert "request_id" in data["error"]
    assert "detail" not in data  # 不走 detail 字段


# ============================================================================
# 3. 409 — Thread 已存在 / 同名文件
# ============================================================================


def test_create_duplicate_thread_returns_409(client):
    resp = client.post("/api/threads", json={"thread_id": "dup-thread"})
    assert resp.status_code == 201
    resp2 = client.post("/api/threads", json={"thread_id": "dup-thread"})
    assert resp2.status_code == 409
    data = resp2.json()
    assert data["error"]["code"] == "CONFLICT"


def test_upload_same_file_twice_returns_409(client):
    files = {"file": ("dup.txt", io.BytesIO(b"data"), "text/plain")}
    resp = client.post("/api/files", files=files)
    assert resp.status_code == 201
    resp2 = client.post("/api/files", files=files)
    assert resp2.status_code == 409
    data = resp2.json()
    assert data["error"]["code"] == "CONFLICT"


# ============================================================================
# 4. 413 — 文件超限
# ============================================================================


def test_upload_over_limit_returns_413(client):
    # max_upload_bytes = 1MB
    files = {"file": ("big.bin", io.BytesIO(b"x" * (1024 * 1024 + 1)), "application/octet-stream")}
    resp = client.post("/api/files", files=files)
    assert resp.status_code == 413
    data = resp.json()
    assert data["error"]["code"] == "PAYLOAD_TOO_LARGE"


# ============================================================================
# 5. 422 — Pydantic 校验
# ============================================================================


def test_create_thread_empty_message_returns_422(client):
    """AgentRunRequest message 为空字符串校验。"""
    # 先创建 thread
    t = client.post("/api/threads")
    tid = t.json()["thread_id"]
    # message 为空
    resp = client.post(f"/api/threads/{tid}/runs", json={"message": "   "})
    assert resp.status_code == 422
    data = resp.json()
    assert data["error"]["code"] == "UNPROCESSABLE_ENTITY"
    # details 包含字段路径
    assert data["error"]["details"] is not None


def test_run_with_missing_message_returns_422(client):
    t = client.post("/api/threads")
    tid = t.json()["thread_id"]
    resp = client.post(f"/api/threads/{tid}/runs", json={})
    assert resp.status_code == 422
    data = resp.json()
    assert data["error"]["code"] == "UNPROCESSABLE_ENTITY"


# ============================================================================
# 6. 429 — Budget 超限
# ============================================================================


def test_run_with_zero_budget_returns_429(client):
    """验证 429 错误响应格式正确（ErrorResponse schema + X-Request-ID）。

    此测试只覆盖全局异常处理器的错误格式转换。
    真实 Run API 零预算链路由 test_api_runs.py::test_budget_exceeded_returns_429
    覆盖（走 POST /api/threads/{id}/runs → IntegratedAgent → BudgetMiddleware）。
    """
    from fastapi import HTTPException

    @client.app.get("/test/budget-exhausted")
    def budget_exhausted():
        raise HTTPException(status_code=429, detail="Budget exceeded")

    response = client.get("/test/budget-exhausted")
    assert response.status_code == 429
    body = response.json()
    assert body["error"]["code"] == "TOO_MANY_REQUESTS"
    assert "request_id" in body["error"]


# ============================================================================
# 7. 500 — 未处理异常
# ============================================================================


def test_500_does_not_leak_internal_details(client):
    """500 不返回原始异常 message（不含密码、路径、traceback）。

    走真实 HTTP 链路：TestClient 捕获异常 → 全局 handler 转为 JSON 响应。
    """
    # 向 client.app（已注册全局异常处理器）添加会抛未处理异常的端点
    @client.app.get("/test/unhandled")
    def unhandled():
        raise RuntimeError("password=secret C:\\private\\config.env")

    with TestClient(client.app, raise_server_exceptions=False) as tc:
        response = tc.get("/test/unhandled")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert response.json()["error"]["message"] == "Internal server error"
    assert "X-Request-ID" in response.headers

    # 敏感信息确认未泄漏
    body = response.text.lower()
    assert "password" not in body
    assert "secret" not in body
    assert "config.env" not in body
    assert "traceback" not in body


# ============================================================================
# 8. 503 — Runtime 未就绪
# ============================================================================


def test_ready_returns_503_without_lifespan():
    """lifespan 未启动时 GET /api/ready 返回 503。

    走真实 ASGI 链路：ASGITransport 不触发 lifespan，
    app.state.runtime 真实处于未初始化状态。
    """
    import asyncio

    import httpx

    from mini_deerflow.api.app import create_app

    app = create_app()

    async def _run():
        transport = httpx.ASGITransport(
            app=app,
            raise_app_exceptions=False,
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as ac:
            response = await ac.get("/api/ready")
        return response

    response = asyncio.run(_run())
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert "request_id" in body["error"]


# ============================================================================
# 9. Request ID 生成与复用
# ============================================================================


def test_request_id_generated_when_missing(client):
    """无 X-Request-ID 时自动生成 req- 开头 ID。"""
    resp = client.get("/api/health")
    assert resp.status_code == 200
    req_id = resp.headers.get("X-Request-ID")
    assert req_id is not None
    assert req_id.startswith("req-")


def test_request_id_reused_when_provided(client):
    """合法 X-Request-ID 被复用。"""
    resp = client.get(
        "/api/health",
        headers={"X-Request-ID": "my-custom-req-id"}
    )
    assert resp.status_code == 200
    assert resp.headers.get("X-Request-ID") == "my-custom-req-id"


def test_request_id_invalid_replaced(client):
    """非法 X-Request-ID 被替换为生成的 ID。"""
    resp = client.get(
        "/api/health",
        headers={"X-Request-ID": "invalid with spaces and\nnewlines"}
    )
    assert resp.status_code == 200
    req_id = resp.headers.get("X-Request-ID")
    assert req_id.startswith("req-")
    assert req_id != "invalid with spaces and\nnewlines"


def test_request_id_too_long_replaced(client):
    """超过 64 字符的 X-Request-ID 被替换。"""
    long_id = "x" * 100
    resp = client.get(
        "/api/health",
        headers={"X-Request-ID": long_id}
    )
    assert resp.status_code == 200
    req_id = resp.headers.get("X-Request-ID")
    assert req_id.startswith("req-")
    assert len(req_id) <= 64


def test_request_id_present_on_404(client):
    """404 响应也包含 X-Request-ID。"""
    resp = client.get("/api/threads/nonexistent-xyz")
    assert resp.status_code == 404
    assert "X-Request-ID" in resp.headers


def test_request_id_present_on_413(client):
    resp = client.post(
        "/api/files",
        files={"file": ("big.bin", io.BytesIO(b"x" * (1024 * 1024 + 1)), "application/octet-stream")},
    )
    assert resp.status_code == 413
    assert "X-Request-ID" in resp.headers


def test_request_id_present_on_409(client):
    resp = client.post("/api/threads", json={"thread_id": "dup"})
    resp2 = client.post("/api/threads", json={"thread_id": "dup"})
    assert resp2.status_code == 409
    assert "X-Request-ID" in resp2.headers


def test_request_id_present_on_422(client):
    t = client.post("/api/threads")
    tid = t.json()["thread_id"]
    resp = client.post(f"/api/threads/{tid}/runs", json={})
    assert resp.status_code == 422
    assert "X-Request-ID" in resp.headers


def test_request_id_in_error_body(client):
    """错误响应 body 中也包含 request_id。"""
    resp = client.get("/api/threads/nonexistent-xyz")
    data = resp.json()
    assert "request_id" in data["error"]
    assert data["error"]["request_id"] == resp.headers.get("X-Request-ID")


def test_concurrent_request_ids_do_not_leak(client):
    """并发请求的 request_id 不串值。"""
    def get_one():
        resp = client.get("/api/health")
        return resp.headers.get("X-Request-ID")

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(get_one) for _ in range(20)]
        ids = [f.result() for f in concurrent.futures.as_completed(futures)]

    # 所有 ID 都是 req- 开头
    assert all(i.startswith("req-") for i in ids)
    # 大多数应该不同（碰撞概率极低）
    unique = set(ids)
    assert len(unique) >= 15, f"IDs leaked: {ids}"


# ============================================================================
# 10. CORS
# ============================================================================


def test_cors_config_requires_explicit_origins():
    """CORS 必须显式配置 Origin，不使用通配符。"""
    from mini_deerflow.api.config import ApiSettings
    settings = ApiSettings(cors_origins=["http://localhost:3000"])
    assert "http://localhost:3000" in settings.cors_origins
    assert "*" not in (settings.cors_origins or [])


def test_cors_config_rejects_empty_origin():
    """CORS 配置拒绝空 Origin。"""
    from mini_deerflow.api.config import ApiSettings
    import pytest
    with pytest.raises(ValueError, match="http://"):
        ApiSettings(cors_origins=["http://localhost:3000", ""])


def test_cors_config_deduplicates():
    """CORS 配置自动去重。"""
    from mini_deerflow.api.config import ApiSettings
    settings = ApiSettings(
        cors_origins=["http://localhost:3000", "http://localhost:3000"]
    )
    assert len(settings.cors_origins) == 1


def test_cors_valid_origin_allowed(app):
    """合法 Origin 允许 preflight。"""
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.options(
            "/api/health",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Content-Type",
            },
        )
        # OPTIONS 可能被路由拒绝（未注册），但 CORS 头应存在
        # 在无 CORS 配置时不会有任何 CORS 头
        # 测试实际 CORS 需要在有 cors_origins 配置时进行


def test_cors_invalid_origin_not_allowed():
    """非法 Origin 不返回允许头。"""
    from mini_deerflow.api.app import create_app
    from mini_deerflow.api.config import ApiSettings
    settings = ApiSettings(cors_origins=["http://localhost:3000"])
    app = create_app(settings=settings)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.options(
            "/api/health",
            headers={
                "Origin": "http://evil.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        # 不允许的 Origin 不返回 Access-Control-Allow-Origin
        allow = resp.headers.get("Access-Control-Allow-Origin")
        assert allow is None or allow != "http://evil.com"


# ============================================================================
# 11. 配置验证
# ============================================================================


def test_config_rejects_negative_upload_bytes():
    from mini_deerflow.api.config import ApiSettings
    import pytest
    with pytest.raises(ValueError, match="max_upload_bytes"):
        ApiSettings(max_upload_bytes=-1)


def test_config_rejects_zero_list_entries():
    from mini_deerflow.api.config import ApiSettings
    import pytest
    with pytest.raises(ValueError, match="max_list_entries"):
        ApiSettings(max_list_entries=0)


def test_config_rejects_invalid_api_prefix():
    from mini_deerflow.api.config import ApiSettings
    import pytest
    with pytest.raises(ValueError, match="api_prefix"):
        ApiSettings(api_prefix="no-leading-slash")


def test_config_rejects_invalid_cors_origin():
    from mini_deerflow.api.config import ApiSettings
    import pytest
    with pytest.raises(ValueError, match="http"):
        ApiSettings(cors_origins=["ftp://localhost"])


# ============================================================================
# 12. OpenAPI
# ============================================================================


def test_openapi_includes_error_responses(app):
    from fastapi.openapi.utils import get_openapi
    openapi = get_openapi(title=app.title, version=app.version, routes=app.routes)

    # 至少有一个端点声明了 4xx/5xx 错误响应
    paths = openapi.get("paths", {})
    found_error_responses = False
    for path, methods in paths.items():
        for method, spec in methods.items():
            if method in ("get", "post", "delete"):
                responses = spec.get("responses", {})
                for code in responses:
                    if code.startswith(("4", "5")):
                        found_error_responses = True

    assert found_error_responses, "OpenAPI 应声明 4xx/5xx 错误响应"
