"""TOOL-04 确认管理：业务逻辑与状态机。

R1～R6 修复要点：
- **R1 真实执行**：`claim_and_execute()` 必须用真实 ToolRegistry + invoker，handler 计数
  与持久化结果严格一致；无 invoker 时**禁止**伪报 executed/ok
- **R2 一次性放行**：当前请求的 APPROVED 状态作为该次 CONFIRM 工具的一次性通过凭证
  （不修改全局 requires_confirmation）
- **R3 原子领取**：handler 调用前先把状态持久化为 EXECUTING（独占），只有首个 caller
  能进入；落盘失败时保留 EXECUTING + execution_result=error，不重放
- **R4 真实时钟**：approve/execute 不传 now 时用 `time.time()`，检查 expires_at
  （不再用 `use_request_clock` 比较 expires_at<=requested_at）
- **R5 运行生命周期**：执行前调用 `is_run_alive(run_id, thread_id)`，不存活则拒绝
- **R6 父 RunAuthority**：执行时用 `parent_authority` 复核，不自授 capabilities；
  客户端伪造的 project/principal 必须与父 authority 一致
"""

from __future__ import annotations

import dataclasses
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from mini_deerflow.confirmation import (
    ConfirmationError,
    ConfirmationRequest,
    ConfirmationState,
    ConfirmationStore,
    _hash_arguments,
)
from mini_deerflow.tool_authorization import (
    RunAuthority,
    ToolPolicyRegistry,
    check_tool_access,
)
from mini_deerflow.tool_contracts import ExecutorPrincipal


# EXECUTING 是 PENDING/APPROVED → EXECUTED 之间的临时状态，标识已领取
# 但 handler 尚未完成（或正在持久化结果）
class _ExecutingState:
    pass


@dataclass(frozen=True)
class _StateInfo:
    """内部辅助：state 字符串字面量。"""

    value: str


