"""Subagent 领域模型（17A 阶段）。

定义 SubagentTask、SubagentResult 和状态转换规则。
不包含执行逻辑，不持有运行时对象。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any


SCHEMA_VERSION = 1


class TaskStatus(Enum):
    """Subagent 任务状态枚举。"""
    QUEUED = auto()
    RUNNING = auto()
    SUCCEEDED = auto()
    FAILED = auto()
    CANCELLED = auto()
    TIMED_OUT = auto()


# --- 合法转换映射 ---
_VALID_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset({
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.TIMED_OUT,
    }),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
    TaskStatus.TIMED_OUT: frozenset(),
}


class InvalidTransitionError(Exception):
    """非法状态转换时抛出。"""
    def __init__(self, task_id: str, from_: TaskStatus, to: TaskStatus) -> None:
        self.task_id = task_id
        self.from_ = from_
        self.to = to
        super().__init__(
            f"任务 {task_id} 状态 {from_.name} 无法转换到 {to.name}"
        )


def _validate_transition(task_id: str, from_: TaskStatus, to: TaskStatus) -> None:
    """验证状态转换合法性；非法时抛出 InvalidTransitionError。"""
    allowed = _VALID_TRANSITIONS.get(from_, frozenset())
    if to not in allowed:
        raise InvalidTransitionError(task_id, from_, to)


# --- Context Policy ---


@dataclass(frozen=True)
class SubagentContextPolicy:
    """子任务可继承的上下文策略。"""
    include_memory: bool = False
    include_skill_context: bool = False
    include_system_instruction: bool = False
    # 工具白名单（**运行时强制**，见 tools.build_allow_list / ensure_tool_allowed）：
    #   None   → 不设限制，沿用提交时传入的注册表（既有默认策略）
    #   []     → 空白名单，**任何工具都不允许**
    #   [...]  → 只允许列出的工具；本地工具、mcp__* 工具、子代理工具一视同仁
    allowed_tools: tuple[str, ...] | None = None
    # 最大子任务深度（相对于此任务）
    max_child_depth: int = 0
    # 是否允许子任务创建更多子任务
    allow_further_delegation: bool = False


# --- SubagentTask ---


@dataclass
class SubagentTask:
    """Subagent 任务域对象。状态可变（仅限合法转换），但字段不可随意修改。"""
    task_id: str
    parent_run_id: str
    parent_thread_id: str
    instruction: str
    status: TaskStatus
    created_at: float
    # 可选字段
    parent_task_id: str | None = None
    depth: int = 0
    timeout_seconds: float | None = None
    started_at: float | None = None
    finished_at: float | None = None
    context_policy: SubagentContextPolicy | None = None
    # TOOL-02：父 Agent 已授予子任务的能力集合
    parent_granted_capabilities: frozenset[Any] = field(default_factory=frozenset)
    # 元数据（不可变字典）
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id:
            raise ValueError("task_id 不能为空")
        if not isinstance(self.parent_run_id, str) or not self.parent_run_id:
            raise ValueError("parent_run_id 不能为空")
        if not isinstance(self.parent_thread_id, str) or not self.parent_thread_id:
            raise ValueError("parent_thread_id 不能为空")
        if not isinstance(self.instruction, str) or not self.instruction:
            raise ValueError("instruction 不能为空")
        if not isinstance(self.status, TaskStatus):
            raise TypeError(f"status 必须是 TaskStatus，收到: {type(self.status).__name__}")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须为正数")
        if self.started_at is not None and self.created_at > self.started_at:
            raise ValueError("started_at 不能早于 created_at")
        if self.finished_at is not None:
            if self.started_at is None:
                raise ValueError("finished_at 必须配合 started_at")
            if self.started_at > self.finished_at:
                raise ValueError("finished_at 不能早于 started_at")

    def transition_to(self, new_status: TaskStatus) -> None:
        """在合法时转换状态；非法转换抛出 InvalidTransitionError。终态不可变。"""
        _validate_transition(self.task_id, self.status, new_status)
        self.status = new_status
        now = time.monotonic()
        if new_status == TaskStatus.RUNNING and self.started_at is None:
            self.started_at = now
        if new_status in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.TIMED_OUT,
        }:
            self.finished_at = now

    def to_dict(self) -> dict[str, object]:
        """序列化：仅包含可安全持久化的字段。"""
        result: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "task_id": self.task_id,
            "parent_run_id": self.parent_run_id,
            "parent_thread_id": self.parent_thread_id,
            "instruction": self.instruction,
            "status": self.status.name,
            "created_at": self.created_at,
        }
        if self.parent_task_id is not None:
            result["parent_task_id"] = self.parent_task_id
        if self.depth != 0:
            result["depth"] = self.depth
        if self.timeout_seconds is not None:
            result["timeout_seconds"] = self.timeout_seconds
        if self.started_at is not None:
            result["started_at"] = self.started_at
        if self.finished_at is not None:
            result["finished_at"] = self.finished_at
        if self.context_policy is not None:
            result["context_policy"] = {
                "include_memory": self.context_policy.include_memory,
                "include_skill_context": self.context_policy.include_skill_context,
                "include_system_instruction": self.context_policy.include_system_instruction,
                "allowed_tools": self.context_policy.allowed_tools,
                "max_child_depth": self.context_policy.max_child_depth,
                "allow_further_delegation": self.context_policy.allow_further_delegation,
            }
        if self.metadata:
            result["metadata"] = dict(self.metadata)
        return result

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "SubagentTask":
        """反序列化。"""
        if not isinstance(data, dict):
            raise ValueError("data 必须是字典")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"未知 schema_version: {data.get('schema_version')}")

        def req(key: str) -> Any:
            if key not in data:
                raise ValueError(f"缺少字段: {key}")
            return data[key]

        status_str = req("status")
        try:
            status = TaskStatus[status_str]
        except KeyError:
            raise ValueError(f"未知状态: {status_str}")

        created_at = float(req("created_at"))
        started_at = float(v) if (v := data.get("started_at")) is not None else None
        finished_at = float(v) if (v := data.get("finished_at")) is not None else None

        ctx = None
        if (ctx_data := data.get("context_policy")) is not None:
            ctx = SubagentContextPolicy(
                include_memory=bool(ctx_data.get("include_memory", False)),
                include_skill_context=bool(ctx_data.get("include_skill_context", False)),
                include_system_instruction=bool(ctx_data.get("include_system_instruction", False)),
                allowed_tools=tuple(ctx_data["allowed_tools"]) if ctx_data.get("allowed_tools") is not None else None,
                max_child_depth=int(ctx_data.get("max_child_depth", 0)),
                allow_further_delegation=bool(ctx_data.get("allow_further_delegation", False)),
            )

        return cls(
            task_id=str(req("task_id")),
            parent_run_id=str(req("parent_run_id")),
            parent_thread_id=str(req("parent_thread_id")),
            instruction=str(req("instruction")),
            status=status,
            created_at=created_at,
            parent_task_id=str(v) if (v := data.get("parent_task_id")) is not None else None,
            depth=int(data.get("depth", 0)),
            timeout_seconds=float(v) if (v := data.get("timeout_seconds")) is not None else None,
            started_at=started_at,
            finished_at=finished_at,
            context_policy=ctx,
            metadata=dict(data.get("metadata", {})),
        )


# --- SubagentResult ---


@dataclass(frozen=True)
class SubagentResult:
    """Subagent 任务执行结果的不可变快照。"""
    task_id: str
    terminal_status: TaskStatus  # 必须是终态
    output: str
    error_type: str | None  # None 表示成功
    error_message: str | None
    started_at: float
    finished_at: float
    # duration 由 finished_at - started_at 计算，不存储
    run_id: str | None = None

    def __post_init__(self) -> None:
        if self.terminal_status not in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.TIMED_OUT,
        }:
            raise ValueError(
                f"terminal_status 必须是终态，收到: {self.terminal_status.name}"
            )
        if self.started_at > self.finished_at:
            raise ValueError("finished_at 不能早于 started_at")

    @property
    def duration(self) -> float:
        """执行时长（秒）。"""
        return self.finished_at - self.started_at

    @property
    def succeeded(self) -> bool:
        return self.terminal_status == TaskStatus.SUCCEEDED

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "task_id": self.task_id,
            "terminal_status": self.terminal_status.name,
            "output": self.output,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
        if self.run_id is not None:
            result["run_id"] = self.run_id
        return result

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "SubagentResult":
        if not isinstance(data, dict):
            raise ValueError("data 必须是字典")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"未知 schema_version: {data.get('schema_version')}")

        status_str = data.get("terminal_status")
        try:
            status = TaskStatus[status_str]
        except (KeyError, TypeError):
            raise ValueError(f"无效的 terminal_status: {status_str}")

        return cls(
            task_id=str(data["task_id"]),
            terminal_status=status,
            output=str(data.get("output", "")),
            error_type=str(v) if (v := data.get("error_type")) is not None else None,
            error_message=str(v) if (v := data.get("error_message")) is not None else None,
            started_at=float(data["started_at"]),
            finished_at=float(data["finished_at"]),
            run_id=str(v) if (v := data.get("run_id")) is not None else None,
        )

    @classmethod
    def from_task(cls, task: SubagentTask, output: str = "", error_type: str | None = None, error_message: str | None = None, run_id: str | None = None) -> "SubagentResult":
        """从已完成的任务构建结果。task 必须处于终态。"""
        if task.status not in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.TIMED_OUT,
        }:
            raise ValueError(f"task 状态必须为终态，收到: {task.status.name}")
        started = task.started_at if task.started_at is not None else task.created_at
        finished = task.finished_at if task.finished_at is not None else time.monotonic()
        return cls(
            task_id=task.task_id,
            terminal_status=task.status,
            output=output,
            error_type=error_type,
            error_message=error_message,
            started_at=started,
            finished_at=finished,
            run_id=run_id,
        )
