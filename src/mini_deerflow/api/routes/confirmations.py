"""TOOL-04 确认 API 路由 + TOOL-05 浏览器侧展示契约。

端点：
- POST   /confirmations                  创建确认请求
- GET    /confirmations                  列出（thread_id / run_id / state 过滤）
- GET    /confirmations/{id}             详情
- POST   /confirmations/{id}/approve     批准
- POST   /confirmations/{id}/deny        拒绝
- POST   /confirmations/{id}/cancel      取消
- POST   /confirmations/{id}/execute     旧执行入口（带 arguments 客户端回传）
- POST   /confirmations/{id}/execute-snapshot  TOOL-05 新入口：只带绑定核对字段，
                                              服务端读保存的参数快照执行

TOOL-05 契约要点：
- 列表与详情**只返回** `arguments_display`（脱敏摘要），不返回原始 `arguments`
- 快照执行入口要求 body 带 run_id/thread_id/tool_name/arguments_hash，
  服务端比对后从 ConfirmationRequest.arguments 读原值，传给现有真实执行器
"""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from mini_deerflow.api.dependencies import get_runtime
from mini_deerflow.confirmation import (
    ConfirmationRequest,
    ConfirmationState,
    ConfirmationStore,
    _hash_arguments,
)
from mini_deerflow.confirmation_manager import ConfirmationManager
from mini_deerflow.runtime import RuntimeContext
from mini_deerflow.tool_authorization import ToolPolicyRegistry
from mini_deerflow.tool_contracts import ExecutorPrincipal

router = APIRouter(prefix="/confirmations", tags=["confirmations"])


# ---------------------------------------------------------------------------
# 脱敏（TOOL-05）
# ---------------------------------------------------------------------------

_SENSITIVE_KEY = re.compile(
    r"(api[_-]?key|token|secret|password|passwd|pwd|auth|credential|cred|access[_-]?token)",
    re.I,
)
_MAX_DEPTH = 8
_MASK = "••••••"
_PREFIX_TAIL = 4  # 长值保留前 4 后 4

# 匹配 Authorization / Bearer / Basic 开头的凭据字符串（用于在自由文本中识别）
# 整段吞到结构化分隔符（行末 / ,;\"'<>]）—— 保留前导 "Authorization: " 标签。
# 凭据内允许空格以吞 "Bearer xxx" / "Basic xxx" 形式。
_BEARER_PATTERN = re.compile(
    r"(\b(?:Authorization|Bearer|Basic)\b\s*[:=]?\s*)([^\r\n,;\"'<>]+)",
    re.I,
)
# URL 中 userinfo：user:pass@host 或 token@host（token-only，无密码）都是凭据
_URL_USERINFO_PATTERN = re.compile(r"^([^/@:]+)(?::([^@]*))?@(.+)$")
# 自由文本（如命令）中的内嵌 URL 子串 —— 不把整段命令当单个 URL 解析
_EMBEDDED_URL_PATTERN = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"'<>]+")


def _mask_netloc(netloc: str) -> str:
    """netloc 中的 userinfo（含 token-only 形式）整体按凭据处理。"""
    if "@" not in netloc:
        return netloc
    m = _URL_USERINFO_PATTERN.match(netloc)
    if not m:
        return netloc
    user = m.group(1)
    has_password = m.group(2) is not None
    host = m.group(3)
    if has_password:
        # user:password@host —— password 全掩，user 截短
        masked_user = user[:2] + "•••" if len(user) > 2 else "•••"
        return f"{masked_user}:{_MASK}@{host}"
    # token-only：user 本身就是凭据 → 全掩
    return f"{_MASK}@{host}"


def _mask_query(query: str) -> str:
    """query 保守安全摘要：所有 value 全掩，仅保留 key 结构。

    不按敏感键判断——签名值（sig/token/expires 等组合）无法穷举键名，
    保守全掩避免漏出。
    """
    if not query:
        return query
    from urllib.parse import parse_qsl, urlencode
    pairs = parse_qsl(query, keep_blank_values=True)
    return urlencode([(k, _MASK) for k, _v in pairs])


