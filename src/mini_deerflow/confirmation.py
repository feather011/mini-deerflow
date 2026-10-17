"""TOOL-04 后端确认生命周期。

确认请求的领域模型 + 持久化存储 + 状态机。

设计原则：
- **绑定具体执行上下文**：每个请求携带 run_id / thread_id / project_id / principal
  / tool_name / arguments(冻结哈希)；批准时这些必须完全一致才能执行
- **状态机单向推进**：PENDING → APPROVED → EXECUTED；PENDING → DENIED / EXPIRED / CANCELLED
- **原子持久化**：每次状态变更先写候选，原子替换
- **不重放**：handler 执行后状态置 EXECUTED，重复 approve 不再生效
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from mini_deerflow.mcp_errors import MCPError
from mini_deerflow.tool_contracts import ExecutorPrincipal


class ConfirmationError(MCPError):
    """TOOL-04 确认领域异常。"""


class ConfirmationState(str, enum.Enum):
    """确认请求状态。"""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    EXECUTED = "executed"

    def is_terminal(self) -> bool:
        return self in {
            ConfirmationState.EXECUTED,
            ConfirmationState.DENIED,
            ConfirmationState.EXPIRED,
            ConfirmationState.CANCELLED,
        }


def _hash_arguments(arguments: Mapping[str, Any]) -> str:
    """参数对象的稳定哈希。dict 必须按 keys 排序再序列化。"""
    payload = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ConfirmationRequest:
    """确认请求。frozen 保证创建后字段不变。"""

    request_id: str
    run_id: str
    thread_id: str
    project_id: str | None
    principal: str  # ExecutorPrincipal.name
    tool_name: str
    arguments_hash: str  # 锁定参数
    arguments: tuple[tuple[str, Any], ...]  # 原始参数（持久化用）
    requested_at: float
    expires_at: float
    state: ConfirmationState = ConfirmationState.PENDING
    decided_at: float | None = None
    decided_by: str | None = None
    executed_at: float | None = None
    execution_result: str | None = None  # "ok" / "error:xxx" / "unknown"

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        thread_id: str,
        project_id: str | None,
        principal: ExecutorPrincipal,
        tool_name: str,
        arguments: Mapping[str, Any],
        ttl_seconds: float = 300.0,
        now: float | None = None,
    ) -> "ConfirmationRequest":
        if ttl_seconds <= 0:
            raise ConfirmationError("ttl_seconds 必须 > 0")
        if not run_id or not thread_id or not tool_name:
            raise ConfirmationError("run_id / thread_id / tool_name 必填")
        if not isinstance(arguments, Mapping):
            raise ConfirmationError("arguments 必须是 dict")
        current = now if now is not None else time.time()
        return cls(
            request_id=f"cf-{uuid.uuid4().hex[:12]}",
            run_id=run_id,
            thread_id=thread_id,
            project_id=project_id,
            principal=principal.name,
            tool_name=tool_name,
            arguments_hash=_hash_arguments(arguments),
            arguments=tuple(sorted(arguments.items())),
            requested_at=current,
            expires_at=current + ttl_seconds,
        )

    def is_expired(self, now: float | None = None) -> bool:
        current = now if now is not None else time.time()
        return current >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "project_id": self.project_id,
            "principal": self.principal,
            "tool_name": self.tool_name,
            "arguments_hash": self.arguments_hash,
            "arguments": [list(item) for item in self.arguments],
            "requested_at": self.requested_at,
            "expires_at": self.expires_at,
            "state": self.state.value,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
            "executed_at": self.executed_at,
            "execution_result": self.execution_result,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ConfirmationRequest":
        return cls(
            request_id=raw["request_id"],
            run_id=raw["run_id"],
            thread_id=raw["thread_id"],
            project_id=raw.get("project_id"),
            principal=raw["principal"],
            tool_name=raw["tool_name"],
            arguments_hash=raw["arguments_hash"],
            arguments=tuple(tuple(item) for item in raw["arguments"]),
            requested_at=raw["requested_at"],
            expires_at=raw["expires_at"],
            state=ConfirmationState(raw["state"]),
            decided_at=raw.get("decided_at"),
            decided_by=raw.get("decided_by"),
            executed_at=raw.get("executed_at"),
            execution_result=raw.get("execution_result"),
        )


def _coerce_args_for_compare(
    arguments: Mapping[str, Any] | tuple[tuple[str, Any], ...],
) -> dict[str, Any]:
    """把不同形态的参数归一化为 dict 用于哈希比较。"""
    if isinstance(arguments, tuple):
        return dict(arguments)
    return dict(arguments)


class ConfirmationStore:
    """确认请求持久化存储（线程安全 by GIL 原子写）。"""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        if path is None:
            path = os.environ.get("MINI_DEERFLOW_CONFIRMATION_STORE", ".mini-deerflow/confirmations.json")
        self._path = Path(path)
        self._requests: dict[str, ConfirmationRequest] = {}
        self._loaded = False

    @property
    def path(self) -> Path:
        return self._path

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        if self._path.is_file():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ConfirmationError(f"确认存储 JSON 解析失败: {exc.msg}") from None
            if not isinstance(raw, Mapping):
                raise ConfirmationError("确认存储顶层必须是对象")
            requests = raw.get("requests", {})
            if not isinstance(requests, Mapping):
                raise ConfirmationError("requests 必须是对象")
            for rid, r_raw in requests.items():
                self._requests[str(rid)] = ConfirmationRequest.from_dict(r_raw)
        self._loaded = True

    def all(self) -> list[ConfirmationRequest]:
        self._ensure_loaded()
        return list(self._requests.values())

    def get(self, request_id: str) -> ConfirmationRequest | None:
        self._ensure_loaded()
        return self._requests.get(request_id)

    def add(self, request: ConfirmationRequest) -> None:
        self._ensure_loaded()
        self._save_then_replace(request.request_id, request)

    def update(self, request: ConfirmationRequest) -> None:
        self._ensure_loaded()
        self._save_then_replace(request.request_id, request)

    def _save_then_replace(self, rid: str, new_req: ConfirmationRequest) -> None:
        """R1 同款：候选字典写盘成功才更新内存。"""
        candidate = dict(self._requests)
        candidate[rid] = new_req
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix="confirm-state-", suffix=".tmp", dir=self._path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(
                    {"requests": {k: v.to_dict() for k, v in candidate.items()}},
                    f, ensure_ascii=False, indent=2,
                )
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except Exception:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            raise
        self._requests[rid] = new_req


__all__ = [
    "ConfirmationError",
    "ConfirmationRequest",
    "ConfirmationState",
    "ConfirmationStore",
    "_hash_arguments",
]
