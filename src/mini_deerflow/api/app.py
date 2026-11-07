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
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
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
from mini_deerflow.model_config import PROVIDER_VAR, load_model_settings
from mini_deerflow.model_errors import ModelConfigError
from mini_deerflow.runtime import RuntimeConfig, create_runtime, load_context_config
from mini_deerflow.thread_store import InMemoryThreadStore, SQLiteThreadStore
from mini_deerflow.project_store import InMemoryProjectStore, SQLiteProjectStore
from mini_deerflow.tools import create_default_registry
from mini_deerflow.web_tools import (
    HostPolicy as WebHostPolicy,
    WebToolConfig,
    build_web_search_provider_from_config,
)


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


def _require_configured_model_provider() -> None:
    """正常模式必须显式配置模型 provider，否则明确报错。

    `MINI_DEERFLOW_MODEL_PROVIDER` 的**代码默认值是 fake**，而 FakeModel
    只会重复固定文案。配置缺失时若放行，界面看起来"能聊天"，实际上
    完全不具备正常对话能力 —— 这正是人工验收时看到的现象。
    这里宁可启动失败，也不让正常模式悄悄退回固定回复。

    拦的是**完全没配置**，不是 `provider=fake`：测试套件
    （`tests/conftest.py` 的 autouse fixture）与容器部署验收
    （`scripts/compose_persistence_check.py`）都**显式**写 fake，
    那是它们有意的选择，必须继续可用。

    本模块顶部的 `load_dotenv(...)` 已在 import 时把 `.env` 并入
    `os.environ`，因此查 `os.environ` 同时覆盖「系统环境变量」与「`.env`」
    两层，优先级也与 `load_model_settings()` 一致（环境变量 > .env）。
    """
    if (os.environ.get(PROVIDER_VAR) or "").strip():
        return

    raise ModelConfigError(
        f"正常模式未配置 {PROVIDER_VAR}。\n"
        "该变量的默认值是 fake，而 fake 只会重复固定回复，不具备正常聊天能力；"
        "因此这里明确报错，而不是悄悄退回固定回复。\n"
        "请配置真实 provider 之一：\n"
        f"  {PROVIDER_VAR}=minimax 并设置 MINIMAX_API_KEY\n"
        f"  {PROVIDER_VAR}=mimo    并设置 MIMO_API_KEY\n"
        "若只想跑流程验收，请用测试入口 scripts/tool07_acceptance_server.py"
        "（脚本模型，只用于流程验收）。"
    )


def _default_runtime_factory(
    settings: ApiSettings | None = None,
) -> "RuntimeContext":
    # 先校验再装配：配置缺失时要立刻失败，不能先建出一堆资源再报错。
    _require_configured_model_provider()

    # 与 CLI 一致注入默认工具（create_default_registry）。
    # 裸 ToolRegistry() 是空表，会导致任何 ToolCall 都以 ToolNotFoundError 失败。
    #
    # model_settings 在此处（lifespan 内）才读取环境：create_app() 与 import
    # 阶段既不读配置也不创建客户端，更不发网络请求。
    resolved = settings or ApiSettings()
    ws_root = resolved.workspaces_root
    runtime = create_runtime(
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
            # R4：先用 unavailable 启动；MCP 启动后再二次注入真实 executor
            web_search_provider=build_web_search_provider_from_config(
                kind=resolved.web_search_provider_kind,
                fake_results=list(resolved.web_search_fake_results),
                mcp_executor=None,
                mcp_tool_name=resolved.web_search_mcp_tool,
            ),
            **load_context_config(),
        ),
        workspaces_root=ws_root,
    )

    # R4：MCP 启动后二次注入真实搜索 Provider
    # kind=mcp + web_search_mcp_server 已配置 + MCPManager 已就绪 →
    # 把 mcp_manager._invoke 按**真实签名** (qualified, args, timeout) -> str
    # 包装成 executor，并用当前可信 run 上下文挂上目标工具的授权复核。
    if (
        resolved.web_search_provider_kind == "mcp"
        and resolved.web_search_mcp_server
        and getattr(runtime, "mcp_manager", None) is not None
    ):
        server_name = resolved.web_search_mcp_server
        tool_name = resolved.web_search_mcp_tool
        mcp_manager = runtime.mcp_manager
        # 验证该 server 已注册并暴露目标工具
        qualified = f"mcp__{server_name}__{tool_name}"
        if qualified in mcp_manager.registered_names:
            # 超时：复用 MCP server 的 call_timeout（缺省 30s）
            try:
                from mini_deerflow.mcp_manager import find_server_config

                call_timeout = find_server_config(
                    load_mcp_config(), server_name
                ).call_timeout
            except LookupError:
                # 服务器未在配置中 → 回落默认值
                call_timeout = 30.0
            # 其他异常（MCPConfigError / OSError 等）不捕获，由调用方处理

            def _mcp_executor(_tool: str, args: dict) -> str:
                # 真实契约：第三个位置参数 timeout；返回值是 **JSON 字符串**
                return mcp_manager._invoke(qualified, args, call_timeout)

            # 授权复核：与 Agent._do_tool_call 第三层同一套判定
            policy_reg = getattr(
                runtime.integrated_agent._agent, "_tool_policy_registry", None
            )

            def _context_guard(context: object | None) -> None:
                if policy_reg is None:
                    return  # 未启用策略层时不做额外判定（前两层仍生效）
                from mini_deerflow.tool_authorization import (
                    check_tool_access,
                    session_capabilities,
                )

                thread_id = getattr(context, "thread_id", None) or ""
                run_id = getattr(context, "run_id", "") or ""
                project_id = None
                store = getattr(policy_reg, "_thread_store", None)
                if thread_id and store is not None:
                    try:
                        project_id = store.get(thread_id).project_id
                    except Exception:
                        project_id = None
                authority = policy_reg.get_authority(
                    thread_id=thread_id,
                    run_id=run_id,
                    project_id=project_id,
                    granted_capabilities=session_capabilities(project_id),
                )
                decision = check_tool_access(policy_reg, qualified, authority)
                if decision.is_denied or decision.is_confirm:
                    from mini_deerflow.web_tools import WebToolPolicyError

                    raise WebToolPolicyError(
                        f"目标 MCP 工具 {qualified} 未授权: {decision.reason}"
                    )

            from mini_deerflow.web_tools import create_web_tools

            new_provider = build_web_search_provider_from_config(
                kind="mcp",
                mcp_executor=_mcp_executor,
                mcp_tool_name=tool_name,
                context_guard=_context_guard,
            )
            # 替换 config 上的 provider；并重建 web_search 工具以使用新 provider
            runtime.config = replace(
                runtime.config, web_search_provider=new_provider
            )
            # 找到 Agent 持有的 registry（私有属性）
            agent = runtime.integrated_agent._agent
            agent_registry = getattr(agent, "_registry", None)
            if agent_registry is not None:
                try:
                    agent_registry.unregister("web_search")
                except Exception:
                    pass
                new_tools = create_web_tools(
                    WebToolConfig(
                        timeout_s=runtime.config.web_read_timeout_s,
                        host_policy=runtime.config.web_host_policy,
                    ),
                    provider=new_provider,
                )
                agent_registry.register_many(
                    [t for t in new_tools if t.name == "web_search"]
                )

    return runtime


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
