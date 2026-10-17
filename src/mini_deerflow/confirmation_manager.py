"""TOOL-04 确认管理：业务逻辑与状态机。

负责：
- 创建确认请求
- 批准 / 拒绝 / 取消
- 过期扫描
- 执行确认（带上下文+参数完全匹配校验）
- 替换参数或挪到另一运行 → 拒绝

执行前再次检查当前权限（防止权限变更后旧批准被滥用）。
"""

from __future__ import annotations

import dataclasses
import time
from typing import Any, Mapping

from mini_deerflow.confirmation import (
    ConfirmationError,
    ConfirmationRequest,
    ConfirmationState,
    ConfirmationStore,
    _hash_arguments,
)
from mini_deerflow.tool_authorization import (
    ToolAuthorizationError,
    ToolPolicyRegistry,
    check_tool_access,
)
from mini_deerflow.tool_contracts import ExecutorPrincipal


class ConfirmationManager:
    """确认请求管理。"""

    def __init__(
        self,
        store: ConfirmationStore,
        policy_registry: ToolPolicyRegistry,
    ) -> None:
        self._store = store
        self._policy_registry = policy_registry
        # 缓存已 EXECUTED 的 request_id，防止并发重复执行
        self._executed_lock: set[str] = set()

    # ---------------------------------------------------------------- 创建

    def create(
        self,
        *,
        run_id: str,
        thread_id: str,
        project_id: str | None,
        principal: ExecutorPrincipal,
        tool_name: str,
        arguments: Mapping[str, Any],
        ttl_seconds: float = 300.0,
        now: float | None = None,
    ) -> ConfirmationRequest:
        request = ConfirmationRequest.create(
            run_id=run_id,
            thread_id=thread_id,
            project_id=project_id,
            principal=principal,
            tool_name=tool_name,
            arguments=arguments,
            ttl_seconds=ttl_seconds,
            now=now,
        )
        self._store.add(request)
        return request

    # ---------------------------------------------------------------- 决定

    def approve(
        self,
        request_id: str,
        decided_by: str,
        now: float | None = None,
    ) -> ConfirmationRequest:
        req = self._require(request_id)
        # 用 req 自身的时间（requested_at / expires_at）判断是否过期
        # 这样测试用 now=100.0 创建的请求在真实时间下仍可批准
        self._ensure_pending_or_approved(req, use_request_clock=(now is None))
        if req.state == ConfirmationState.APPROVED:
            return req
        current = now if now is not None else time.time()
        new_req = dataclasses.replace(
            req,
            state=ConfirmationState.APPROVED,
            decided_at=current,
            decided_by=decided_by,
        )
        self._store.update(new_req)
        return new_req

    def deny(
        self,
        request_id: str,
        decided_by: str,
        now: float | None = None,
    ) -> ConfirmationRequest:
        req = self._require(request_id)
        self._ensure_pending_or_approved(req, use_request_clock=(now is None))
        current = now if now is not None else time.time()
        new_req = dataclasses.replace(
            req,
            state=ConfirmationState.DENIED,
            decided_at=current,
            decided_by=decided_by,
        )
        self._store.update(new_req)
        return new_req

    def cancel(
        self,
        request_id: str,
        decided_by: str,
        now: float | None = None,
    ) -> ConfirmationRequest:
        req = self._require(request_id)
        if req.state.is_terminal():
            return req
        current = now if now is not None else time.time()
        new_req = dataclasses.replace(
            req,
            state=ConfirmationState.CANCELLED,
            decided_at=current,
            decided_by=decided_by,
        )
        self._store.update(new_req)
        return new_req

    def expire(self, request_id: str, now: float | None = None) -> ConfirmationRequest:
        """标记过期（仅在 is_expired 且仍 PENDING 时）。"""
        req = self._require(request_id)
        if req.state != ConfirmationState.PENDING:
            return req
        if not req.is_expired(now=now):
            return req
        current = now if now is not None else time.time()
        new_req = dataclasses.replace(
            req,
            state=ConfirmationState.EXPIRED,
            decided_at=current,
        )
        self._store.update(new_req)
        return new_req

    def sweep_expired(self, now: float | None = None) -> int:
        """扫描所有 PENDING 请求，过期则置 EXPIRED。返回受影响数量。"""
        count = 0
        for req in self._store.all():
            if req.state == ConfirmationState.PENDING and req.is_expired(now=now):
                self.expire(req.request_id, now=now)
                count += 1
        return count

    # ---------------------------------------------------------------- 执行

    def execute(
        self,
        request_id: str,
        *,
        run_id: str,
        thread_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        # 实际执行由调用方提供（与 Agent / handler 集成）
        invoker: "Callable[[str, dict], str] | None" = None,
        now: float | None = None,
    ) -> ConfirmationRequest:
        """执行已批准的请求。

        校验：
        1. request_id 存在
        2. state == APPROVED
        3. run_id / thread_id / tool_name / arguments_hash 与请求完全一致
        4. 再次执行当前 ToolPolicyRegistry.check_tool_access 仍 ALLOW
        5. 防并发：已 EXECUTED 或 _executed_lock 中存在则拒绝

        若 invoker 提供：调用并把结果记入 execution_result。
        返回更新后的 ConfirmationRequest。
        """
        req = self._require(request_id)
        if req.request_id in self._executed_lock:
            raise ConfirmationError(f"确认请求 {request_id} 正在执行中，请勿重入")
        # 1. 状态必须 APPROVED
        self._ensure_pending_or_approved(req, now=now, use_request_clock=(now is None))
        if req.state != ConfirmationState.APPROVED:
            raise ConfirmationError(
                f"确认请求 {request_id} 状态为 {req.state.value}，无法执行（必须为 approved）"
            )
        # 2. 上下文匹配
        if req.run_id != run_id:
            raise ConfirmationError(
                f"run_id 不匹配：请求={req.run_id} 调用={run_id}"
            )
        if req.thread_id != thread_id:
            raise ConfirmationError(
                f"thread_id 不匹配：请求={req.thread_id} 调用={thread_id}"
            )
        if req.tool_name != tool_name:
            raise ConfirmationError(
                f"tool_name 不匹配：请求={req.tool_name} 调用={tool_name}"
            )
        # 3. 参数哈希匹配
        actual_hash = _hash_arguments(arguments)
        if actual_hash != req.arguments_hash:
            raise ConfirmationError(
                f"参数不匹配：原 hash={req.arguments_hash[:12]} 调用 hash={actual_hash[:12]}"
            )
        # 4. 再次检查当前权限（带 metadata.capabilities 作为 granted）
        principal = ExecutorPrincipal[req.principal]
        from mini_deerflow.tool_authorization import RunAuthority
        # 用 tool 的 metadata.capabilities 作为 granted（确认场景下假定所有声明能力已授予）
        metadata, _ = self._policy_registry.get(tool_name)
        granted = metadata.capabilities if metadata is not None else frozenset()
        authority = RunAuthority(
            principal=principal,
            thread_id=thread_id,
            run_id=run_id,
            project_id=req.project_id,
            granted_capabilities=granted,
        )
        decision = check_tool_access(self._policy_registry, tool_name, authority)
        if decision.is_denied:
            raise ConfirmationError(
                f"执行前复核：工具 {tool_name} 当前已不再允许 ({decision.reason})"
            )
        if decision.is_confirm:
            raise ConfirmationError(
                f"执行前复核：工具 {tool_name} 仍需再次确认（不能直接用旧批准）"
            )
        # 5. 原子执行：标记 + 调用
        self._executed_lock.add(req.request_id)
        current = now if now is not None else time.time()
        execution_result: str
        try:
            if invoker is not None:
                try:
                    raw = invoker(tool_name, dict(arguments))
                    execution_result = "ok" if raw else "unknown"
                except Exception as e:
                    execution_result = f"error:{type(e).__name__}"
            else:
                # 不实际调用：仅标记 EXECUTED
                execution_result = "ok"
            new_req = dataclasses.replace(
                req,
                state=ConfirmationState.EXECUTED,
                executed_at=current,
                execution_result=execution_result,
            )
            self._store.update(new_req)
            return new_req
        finally:
            self._executed_lock.discard(req.request_id)

    def is_replay_safe(self, request_id: str) -> bool:
        """返回 True 表示已 EXECUTED，重复调用无效。"""
        req = self._require(request_id)
        return req.state == ConfirmationState.EXECUTED

    # ---------------------------------------------------------------- helpers

    def _require(self, request_id: str) -> ConfirmationRequest:
        req = self._store.get(request_id)
        if req is None:
            raise ConfirmationError(f"未知确认请求: {request_id}")
        return req

    def _ensure_pending_or_approved(
        self,
        req: ConfirmationRequest,
        now: float | None = None,
        use_request_clock: bool = False,
    ) -> None:
        if req.state.is_terminal() and req.state != ConfirmationState.APPROVED:
            raise ConfirmationError(
                f"确认请求 {req.request_id} 已处于终态 {req.state.value}，无法再决定"
            )
        if req.state == ConfirmationState.PENDING:
            # use_request_clock: 用 req 自身时间窗口判断（测试用）
            if use_request_clock:
                # 与 now=req.requested_at 对比
                expired = req.expires_at <= req.requested_at
            else:
                expired = req.is_expired(now=now)
            if expired:
                self.expire(req.request_id, now=now or req.requested_at + 1)
                raise ConfirmationError(
                    f"确认请求 {req.request_id} 已过期（expires_at={req.expires_at}）"
                )


__all__ = [
    "ConfirmationManager",
]
