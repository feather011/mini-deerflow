"""TOOL-04 确认 API 路由。

端点：
- POST   /confirmations                  创建确认请求
- GET    /confirmations                  列出（可选过滤）
- GET    /confirmations/{id}             详情
- POST   /confirmations/{id}/approve     批准
- POST   /confirmations/{id}/deny        拒绝
- POST   /confirmations/{id}/cancel      取消
- POST   /confirmations/{id}/execute     执行（带 run_id / thread_id / tool_name / arguments 匹配）
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field

from mini_deerflow.api.dependencies import get_runtime
from mini_deerflow.confirmation import (
    ConfirmationRequest,
    ConfirmationState,
    ConfirmationStore,
)
from mini_deerflow.confirmation_manager import ConfirmationManager
from mini_deerflow.runtime import RuntimeContext
from mini_deerflow.tool_authorization import ToolPolicyRegistry
from mini_deerflow.tool_contracts import ExecutorPrincipal

router = APIRouter(prefix="/confirmations", tags=["confirmations"])


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
        )
        runtime.confirmation_manager = manager
    return manager, store


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
    """执行确认请求的入参。必须完全匹配绑定上下文。"""

    run_id: str
    thread_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


def _to_info(req: ConfirmationRequest) -> dict[str, Any]:
    return {
        "request_id": req.request_id,
        "run_id": req.run_id,
        "thread_id": req.thread_id,
        "project_id": req.project_id,
        "principal": req.principal,
        "tool_name": req.tool_name,
        "arguments_hash": req.arguments_hash,
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
    summary="列出确认请求",
)
def list_confirmations(
    runtime: RuntimeContext = Depends(get_runtime),
    state: str | None = None,
) -> dict:
    _, store = _get_components(runtime)
    items = store.all()
    if state is not None:
        items = [r for r in items if r.state.value == state]
    return {"confirmations": [_to_info(r) for r in items]}


@router.get(
    "/{request_id}",
    summary="确认请求详情",
)
def get_confirmation(
    request_id: str = Path(...),
    runtime: RuntimeContext = Depends(get_runtime),
) -> dict:
    _, store = _get_components(runtime)
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
    return _to_info(req)


@router.post(
    "/{request_id}/execute",
    summary="执行确认请求（需完全匹配绑定上下文）",
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
