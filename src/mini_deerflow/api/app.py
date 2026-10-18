"""FastAPI 应用工厂与 Uvicorn 入口（19A 阶段，19F 统一收口）。

职责：
- 构造 FastAPI 实例
- 通过 lifespan 启动/关闭 RuntimeContext
- 全局统一错误响应（所有 HTTP 状态码）
- Request ID 中间件
- CORS 中间件
- OpenAPI tags 整理
- 提供 runtime_factory 注入点
"""
from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Callable

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

# 加载 .env：ApiSettings 只读 os.environ，不自己调 load_dotenv。
# 显式给绝对路径——app.py 在 src/mini_deerflow/api/，向上四级到仓库根，
# 保证 cwd 在哪都能加载到。
load_dotenv(Path(__file__).parent.parent.parent.parent / ".env")

from mini_deerflow.api.config import ApiSettings
from mini_deerflow.api.middleware.request_id import RequestIdMiddleware
from mini_deerflow.api.routes.health import router as health_router
from mini_deerflow.api.routes.runs import router as runs_router
from mini_deerflow.api.routes.runs_stream import router as runs_stream_router
from mini_deerflow.api.routes.threads import router as threads_router
from mini_deerflow.api.routes.files import router as files_router
from mini_deerflow.api.routes.context_usage import router as context_usage_router
from mini_deerflow.api.routes.projects import router as projects_router
from mini_deerflow.api.routes.mcp import router as mcp_router
from mini_deerflow.api.routes.confirmations import router as confirmations_router
from mini_deerflow.api.schemas.errors import (
    ErrorCode,
    build_error_response,
    safe_message,
)
from mini_deerflow.mcp_config import load_mcp_config
from mini_deerflow.model_config import load_model_settings
from mini_deerflow.runtime import RuntimeConfig, create_runtime, load_context_config
from mini_deerflow.thread_store import InMemoryThreadStore, SQLiteThreadStore
from mini_deerflow.project_store import InMemoryProjectStore, SQLiteProjectStore
from mini_deerflow.tools import create_default_registry
from mini_deerflow.web_tools import HostPolicy as WebHostPolicy


_LOGGER = logging.getLogger(__name__)


RuntimeFactory = Callable[[], "RuntimeContext"]


def _build_store(settings: ApiSettings):
    """按配置选择 ThreadStore（23 生产持久化）。

    - `MINI_DEERFLOW_DB_PATH` 未设置 → 内存实现（默认行为不变，测试零副作用）
    - 设置了 → SQLite 持久化，容器/本机重启后 Thread 与消息仍在

    放在 lifespan 内的工厂里执行，因此 `create_app()` 与模块 import
    阶段既不建数据库文件也不碰文件系统。
    """
    if not settings.db_path:
        return InMemoryThreadStore()

    # SQLite 不会自动建父目录；卷挂载点之外的路径要自己保证存在
    Path(settings.db_path).expanduser().resolve().parent.mkdir(
        parents=True, exist_ok=True
    )
    _LOGGER.info("ThreadStore = SQLite (%s)", settings.db_path)
    return SQLiteThreadStore(settings.db_path)


def _build_project_store(settings: ApiSettings):
    """按配置选择 ProjectStore；与 ThreadStore 用同一个数据库文件。

    项目与线程本来就是一对多，共享一个 SQLite 文件省掉一处连接管理，
    也让「删项目级联删会话」在同一个事务边界内可见。
    """
    if not settings.db_path:
        return InMemoryProjectStore()
    _LOGGER.info("ProjectStore = SQLite (%s)", settings.db_path)
    return SQLiteProjectStore(settings.db_path)


def build_web_host_policy(settings: ApiSettings) -> WebHostPolicy:
    """根据 ApiSettings 构造 SSRF 防护策略。

    - 默认拒绝 loopback / 私有网络 / IP 字面量
    - 显式白名单（精确匹配，lowercase）
    - `WEB_ALLOW_LOOPBACK=1` 时放行 loopback（仅 CI/测试用）
    """
    return WebHostPolicy(
        allow_loopback=bool(settings.web_allow_loopback),
        allow_private_networks=False,
        allow_ip_literal=False,
        allowed_hosts=frozenset(h.lower() for h in settings.web_allowed_hosts),
    )


def _default_runtime_factory(
    settings: ApiSettings | None = None,
) -> "RuntimeContext":
    # 与 CLI 一致注入默认工具（create_default_registry）。
    # 裸 ToolRegistry() 是空表，会导致任何 ToolCall 都以 ToolNotFoundError 失败。
    #
    # model_settings 在此处（lifespan 内）才读取环境：create_app() 与 import
    # 阶段既不读配置也不创建客户端，更不发网络请求。
    resolved = settings or ApiSettings()
    ws_root = resolved.workspaces_root
    return create_runtime(
        store=_build_store(resolved),
        project_store=_build_project_store(resolved),
        registry=create_default_registry(),
        config=RuntimeConfig(
            workspace_root=resolved.workspace_root,
            workspace_max_upload_bytes=resolved.max_upload_bytes,
            workspace_max_list_entries=resolved.max_list_entries,
            model_settings=load_model_settings(),
            mcp_servers=load_mcp_config(),
            harness_root=resolved.harness_root,
            harness_timeout_seconds=resolved.harness_timeout_seconds,
            disable_budget=True,
            # TOOL-06：网络只读配置
            web_read_timeout_s=resolved.web_read_timeout_s,
            web_host_policy=build_web_host_policy(resolved),
            **load_context_config(),
        ),
        workspaces_root=ws_root,
    )