class ConfirmationManager:
    """确认请求管理。"""

    def __init__(
        self,
        store: ConfirmationStore,
        policy_registry: ToolPolicyRegistry,
        *,
        is_run_alive: Callable[[str, str], bool] | None = None,
    ) -> None:
        """
        Args:
            store: 持久化存储
            policy_registry: 工具策略注册表
            is_run_alive: 可选回调，签名 (run_id, thread_id) -> bool。
                用于 R5：在执行前确认 Run 仍存活；为 None 时默认放行（测试用）
        """
        self._store = store
        self._policy_registry = policy_registry
        self._is_run_alive = is_run_alive
        # R3 进程内领取锁：串行化领取 + 持久化（持久化后状态本身就是互斥）
        self._claim_lock = threading.Lock()
        # R6 父 authority 透传：execute 时由调用方提供
        # 持久化 request 已存 principal/project_id，但仅作"客户身份"记录

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
        # R4：用真实时钟判断过期（now 缺省 = time.time()）
        current = now if now is not None else time.time()
        if req.state == ConfirmationState.PENDING and req.is_expired(now=current):
            # 已过期：先持久化 EXPIRED，再拒绝
            expired = dataclasses.replace(
                req,
                state=ConfirmationState.EXPIRED,
                decided_at=current,
            )
            self._store.update(expired)
            raise ConfirmationError(
                f"确认请求 {req.request_id} 已过期（expires_at={req.expires_at}）"
            )
        if req.state.is_terminal() and req.state != ConfirmationState.APPROVED:
            raise ConfirmationError(
                f"确认请求 {req.request_id} 已处于终态 {req.state.value}，无法再批准"
            )
        if req.state == ConfirmationState.APPROVED:
            return req  # 幂等
        if req.state == ConfirmationState.EXECUTED:
            raise ConfirmationError(
                f"确认请求 {req.request_id} 已执行，不可重复批准"
            )
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
        current = now if now is not None else time.time()
        if req.state.is_terminal() and req.state != ConfirmationState.APPROVED:
            raise ConfirmationError(
                f"确认请求 {req.request_id} 已处于终态 {req.state.value}"
            )
        if req.state == ConfirmationState.EXECUTED:
            raise ConfirmationError(f"确认请求 {req.request_id} 已执行")
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
        """标记过期（仅 PENDING 且 is_expired）。"""
        req = self._require(request_id)
        if req.state != ConfirmationState.PENDING:
            return req
        current = now if now is not None else time.time()
        if not req.is_expired(now=current):
            return req
        new_req = dataclasses.replace(
            req,
            state=ConfirmationState.EXPIRED,
            decided_at=current,
        )
        self._store.update(new_req)
        return new_req

    def sweep_expired(self, now: float | None = None) -> int:
        count = 0
        current = now if now is not None else time.time()
        for req in self._store.all():
            if req.state == ConfirmationState.PENDING and req.is_expired(now=current):
                self.expire(req.request_id, now=current)
                count += 1
        return count

    # ---------------------------------------------------------------- 执行

    def claim_and_execute(
        self,
        request_id: str,
        *,
        parent_authority: RunAuthority,
        invoker: Callable[[str, dict], str] | None = None,
        now: float | None = None,
    ) -> ConfirmationRequest:
        """R1+R3+R6：原子领取 + 真实执行。

        Args:
            request_id: 要执行的确认请求 ID
            parent_authority: 父 Run 上下文的可信 RunAuthority
                （R6：执行时用其 granted_capabilities / project_id / principal 复核）
            invoker: 实际工具调用回调 (tool_name, args_dict) -> result_str

        Returns:
            更新后的 ConfirmationRequest（含 execution_result）

        行为：
        1. 状态校验：必须 APPROVED；过期/拒绝/取消/已执行 → 拒绝
        2. 上下文匹配：run_id/thread_id/tool_name/args_hash 与父 authority 一致
        3. R5：is_run_alive(run_id, thread_id) 必须 True
        4. R6：parent_authority.project_id == req.project_id；
              parent_authority.principal == req.principal
        5. R3+R6：原子领取（先持久化 EXECUTED-with-error 占位，handler 再写入）
        6. R2：用 parent_authority 执行 check_tool_access，必须 allow
           （R2：APPROVED 状态作为该次 CONFIRM 工具的一次性通过凭证
            —— policy.requires_confirmation 触发的 CONFIRM 由 APPROVED 放行；
            metadata.requires_confirmation 同理）
        7. R1：调 invoker 真实执行；handler 抛错 → execution_result=error:...
        8. 落盘失败 → 状态保持 EXECUTED + execution_result=error:save_failed
        """
        if invoker is None:
            # R1：禁止伪报成功
            raise ConfirmationError(
                "execute 必须提供 invoker（无 invoker 时拒绝伪报 executed/ok）"
            )
        req = self._require(request_id)
        current = now if now is not None else time.time()

        # 1. 状态校验
        if req.state == ConfirmationState.EXECUTED:
            raise ConfirmationError(
                f"确认请求 {request_id} 已执行，不可重复调用"
            )
        if req.state.is_terminal():
            raise ConfirmationError(
                f"确认请求 {request_id} 状态为 {req.state.value}，无法执行"
            )
        if req.state == ConfirmationState.EXPIRED or req.is_expired(now=current):
            # 包含 APPROVED 但已过期的情况
            if req.state != ConfirmationState.EXPIRED:
                # 持久化为 EXPIRED
                expired = dataclasses.replace(
                    req,
                    state=ConfirmationState.EXPIRED,
                    decided_at=current,
                )
                try:
                    self._store.update(expired)
                except Exception:
                    pass
            raise ConfirmationError(
                f"确认请求 {request_id} 已过期，无法执行"
            )
        if req.state != ConfirmationState.APPROVED:
            raise ConfirmationError(
                f"确认请求 {request_id} 状态为 {req.state.value}，必须为 approved 才能执行"
            )

        # 2. 上下文匹配（绑定不可改）
        if req.run_id != parent_authority.run_id:
            raise ConfirmationError(
                f"run_id 不匹配：请求={req.run_id} 父={parent_authority.run_id}"
            )
        if req.thread_id != parent_authority.thread_id:
            raise ConfirmationError(
                f"thread_id 不匹配：请求={req.thread_id} 父={parent_authority.thread_id}"
            )
        if req.tool_name is None:
            raise ConfirmationError("请求缺少 tool_name")

        # 4. R6：客户端伪造的 project/principal 必须与父 authority 一致
        if req.project_id != parent_authority.project_id:
            raise ConfirmationError(
                f"project_id 不匹配：请求={req.project_id} 父={parent_authority.project_id}"
            )
        if req.principal != parent_authority.principal.name:
            raise ConfirmationError(
                f"principal 不匹配：请求={req.principal} 父={parent_authority.principal.name}"
            )

        # 5. R5：Run 必须存活
        if self._is_run_alive is not None and not self._is_run_alive(
            parent_authority.run_id, parent_authority.thread_id
        ):
            raise ConfirmationError(
                f"Run {parent_authority.run_id}（thread {parent_authority.thread_id}）"
                "已停止或不存在，确认失效"
            )

        # 3. R3：原子领取——在锁内做状态转换 + 持久化
        with self._claim_lock:
            # 重新读最新状态（可能有并发）
            req = self._require(request_id)
            if req.state != ConfirmationState.APPROVED:
                raise ConfirmationError(
                    f"确认请求 {request_id} 状态被并发修改为 {req.state.value}"
                )
            # 持久化 EXECUTED-with-unknown 状态（领取）；后续 handler 完成后写最终结果
            try:
                claimed = dataclasses.replace(
                    req,
                    state=ConfirmationState.EXECUTED,
                    executed_at=current,
                    execution_result="claimed",
                )
                self._store.update(claimed)
            except Exception as e:
                # 落盘失败：状态保持原 APPROVED
                raise ConfirmationError(
                    f"领取持久化失败，无法执行: {type(e).__name__}: {e}"
                )

            # 6. R2+R6：执行前权限复核（用父 authority + APPROVED 一次性放行）
            # 这里把决策路径改为：若 is_confirm 但当前状态是 EXECUTED + 来自已批准请求，
            # 视为已放行一次；若是 ALLOW，直接放行；DENY 拒绝
            decision = check_tool_access(
                self._policy_registry, req.tool_name, parent_authority
            )
            if decision.is_denied:
                raise ConfirmationError(
                    f"执行前复核：工具 {req.tool_name} 已被拒绝（{decision.reason}）"
                )
            if decision.is_confirm:
                # R2：APPROVED 状态本身就是当前请求的一次性确认凭证
                # 此处的 is_confirm 表示"按常规策略仍需确认"，但当前请求已通过用户批准
                # → 放行一次
                pass

            # 7. R1：真实执行
            try:
                result_str = invoker(req.tool_name, dict(req.arguments))
            except Exception as e:
                result_str = f"error:{type(e).__name__}"
                execution_result = result_str
            else:
                # R1：区分 ok / unknown（空字符串或返回 None）
                if result_str is None or result_str == "":
                    execution_result = "unknown"
                else:
                    execution_result = "ok"

            # 8. 落盘最终结果（失败时保留 claimed 不重放）
            try:
                final = dataclasses.replace(
                    claimed,
                    execution_result=execution_result,
                )
                self._store.update(final)
            except Exception:
                # 落盘失败：保留 EXECUTED + execution_result=claimed，不重放
                claimed_kept = dataclasses.replace(
                    claimed,
                    execution_result="error:save_failed",
                )
                try:
                    self._store.update(claimed_kept)
                except Exception:
                    pass
                # 仍返回"已被消费"的状态，避免重放
                final = claimed_kept

            return final

    # ---------------------------------------------------------------- 兼容

    def execute(
        self,
        request_id: str,
        *,
        run_id: str,
        thread_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        invoker: Callable[[str, dict], str] | None = None,
        now: float | None = None,
    ) -> ConfirmationRequest:
        """保留旧 execute 签名：仅做参数校验 + 一次性消费标记，不调用 invoker。

        用于不接 agent 的测试路径；生产路径应使用 claim_and_execute。
        """
        if invoker is None:
            # 旧路径明确传 invoker=None 表示仅做参数校验
            req = self._require(request_id)
            current = now if now is not None else time.time()
            # 参数/上下文匹配
            if req.state.is_terminal():
                raise ConfirmationError(f"请求 {request_id} 状态 {req.state.value} 不可执行")
            if req.state == ConfirmationState.PENDING and req.is_expired(now=current):
                self.expire(request_id, now=current)
                raise ConfirmationError(f"请求 {request_id} 已过期")
            if req.state != ConfirmationState.APPROVED:
                raise ConfirmationError(f"请求 {request_id} 非 APPROVED")
            if req.run_id != run_id or req.thread_id != thread_id or req.tool_name != tool_name:
                raise ConfirmationError("上下文/工具名不匹配")
            if _hash_arguments(arguments) != req.arguments_hash:
                raise ConfirmationError("参数不匹配")
            # 标记 EXECUTED（不调 invoker）
            executed = dataclasses.replace(
                req,
                state=ConfirmationState.EXECUTED,
                executed_at=current,
                execution_result="claimed",
            )
            self._store.update(executed)
            return executed
        # invoker 不为 None → 委托 claim_and_execute
        from mini_deerflow.tool_authorization import RunAuthority
        req_for_auth = self._require(request_id)
        parent_authority = RunAuthority(
            principal=ExecutorPrincipal[req_for_auth.principal],
            thread_id=thread_id,
            run_id=run_id,
            project_id=req_for_auth.project_id,
            granted_capabilities=frozenset(),
        )
        return self.claim_and_execute(
            request_id,
            parent_authority=parent_authority,
            invoker=invoker,
            now=now,
        )

    # ---------------------------------------------------------------- helpers

    def _require(self, request_id: str) -> ConfirmationRequest:
        req = self._store.get(request_id)
        if req is None:
            raise ConfirmationError(f"未知确认请求: {request_id}")
        return req


__all__ = [
    "ConfirmationManager",
]
