"""统一错误响应 Schema（19F 阶段）。

稳定错误码规范：
- code: 机器码，前端可依赖其判断错误类型，不依赖 message
- message: 对用户安全的文案，不含路径/凭据/Traceback
- request_id: 本次请求 ID，可关联日志
- details: 字段级验证详情（仅 422），其余为 null
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- 稳定错误码


class ErrorCode(str):
    """稳定机器错误码（协约不可变）。"""

    # 4xx 客户端错误
    BAD_REQUEST = "BAD_REQUEST"           # 400
    UNAUTHORIZED = "UNAUTHORIZED"         # 401
    FORBIDDEN = "FORBIDDEN"               # 403
    NOT_FOUND = "NOT_FOUND"               # 404
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"  # 405
    CONFLICT = "CONFLICT"                # 409
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"  # 413
    UNPROCESSABLE_ENTITY = "UNPROCESSABLE_ENTITY"  # 422
    TOO_MANY_REQUESTS = "TOO_MANY_REQUESTS"  # 429

    # 5xx 服务端错误
    INTERNAL_ERROR = "INTERNAL_ERROR"    # 500
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"  # 503
    GATEWAY_TIMEOUT = "GATEWAY_TIMEOUT"  # 504

    @classmethod
    def from_status(cls, status_code: int) -> "ErrorCode":
        """HTTP 状态码 → 稳定错误码。"""
        mapping = {
            400: cls.BAD_REQUEST,
            401: cls.UNAUTHORIZED,
            403: cls.FORBIDDEN,
            404: cls.NOT_FOUND,
            405: cls.METHOD_NOT_ALLOWED,
            409: cls.CONFLICT,
            413: cls.PAYLOAD_TOO_LARGE,
            422: cls.UNPROCESSABLE_ENTITY,
            429: cls.TOO_MANY_REQUESTS,
            500: cls.INTERNAL_ERROR,
            503: cls.SERVICE_UNAVAILABLE,
            504: cls.GATEWAY_TIMEOUT,
        }
        return mapping.get(status_code, cls.INTERNAL_ERROR)


# --------------------------------------------------------------------------- ErrorResponse


class ErrorResponse(BaseModel):
    """统一错误响应格式。"""

    error: "ErrorDetail" = Field(..., description="错误详情")


class ErrorDetail(BaseModel):
    """error 字段内容。"""

    code: str = Field(..., description="稳定机器错误码，前端可依赖此值")
    message: str = Field(..., description="对用户安全的文案")
    request_id: str = Field(..., description="本次请求 ID，可关联日志")
    details: dict[str, Any] | None = Field(
        default=None,
        description="字段级验证详情（仅 422），其余为 null",
    )

    model_config = {"frozen": True}


def build_error_response(
    code: str,
    message: str,
    request_id: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造统一错误响应字典（路由层直接返回 dict）。"""
    return {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id,
            "details": details,
        }
    }


def safe_message(message: str | None, fallback: str) -> str:
    """确保 message 不为空或纯空白。"""
    if message and message.strip():
        return message.strip()
    return fallback
