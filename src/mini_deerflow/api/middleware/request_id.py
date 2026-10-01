"""Request ID 中间件（19F 阶段）。

职责：
- 读取 X-Request-ID（合法则复用）
- 缺失或非法则生成 req- 开头 ID
- 写入 request.state.request_id
- 写回响应头 X-Request-ID
- 支持 ContextVar 跨模块日志关联

输入限制：
- 最大 64 字符
- 仅允许 safe ASCII（非空白、无控制字符、无换行）
- 拒绝换行符防止日志注入
"""
from __future__ import annotations

import logging
import re
import uuid
from contextvars import ContextVar
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

_LOGGER = logging.getLogger(__name__)

# ContextVar：并发请求之间不串值
_request_id_var: ContextVar[str] = ContextVar("request_id", default="")

# 安全字符：字母数字 + 连字符 + 下划线 + 点（RFC 7230 token）
_VALID_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9\-_.]{1,64}$")

# 生成前缀
_ID_PREFIX = "req-"
_ID_GENERATOR = uuid.uuid4


def get_request_id() -> str:
    """从 ContextVar 读取当前请求 ID（供日志模块调用）。"""
    return _request_id_var.get()


def _generate_id() -> str:
    return f"{_ID_PREFIX}{uuid.uuid4().hex[:16]}"


def _validate_id(raw: str | None) -> str | None:
    """验证请求 ID：合法返回原值，否则返回 None。"""
    if raw is None:
        return None
    raw = raw.strip()
    if _VALID_REQUEST_ID_PATTERN.match(raw):
        return raw
    return None


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Request ID 中间件。

    放置在所有路由之前，确保：
    - 正常响应含 X-Request-ID
    - 404/422/500 等异常响应也含 X-Request-ID
    - CORS preflight 也透传
    """

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        # 读取或生成 ID
        incoming = request.headers.get("X-Request-ID")
        request_id = _validate_id(incoming) or _generate_id()

        # 写入 state + ContextVar
        request.state.request_id = request_id
        token = _request_id_var.set(request_id)

        # 追加到日志上下文
        # （如有结构化日志，可在 log format 中引用 request_id_var）
        try:
            response = await call_next(request)
        finally:
            _request_id_var.reset(token)

        # 所有响应（含异常）都写回 X-Request-ID
        response.headers["X-Request-ID"] = request_id

        return response
