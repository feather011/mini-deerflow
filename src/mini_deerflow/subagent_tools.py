"""Subagent 工具适配层（17D 阶段）。

将 SubagentManager 的能力包装为 Tool，供父 Agent 调用。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from mini_deerflow.subagent_executor import CancellationToken, SubagentBudget, SubagentManager
from mini_deerflow.subagent_models import (
    SubagentContextPolicy,
    SubagentResult,
    TaskStatus,
)
from mini_deerflow.tools import Tool, ToolRegistry


# --- 工具结果工厂 ---


def _ok(**kwargs: Any) -> str:
    payload = {"ok": True}
    payload.update(kwargs)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _error(tool_name: str, error_type: str, message: str) -> str:
    return json.dumps(
        {
            "ok": False,
            "error": {
                "type": error_type,
                "message": message,
            },
        },
        ensure_ascii=False,
        sort_keys=True,
    )


# --- 错误类型 ---


class SubagentToolError(ValueError):
    """工具参数校验失败时抛出。"""
    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"[subagent] 参数 '{field}': {reason}")


def _check_str(args: dict, name: str, field: str) -> str:
    if field not in args:
        raise SubagentToolError(field, "缺少必填字段")
    val = args[field]
    if not isinstance(val, str):
        raise SubagentToolError(field, f"必须是 str，收到: {type(val).__name__}")
    if not val.strip():
        raise SubagentToolError(field, "不能为空字符串")
    return val


def _check_int(args: dict, name: str, field: str, min_val: int | None = None) -> int:
    if field not in args:
        return 0  # 缺省返回 0
    val = args[field]
    if isinstance(val, bool):
        raise SubagentToolError(field, "bool 类型不合法")
    if not isinstance(val, (int, float)):
        raise SubagentToolError(field, f"必须是数字，收到: {type(val).__name__}")
    result = int(val)
    if min_val is not None and result < min_val:
        raise SubagentToolError(field, f"必须 >= {min_val}")
    return result


# --- Handler 实现 ---


class _SubmitHandler:
    """subagent_submit handler。"""
    __slots__ = ("_manager", "_parent_run_id", "_parent_thread_id", "_depth")

    def __init__(
        self,
        manager: SubagentManager,
        parent_run_id: str,
        parent_thread_id: str,
        depth: int = 1,
    ) -> None:
        self._manager = manager
        self._parent_run_id = parent_run_id
        self._parent_thread_id = parent_thread_id
        # 工具调用来源的子任务深度（父 Agent 视作 depth=0，本工具提交的子任务从 1 开始）
        self._depth = depth

    def __call__(self, arguments: dict[str, object], context: object | None = None) -> str:
        tool_name = "subagent_submit"
        try:
            if not isinstance(arguments, dict):
                raise SubagentToolError("arguments", "必须是 dict")

            # TOOL-02：从执行上下文提取父 Agent 已授予的能力，传递给子任务
            parent_caps: frozenset = frozenset()
            if context is not None:
                if hasattr(context, "granted_capabilities"):
                    parent_caps = getattr(context, "granted_capabilities") or frozenset()

            instruction = _check_str(arguments, tool_name, "instruction")
            timeout = _check_int(arguments, tool_name, "timeout_seconds", 1)
            max_steps = _check_int(arguments, tool_name, "max_steps", 1)
            max_model_calls = _check_int(arguments, tool_name, "max_model_calls", 1)
            max_tool_calls = _check_int(arguments, tool_name, "max_tool_calls", 1)

            # 上下文策略
            include_memory = bool(arguments.get("include_memory", False))
            include_skill = bool(arguments.get("include_skill_context", False))
            include_system = bool(arguments.get("include_system_instruction", False))
            allow_delegation = bool(arguments.get("allow_further_delegation", False))

            policy = SubagentContextPolicy(
                include_memory=include_memory,
                include_skill_context=include_skill,
                include_system_instruction=include_system,
                allow_further_delegation=allow_delegation,
            )

            # 构建 per-task budget。仅当调用方显式提供了局部上限时才创建独立 Budget；
            # 默认（None）走 Manager 的 shared_budget，父子共享同一额度。
            budget: SubagentBudget | None = None
            if max_model_calls or max_tool_calls or (max_steps and max_steps > 0):
                budget = SubagentBudget(
                    max_steps=max_steps,
                    max_model_calls=max_model_calls if max_model_calls else None,
                    max_tool_calls=max_tool_calls if max_tool_calls else None,
                )

            # TOOL-02：从执行上下文提取父 Agent 已授予的能力，传递给子任务
            parent_caps: frozenset = frozenset()
            if context is not None:
                if hasattr(context, "granted_capabilities"):
                    parent_caps = getattr(context, "granted_capabilities") or frozenset()

            task = self._manager.submit(
                instruction=instruction,
                parent_run_id=self._parent_run_id,
                parent_thread_id=self._parent_thread_id,
                depth=self._depth,
                timeout_seconds=timeout if timeout else None,
                budget=budget,
                context_policy=policy,
                parent_granted_capabilities=parent_caps,
            )

            return _ok(
                task_id=task.task_id,
                status=task.status.name,
                created_at=task.created_at,
            )

        except SubagentToolError:
            raise
        except Exception as e:
            return _error(tool_name, type(e).__name__, str(e))


class _StatusHandler:
    """subagent_status handler。"""
    __slots__ = ("_manager",)

    def __init__(self, manager: SubagentManager) -> None:
        self._manager = manager

    def __call__(self, arguments: dict[str, object]) -> str:
        tool_name = "subagent_status"
        try:
            if not isinstance(arguments, dict):
                raise SubagentToolError("arguments", "必须是 dict")
            task_id = _check_str(arguments, tool_name, "task_id")

            try:
                status = self._manager.get_status(task_id)
            except KeyError:
                return _error(tool_name, "TaskNotFoundError", f"未知任务: {task_id}")

            tasks = self._manager.list_tasks()
            task = next((t for t in tasks if t.task_id == task_id), None)

            result: dict[str, object] = {
                "task_id": task_id,
                "status": status.name,
            }
            if task:
                if task.started_at is not None:
                    result["started_at"] = task.started_at
                if task.finished_at is not None:
                    result["finished_at"] = task.finished_at
                if task.timeout_seconds is not None:
                    result["timeout_seconds"] = task.timeout_seconds

            return _ok(**result)

        except SubagentToolError:
            raise
        except Exception as e:
            return _error(tool_name, type(e).__name__, str(e))


class _CollectHandler:
    """subagent_collect handler。"""
    __slots__ = ("_manager",)

    def __init__(self, manager: SubagentManager) -> None:
        self._manager = manager

    def __call__(self, arguments: dict[str, object]) -> str:
        tool_name = "subagent_collect"
        try:
            if not isinstance(arguments, dict):
                raise SubagentToolError("arguments", "必须是 dict")

            task_id = _check_str(arguments, tool_name, "task_id")
            timeout = _check_int(arguments, tool_name, "timeout", 0)

            try:
                result = self._manager.collect(task_id, timeout=timeout)
                return _ok(
                    task_id=result.task_id,
                    status=result.terminal_status.name,
                    output=result.output,
                    succeeded=result.succeeded,
                    duration=result.duration,
                    error_type=result.error_type,
                    error_message=result.error_message,
                )
            except KeyError:
                status = self._manager.get_status(task_id)
                return _ok(
                    task_id=task_id,
                    status=status.name,
                    output="",
                    succeeded=False,
                    collected=False,
                    reason="任务尚未完成",
                )
            except TimeoutError:
                status = self._manager.get_status(task_id)
                return _ok(
                    task_id=task_id,
                    status=status.name,
                    output="",
                    succeeded=False,
                    collected=False,
                    reason="等待超时",
                )

        except SubagentToolError:
            raise
        except Exception as e:
            return _error(tool_name, type(e).__name__, str(e))


class _CancelHandler:
    """subagent_cancel handler。"""
    __slots__ = ("_manager",)

    def __init__(self, manager: SubagentManager) -> None:
        self._manager = manager

    def __call__(self, arguments: dict[str, object]) -> str:
        tool_name = "subagent_cancel"
        try:
            if not isinstance(arguments, dict):
                raise SubagentToolError("arguments", "必须是 dict")
            task_id = _check_str(arguments, tool_name, "task_id")

            accepted = self._manager.cancel(task_id)
            if not accepted:
                try:
                    status = self._manager.get_status(task_id)
                    return _ok(
                        task_id=task_id,
                        cancel_accepted=False,
                        reason=f"任务已处于终态: {status.name}",
                    )
                except KeyError:
                    return _error(tool_name, "TaskNotFoundError", f"未知任务: {task_id}")

            return _ok(
                task_id=task_id,
                cancel_accepted=True,
            )

        except SubagentToolError:
            raise
        except Exception as e:
            return _error(tool_name, type(e).__name__, str(e))


# --- 工具创建 ---


def create_subagent_tools(
    manager: SubagentManager,
    parent_run_id: str,
    parent_thread_id: str,
) -> list[Tool]:
    """创建 4 个 Subagent 工具。"""
    return [
        Tool(
            name="subagent_submit",
            description=(
                "提交一个子 Agent 任务（委派执行）。"
                "返回 task_id 和 queued/running 状态。"
                "使用 subagent_collect 回收结果。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "instruction": {
                        "type": "string",
                        "description": "子任务的指令",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "超时秒数（默认使用系统配置）",
                    },
                    "max_steps": {
                        "type": "integer",
                        "description": "最大 Agent 步数",
                    },
                    "max_model_calls": {
                        "type": "integer",
                        "description": "最大模型调用次数",
                    },
                    "max_tool_calls": {
                        "type": "integer",
                        "description": "最大工具调用次数",
                    },
                    "include_memory": {
                        "type": "boolean",
                        "description": "是否传递长期记忆上下文",
                    },
                    "include_skill_context": {
                        "type": "boolean",
                        "description": "是否传递 Skill 上下文",
                    },
                },
                "required": ["instruction"],
                "additionalProperties": False,
            },
            handler=_SubmitHandler(manager, parent_run_id, parent_thread_id),
        ),
        Tool(
            name="subagent_status",
            description="查询子任务的当前状态。",
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "要查询的任务 ID",
                    },
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
            handler=_StatusHandler(manager),
        ),
        Tool(
            name="subagent_collect",
            description=(
                "回收子任务结果。"
                "timeout=0 立即返回（未完成则说明状态）。"
                "重复 collect 幂等。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "要回收的任务 ID",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "等待超时秒数（默认 0，立即返回）",
                    },
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
            handler=_CollectHandler(manager),
        ),
        Tool(
            name="subagent_cancel",
            description="请求取消一个子任务。",
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "要取消的任务 ID",
                    },
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
            handler=_CancelHandler(manager),
        ),
    ]