def _mask_fragment(fragment: str) -> str:
    """fragment 同样保守：键值对形式全掩 value；裸 token 直接全掩。"""
    if not fragment:
        return fragment
    if "=" not in fragment:
        return _MASK
    from urllib.parse import parse_qsl, urlencode
    pairs = parse_qsl(fragment, keep_blank_values=True)
    return urlencode([(k, _MASK) for k, _v in pairs])


def _mask_url(value: str) -> str:
    """URL 脱敏：userinfo（含 token-only）、query、fragment 保守掩码。"""
    try:
        sp = urlsplit(value)
    except ValueError:
        return value
    netloc = _mask_netloc(sp.netloc)
    new_query = _mask_query(sp.query)
    new_fragment = _mask_fragment(sp.fragment)
    return urlunsplit((sp.scheme, netloc, sp.path, new_query, new_fragment))


def _mask_bearer_string(value: str) -> str:
    """识别 Authorization: Bearer xxx / Bearer xxx 形式，掩凭据部分。"""
    def _sub(m: re.Match) -> str:
        return f"{m.group(1)}{_MASK}"
    return _BEARER_PATTERN.sub(_sub, value)


def _mask_embedded_urls(value: str) -> str:
    """自由文本（如 shell 命令）中内嵌的 URL 逐个脱敏。"""
    return _EMBEDDED_URL_PATTERN.sub(lambda m: _mask_url(m.group(0)), value)


def _mask_text(value: str) -> str:
    """字符串脱敏组合策略（顺序敏感）：

    1. 先掩 Authorization/Bearer/Basic 凭据段 —— 凭据值可能形如 https://...
       若先按 URL 解析会把整个命令当单个 URL 处理而漏掉 Bearer；
    2. 再对文本中内嵌的每个 URL 单独脱敏（不把整段命令当 URL 解析）。
    """
    masked = _mask_bearer_string(value)
    if "://" in masked:
        masked = _mask_embedded_urls(masked)
    return masked