def _settings_runtime_factory(settings: ApiSettings) -> "RuntimeContext":
    return _default_runtime_factory(settings)


def _build_factory(
    settings: ApiSettings | None,
    runtime_factory: RuntimeFactory | None,
) -> RuntimeFactory:
    if runtime_factory:
        return runtime_factory
    if settings:
        return lambda: _settings_runtime_factory(settings)
    return _default_runtime_factory


def _add_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器，统一返回 ErrorResponse 格式。"""

    def _get_request_id(request: Request) -> str:
        return getattr(request.state, "request_id", "unknown")

    # ---- HTTPException / StarletteHTTPException ----
    @app.exception_handler(StarletteHTTPException)
    async def starlette_http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """处理所有 HTTP 异常，包括 FastAPI raise HTTPException。"""
        code = ErrorCode.from_status(exc.status_code)
        message = safe_message(exc.detail, f"Error {exc.status_code}")
        request_id = _get_request_id(request)
        body = build_error_response(code, message, request_id)
        return JSONResponse(status_code=exc.status_code, content=body)

    # ---- RequestValidationError（422）----
    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        request_id = _get_request_id(request)
        details: dict = {}
        for err in exc.errors():
            loc = ".".join(str(l) for l in err["loc"])
            details[loc] = err["type"]
        body = build_error_response(
            ErrorCode.UNPROCESSABLE_ENTITY,
            "请求参数校验失败",
            request_id,
            details=details if details else None,
        )
        return JSONResponse(status_code=422, content=body)

    # ---- 未处理的 500 ----
    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        request_id = _get_request_id(request)
        _LOGGER.exception("Unhandled exception request_id=%s", request_id)
        body = build_error_response(
            ErrorCode.INTERNAL_ERROR,
            "Internal server error",
            request_id,
        )
        # 直接在 handler 中写 header：ServerErrorMiddleware 会直接发送响应，
        # 不会回落到 RequestIdMiddleware 的 response.headers 写入逻辑。
        return JSONResponse(
            status_code=500,
            content=body,
            headers={"X-Request-ID": request_id},
        )


def _add_cors(app: FastAPI, settings: ApiSettings) -> None:
    """配置 CORS 中间件。"""
    if not settings.cors_origins:
        return

    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=[
            "Content-Type",
            "Accept",
            "X-Request-ID",
            "Authorization",
        ],
        expose_headers=["X-Request-ID", "X-Run-ID"],
    )


def create_app(
    settings: ApiSettings | None = None,
    runtime_factory: RuntimeFactory | None = None,
) -> FastAPI:
    resolved_settings = settings or ApiSettings()
    factory = _build_factory(resolved_settings, runtime_factory)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.runtime_factory = factory
        runtime = factory()
        app.state.runtime = runtime
        _LOGGER.info("Runtime started")
        try:
            yield
        finally:
            runtime_obj = getattr(app.state, "runtime", None)
            if runtime_obj is None:
                _LOGGER.warning("No runtime to shutdown")
            else:
                shutdown_error: Exception | None = None
                try:
                    runtime_obj.shutdown()
                except Exception as exc:
                    _LOGGER.exception("Runtime shutdown failed")
                    shutdown_error = exc
                finally:
                    if hasattr(app.state, "runtime"):
                        del app.state.runtime
                if shutdown_error is not None:
                    raise shutdown_error
                _LOGGER.info("Runtime stopped")

    app = FastAPI(
        title=resolved_settings.title,
        version=resolved_settings.version,
        lifespan=lifespan,
    )

    # Starlette 后注册的中间件位于外层。
    # 先注册 CORS，再注册 RequestId，
    # 因此 RequestId 能覆盖包括 CORS preflight 在内的所有响应。
    _add_cors(app, resolved_settings)
    app.add_middleware(RequestIdMiddleware)

    # 全局异常处理
    _add_exception_handlers(app)

    # 路由
    app.include_router(
        health_router,
        prefix=resolved_settings.api_prefix,
    )
    app.include_router(
        threads_router,
        prefix=resolved_settings.api_prefix,
    )
    app.include_router(
        runs_router,
        prefix=resolved_settings.api_prefix,
    )
    app.include_router(
        runs_stream_router,
        prefix=resolved_settings.api_prefix,
    )
    app.include_router(
        files_router,
        prefix=resolved_settings.api_prefix,
    )
    app.include_router(
        context_usage_router,
        prefix=resolved_settings.api_prefix,
    )
    app.include_router(
        projects_router,
        prefix=resolved_settings.api_prefix,
    )
    app.include_router(
        mcp_router,
        prefix=resolved_settings.api_prefix,
    )
    app.include_router(
        confirmations_router,
        prefix=resolved_settings.api_prefix,
    )

    return app


# Uvicorn 入口
app = create_app()