def _mask_value(value: Any, *, depth: int = 0) -> Any:
    """递归脱敏：键名匹配敏感 pattern → 全掩；自由文本走 _mask_text 组合策略；其他原样。"""
    if depth > _MAX_DEPTH:
        return "..."
    if isinstance(value, dict):
        return {
            k: (_MASK if _SENSITIVE_KEY.search(k) else _mask_value(v, depth=depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_mask_value(v, depth=depth + 1) for v in value]
    if isinstance(value, str):
        return _mask_text(value)
    return value
    return value


# ---------------------------------------------------------------------------
# 组件获取
# ---------------------------------------------------------------------------


def _get_components(runtime: RuntimeContext) -> tuple[ConfirmationManager, ConfirmationStore]:
    """从 runtime 获取 manager / store；缺则创建默认。"""
    store: ConfirmationStore | None = getattr(runtime, "confirmation_store", None)
    if store is None:
        store = ConfirmationStore()
        runtime.confirmation_store = store
    manager: ConfirmationManager | None = getattr(runtime, "confirmation_manager", None)
    if manager is None:
        policy_registry: ToolPolicyRegistry | None = None
        agent = getattr(runtime, "agent", None) or getattr(runtime, "integrated_agent", None)
        if agent is not None:
            policy_registry = getattr(agent, "_tool_policy_registry", None)
        manager = ConfirmationManager(
            store=store,
            policy_registry=policy_registry or ToolPolicyRegistry(),
            runtime=runtime,
        )
        runtime.confirmation_manager = manager
    return manager, store


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class CreateConfirmationRequest(BaseModel):
    """创建确认请求的入参。"""

    run_id: str
    thread_id: str
    project_id: str | None = None
    principal: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    ttl_seconds: float = 300.0


class DecisionRequest(BaseModel):
    """批准/拒绝/取消请求的入参。"""

    decided_by: str = "user"


class ExecuteRequest(BaseModel):
    """旧执行入口入参。完全匹配绑定上下文。"""

    run_id: str
    thread_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ExecuteSnapshotRequest(BaseModel):
    """TOOL-05 快照执行入口入参。

    浏览器只带绑定核对字段；原始 arguments 由服务端从 ConfirmationRequest 读取。
    """

    run_id: str
    thread_id: str
    tool_name: str
    arguments_hash: str


# ---------------------------------------------------------------------------
# 响应装配（_to_info）
# ---------------------------------------------------------------------------


def _to_info(req: ConfirmationRequest) -> dict[str, Any]:
    """TOOL-05：响应只暴露 `arguments_display`，不暴露原值。"""
    return {
        "request_id": req.request_id,
        "run_id": req.run_id,
        "thread_id": req.thread_id,
        "project_id": req.project_id,
        "principal": req.principal,
        "tool_name": req.tool_name,
        "arguments_hash": req.arguments_hash,
        "arguments_display": _mask_value(req.arguments),
        "requested_at": req.requested_at,
        "expires_at": req.expires_at,
        "state": req.state.value,
        "decided_at": req.decided_at,
        "decided_by": req.decided_by,
        "executed_at": req.executed_at,
        "execution_result": req.execution_result,
    }


def _principal_or_400(value: str) -> ExecutorPrincipal:
    try:
        return ExecutorPrincipal[value]
    except KeyError:
        raise HTTPException(
            status_code=400,
            detail=f"未知主体: {value!r}",
        )


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@router.post(
    "",
    summary="创建确认请求",
)
def create_confirmation(
    body: CreateConfirmationRequest,
    runtime: RuntimeContext = Depends(get_runtime),
) -> dict:
    manager, _ = _get_components(runtime)
    principal = _principal_or_400(body.principal)
    try:
        req = manager.create(
            run_id=body.run_id,
            thread_id=body.thread_id,
            project_id=body.project_id,
            principal=principal,
            tool_name=body.tool_name,
            arguments=body.arguments,
            ttl_seconds=body.ttl_seconds,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _to_info(req)


@router.get(
    "",
    summary="列出确认请求（TOOL-05 支持 thread_id / run_id / state 过滤）",
)
def list_confirmations(
    runtime: RuntimeContext = Depends(get_runtime),
    state: str | None = None,
    thread_id: str | None = None,
    run_id: str | None = None,
) -> dict:
    manager, store = _get_components(runtime)
    # TOOL-05：查询时走 manager 锁同步过期；不影响已领取/已执行记录
    try:
        manager.sweep_expired()
    except Exception:
        pass
    items = store.all()
    if state is not None:
        items = [r for r in items if r.state.value == state]
    if thread_id is not None:
        items = [r for r in items if r.thread_id == thread_id]
    if run_id is not None:
        items = [r for r in items if r.run_id == run_id]
    return {"confirmations": [_to_info(r) for r in items]}


@router.get(
    "/{request_id}",
    summary="确认请求详情",
)
def get_confirmation(
    request_id: str = Path(...),
    runtime: RuntimeContext = Depends(get_runtime),
) -> dict:
    manager, store = _get_components(runtime)
    # TOOL-05：详情也走过期同步
    try:
        manager.expire(request_id)
    except Exception:
        pass
    req = store.get(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail=f"未知确认请求: {request_id}")
    return _to_info(req)


@router.post(
    "/{request_id}/approve",
    summary="批准确认请求",
)
def approve_confirmation(
    request_id: str = Path(...),
    body: DecisionRequest = ...,
    runtime: RuntimeContext = Depends(get_runtime),
) -> dict:
    manager, _ = _get_components(runtime)
    try:
        req = manager.approve(request_id, decided_by=body.decided_by)
    except Exception as e:
        raise HTTPException(status_code=409, detail=str(e))
    return _to_info(req)


@router.post(
    "/{request_id}/deny",
    summary="拒绝确认请求",
)
def deny_confirmation(
    request_id: str = Path(...),
    body: DecisionRequest = ...,
    runtime: RuntimeContext = Depends(get_runtime),
) -> dict:
    manager, _ = _get_components(runtime)
    try:
        req = manager.deny(request_id, decided_by=body.decided_by)
    except Exception as e:
        raise HTTPException(status_code=409, detail=str(e))
    # R1 round 5：deny 也要唤醒 Agent 的等待
    if hasattr(runtime, "signal_confirmation"):
        try:
            runtime.signal_confirmation(
                request_id,
                outcome="denied",
                error=f"denied by {body.decided_by}",
            )
        except Exception:
            pass
    return _to_info(req)


@router.post(
    "/{request_id}/cancel",
    summary="取消确认请求（停止 run）",
)
def cancel_confirmation(
    request_id: str = Path(...),
    body: DecisionRequest = ...,
    runtime: RuntimeContext = Depends(get_runtime),
) -> dict:
    manager, _ = _get_components(runtime)
    try:
        req = manager.cancel(request_id, decided_by=body.decided_by)
    except Exception as e:
        raise HTTPException(status_code=409, detail=str(e))
    # R1 round 5：cancel 也要唤醒 Agent 的等待
    if hasattr(runtime, "signal_confirmation"):
        try:
            runtime.signal_confirmation(
                request_id,
                outcome="cancelled",
                error=f"cancelled by {body.decided_by}",
            )
        except Exception:
            pass
    return _to_info(req)


@router.post(
    "/{request_id}/execute",
    summary="执行确认请求（需完全匹配绑定上下文，body 携带 arguments）",
)
def execute_confirmation(
    request_id: str = Path(...),
    body: ExecuteRequest = ...,
    runtime: RuntimeContext = Depends(get_runtime),
) -> dict:
    """R1：必须提供真实执行器；runtime.execute_confirmed_request 走 ToolRegistry。
    不在 API 路由内直接调用 tool.handler。
    """
    invoker = getattr(runtime, "execute_confirmed_request", None)
    if invoker is None:
        raise HTTPException(
            status_code=503,
            detail="Runtime 未提供真实执行器（execute_confirmed_request）",
        )
    try:
        req = invoker(
            request_id,
            run_id=body.run_id,
            thread_id=body.thread_id,
            tool_name=body.tool_name,
            arguments=body.arguments,
        )
    except Exception as e:
        raise HTTPException(status_code=409, detail=str(e))
    return _to_info(req)


@router.post(
    "/{request_id}/execute-snapshot",
    summary="TOOL-05 快照执行：服务端读保存的参数快照执行；body 不携带原值",
)
def execute_snapshot_confirmation(
    request_id: str = Path(...),
    body: ExecuteSnapshotRequest = ...,
    runtime: RuntimeContext = Depends(get_runtime),
) -> dict:
    """TOOL-05 浏览器友好的执行入口。

    流程：
    1. 走原有 TOOL-04 真实执行器（runtime.execute_confirmed_request）
    2. 内部读 ConfirmationRequest.arguments 的原值快照（hash 已校验一致）
    3. 复用 TOOL-04 的权限复核、领取、停止、结果回传等全部链路
    """
    invoker = getattr(runtime, "execute_confirmed_request", None)
    if invoker is None:
        raise HTTPException(
            status_code=503,
            detail="Runtime 未提供真实执行器（execute_confirmed_request）",
        )
    # TOOL-05：服务端从 store 读取快照 + hash 校验，避免在响应字段暴露原值
    store: ConfirmationStore | None = getattr(runtime, "confirmation_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Runtime 未提供确认存储")
    req = store.get(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail=f"未知确认请求: {request_id}")
    # hash 核对（防止客户端伪造不同 args 但带错 hash）
    if body.arguments_hash != req.arguments_hash:
        raise HTTPException(
            status_code=409,
            detail=(
                f"参数 hash 不匹配：请求 {req.arguments_hash[:12]} "
                f"调用 {body.arguments_hash[:12]}"
            ),
        )
    # 复制原值给 invoker；invoker 内部还会做 hash 校验
    arguments_snapshot = dict(req.arguments or {})
    try:
        result_req = invoker(
            request_id,
            run_id=body.run_id,
            thread_id=body.thread_id,
            tool_name=body.tool_name,
            arguments=arguments_snapshot,
        )
    except Exception as e:
        raise HTTPException(status_code=409, detail=str(e))
    return _to_info(result_req)
