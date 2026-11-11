"""Subagent 工具适配层（17D 阶段）。

将 SubagentManager 的能力包装为 Tool，供父 Agent 调用。

首版闭环的关键约定：
- **同步等待**：``subagent_submit`` 提交后**等待**子任务完成再返回结果，
  主 Agent 因此在一次工具调用里就拿到子 Agent 的产出，无需再调 collect。
  等待期间**响应父取消**：父任务被停止时调用 ``Manager.cancel``，
  子任务随即在自己的 token 检查点退出（取消边界见下）。
- **权限取交集**：子任务的有效权限 = 父任务实际权限 ∩ 服务端只读上限
  ∩ 子执行主体策略。计算逻辑在 ``subagent_policy``，模型无法影响。
- **结果落库**：子任务记录写进父线程的 ``ThreadState.subagent_runs``，
  页面刷新后仍能读出（G6）。

取消边界的**准确描述**：取消令牌是**协作式**的 —— 子代理在模型调用前、
工具调用前检查令牌。因此取消能阻止**后续**的模型／工具调用，但**不能证明
进行中的调用已被强制中断**。文案与测试都按这个口径写。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from mini_deerflow.middleware import Budget, ChildBudget, build_child_budget
from mini_deerflow.subagent_executor import CancellationToken, SubagentBudget, SubagentManager
from mini_deerflow.subagent_models import (
    SubagentContextPolicy,
    SubagentResult,
    TaskStatus,
)
from mini_deerflow.subagent_policy import (
    compute_child_capabilities,
    compute_child_tools,
)
from mini_deerflow.subagent_runs import (
    append_subagent_run,
    build_subagent_run_record,
)
from mini_deerflow.tools import Tool, ToolRegistry

if TYPE_CHECKING:
    from mini_deerflow.models import Model


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


def _extract_message_anchor(context: object | None) -> int | None:
    """从执行上下文里取"过程块应插在第几条消息之后"的锚点。

    Agent.run 在构造工具调用的 ExecutionContext 时把锚点放进
    ``metadata['message_anchor']``（= 本轮 user 消息之前的历史条数 + 1）。
    这里是唯一的读取点：拿不到就返回 None，由调用方走兜底逻辑。
    """
    if context is None:
        return None
    metadata = getattr(context, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("message_anchor")
    # bool 是 int 子类，必须排除
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


# --- Handler 实现 ---


class _SubmitHandler:
    """subagent_submit handler。

    一次调用完成完整闭环：派发 → 子 Agent 独立执行 → 结果回传 → 记录落库。
    """
    __slots__ = (
        "_manager", "_parent_run_id", "_parent_thread_id", "_depth",
        "_thread_store", "_model", "_registry", "_parent_tool_names",
    )

    def __init__(
        self,
        manager: SubagentManager,
        parent_run_id: str,
        parent_thread_id: str,
        depth: int = 1,
        thread_store: "InMemoryThreadStore | None" = None,
        model: "Model | None" = None,
        registry: "ToolRegistry | None" = None,
        parent_tool_names: tuple[str, ...] | None = None,
    ) -> None:
        self._manager = manager
        self._parent_run_id = parent_run_id
        self._parent_thread_id = parent_thread_id
        self._depth = depth
        # TOOL-02：子代理与父共享 ThreadStore（用于反查 project_id）
        self._thread_store = thread_store
        # 子任务执行依赖（模型 / 工具注册表）：由 submit 调用带入，
        # 由 Manager 在 worker 线程里装配成 SubagentRunner。
        # 不传则沿用 Manager 默认（FakeModel + 空注册表）。
        self._model = model
        self._registry = registry
        # 父任务本次运行**真正可见**的工具名。子任务工具集 =
        # 父可见 ∩ 服务端只读上限（见 subagent_policy.compute_child_tools）。
        # None 表示不限制父侧（取交集时父侧视为全集）。
        self._parent_tool_names = parent_tool_names

    def __call__(self, arguments: dict[str, object], context: object | None = None) -> str:
        tool_name = "subagent_submit"
        try:
            if not isinstance(arguments, dict):
                raise SubagentToolError("arguments", "必须是 dict")

            # TOOL-02（R2a-3-run）：从 context 提取父 Agent 的 run_id 和 thread_id（而非 hardcoded）
            effective_parent_run_id = self._parent_run_id
            effective_parent_thread_id = self._parent_thread_id
            if context is not None:
                if hasattr(context, "run_id"):
                    ctx_run_id = getattr(context, "run_id")
                    if ctx_run_id:
                        effective_parent_run_id = ctx_run_id
                if hasattr(context, "thread_id"):
                    ctx_thread_id = getattr(context, "thread_id")
                    if ctx_thread_id:
                        effective_parent_thread_id = ctx_thread_id

            # TOOL-02：从执行上下文提取父 Agent 已授予的能力，传递给子任务
            parent_caps: frozenset = frozenset()
            if context is not None:
                if hasattr(context, "granted_capabilities"):
                    parent_caps = getattr(context, "granted_capabilities") or frozenset()

            # 权限交集（第一步）：父任务实际权限 ∩ 服务端只读上限。
            # 上限**只做减法** —— 不会因为存在上限常量就授予父任务没有的能力。
            child_caps = compute_child_capabilities(parent_caps)

            # BUDGET-01：父运行的预算（Agent.run 放进 tool_ctx 的那一个）。
            # 子代理必须与父**共享同一个**计数器 —— 子代理的模型/工具消耗
            # 真实计入父运行，父的额度上限对子代理同样生效。
            #
            # 复审 B1：必须同时接受 `Budget` 与 `ChildBudget`。带局部额度的
            # 子代理持有的是 `ChildBudget` 视图（不是 Budget 子类），只认
            # Budget 会让**继续委派**在这一层把整条父预算链丢掉 —— 孙代理
            # 的消耗从此不进根计数器。ChildBudget 作为 parent 传给
            # build_child_budget 会继续向上链到根，链条不中断。
            parent_budget: Budget | ChildBudget | None = None
            if context is not None:
                ctx_budget = getattr(context, "budget", None)
                if isinstance(ctx_budget, (Budget, ChildBudget)):
                    parent_budget = ctx_budget

            # 父任务的取消令牌：同步等待期间必须能响应父取消。
            parent_token = None
            if context is not None:
                ctx_token = getattr(context, "cancellation_token", None)
                if ctx_token is not None and hasattr(ctx_token, "is_cancelled"):
                    parent_token = ctx_token

            # TRACE-01：父侧工具 span 的 id —— 子代理的 SUBAGENT_TASK span
            # 挂在它下面，于是一次运行里的「模型 → 工具 → 子代理」是一条链。
            parent_span_id: str | None = None
            if context is not None:
                ctx_span_id = getattr(context, "span_id", None)
                if isinstance(ctx_span_id, str) and ctx_span_id:
                    parent_span_id = ctx_span_id

            # 时序锚点：子任务卡要插在父会话消息流里的**当时位置**
            # （本轮用户消息之后），而不是永远挂在会话末尾。
            #
            # 首选 Agent.run 放进 ExecutionContext.metadata 的值 —— 那是
            # "本轮 user 消息之前有多少条消息"的权威答案。拿不到时退回到
            # 父线程当前消息数 + 1（父线程里本轮 user 消息尚未落库，
            # 因此 +1 正好是它之后的位置）。
            after_message_count = _extract_message_anchor(context)
            if after_message_count is None:
                after_message_count = self._fallback_message_anchor(
                    effective_parent_thread_id
                )

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

            # TOOL-02（R2a-1）：从共享 ThreadStore 获取父线程的 project_id，继承项目会话上下文
            # 项目绑定**只从父线程反查**继承 —— 模型无法通过参数更换项目绑定。
            parent_project_id: str | None = None
            if self._thread_store is not None:
                try:
                    parent_thread = self._thread_store.get(effective_parent_thread_id)
                    parent_project_id = getattr(parent_thread, "project_id", None) or None
                except Exception:
                    pass  # 非项目会话时 project_id 为 None

            # 权限交集（第二步）：工具集 = 父可见 ∩ 服务端只读上限 ∩
            # 禁列剔除。结果写进 policy.allowed_tools —— 它在**运行时**由
            # build_allow_list / filter_tool_specs / ensure_tool_allowed 强制，
            # 不只是序列化存取。
            #
            # 父可见集按**父线程的真实身份**算：普通会话看不到 project_* 工具
            # （与父 Agent 的 allow list 同一套规则，见 compute_thread_tool_allow_list）。
            # 注意：allowed_tools **不出现在工具 schema 里**，模型无法传参
            # 扩大自己的权限。
            child_tools = compute_child_tools(
                self._parent_visible_tools(parent_project_id)
            )

            policy = SubagentContextPolicy(
                include_memory=include_memory,
                include_skill_context=include_skill,
                include_system_instruction=include_system,
                allowed_tools=child_tools,
                # 不可递归：即使模型传 allow_further_delegation=True，
                # 服务端也不给子任务继续派发的能力（child_caps 已剔除
                # SUBAGENT_DELEGATE，且子任务工具集不含 subagent_*）。
                allow_further_delegation=False,
            )

            # BUDGET-01：模型自填的局部上限**只能收紧，不能扩额**。
            # 过去这里直接造一个独立的 SubagentBudget 顶替父额度 —— 子代理
            # 的消耗完全不进父计数器，等于凭空扩出一份额度。
            # 现在：父额度始终参与计数；模型自填的上限作为**附加的更严限制**，
            # 由 ChildBudget 视图同时检查两边。
            local_budget: SubagentBudget | None = None
            if max_model_calls or max_tool_calls or (max_steps and max_steps > 0):
                local_budget = SubagentBudget(
                    # 缺省（0）表示**不设局部步数上限**，不能当成"0 步"——
                    # 否则模型只填 max_model_calls 就会把子代理直接卡死。
                    max_steps=max_steps if max_steps and max_steps > 0 else None,
                    max_model_calls=max_model_calls if max_model_calls else None,
                    max_tool_calls=max_tool_calls if max_tool_calls else None,
                )
            budget = build_child_budget(parent_budget, local_budget)

            # TOOL-02（R2a-1）：从共享 ThreadStore 获取父线程的 project_id，继承项目会话上下文
            # 项目绑定**只从父线程反查**继承 —— 模型无法通过参数更换项目绑定。
            parent_project_id: str | None = None
            if self._thread_store is not None:
                try:
                    parent_thread = self._thread_store.get(effective_parent_thread_id)
                    parent_project_id = getattr(parent_thread, "project_id", None) or None
                except Exception:
                    pass  # 非项目会话时 project_id 为 None

            task = self._manager.submit(
                instruction=instruction,
                parent_run_id=effective_parent_run_id,
                parent_thread_id=effective_parent_thread_id,
                depth=self._depth,
                timeout_seconds=timeout if timeout else None,
                budget=budget,
                model=self._model,
                registry=self._registry,
                context_policy=policy,
                # 子任务继承**收窄后**的能力（父 ∩ 上限），不是父的全部能力。
                parent_granted_capabilities=child_caps,
                parent_project_id=parent_project_id,
                # TRACE-01：子代理 span 的父节点
                parent_span_id=parent_span_id,
            )

            # 首版闭环：提交后**同步等待**子任务完成，父 Agent 在这一次
            # 工具调用里就拿到结果。等待期间响应父取消。
            result = self._await_result(
                task.task_id,
                timeout=timeout if timeout else None,
                parent_token=parent_token,
            )

            record = self._build_record(task, result, after_message_count=after_message_count)
            self._persist(effective_parent_thread_id, record)

            if result is None:
                # 等待超时：子任务可能仍在跑，如实告知，不谎称成功。
                return _ok(
                    task_id=task.task_id,
                    status=task.status.name,
                    collected=False,
                    reason="等待子任务超时，子任务可能仍在后台运行",
                )

            return _ok(
                task_id=result.task_id,
                status=result.terminal_status.name,
                output=result.output,
                succeeded=result.succeeded,
                duration=result.duration,
                error_type=result.error_type,
                error_message=result.error_message,
            )

        except SubagentToolError:
            raise
        except Exception as e:
            return _error(tool_name, type(e).__name__, str(e))

    def _parent_visible_tools(self, parent_project_id: str | None) -> tuple[str, ...]:
        """父任务本次运行真正可见的工具名。

        这是权限交集的**父侧**：子任务能用什么，先得是父能用的。

        计算方式与父 Agent 的 allow list **同一套规则**
        （``compute_thread_tool_allow_list``）：普通会话看不到 ``project_*``
        工具，项目会话可见全部。父侧基数取自构造时传入的
        ``parent_tool_names``（通常是注册表全量），再按会话身份收窄。
        """
        base = self._parent_tool_names
        if base is None:
            # 未提供父侧基数（直接构造 handler 的场景）：
            # 用注册表当前的全部工具。
            if self._registry is None:
                return ()
            base = tuple(t.name for t in self._registry.list_tools())
        if parent_project_id is None:
            # 普通会话：父看不到 project_* 工具
            return tuple(n for n in base if not n.startswith("project_"))
        return base

    def _await_result(
        self,
        task_id: str,
        *,
        timeout: float | None,
        parent_token: Any | None,
    ) -> SubagentResult | None:
        """等待子任务完成；父取消时**取消子任务**并返回其终态。

        返回 None 表示等待超时（子任务可能仍在跑）。

        取消边界（准确口径）：``Manager.cancel`` 置位子任务自己的令牌，
        子代理在**模型调用前 / 工具调用前**检查它 —— 因此取消能阻止
        **后续**调用，但不能证明**进行中**的调用已被强制中断。
        """
        deadline = (
            time.monotonic() + timeout if timeout and timeout > 0 else None
        )
        while True:
            # 父已取消 → 立刻取消子任务，不让它继续跑
            if parent_token is not None and parent_token.is_cancelled():
                self._manager.cancel(task_id)
                # 给子任务一点时间收口（它会在下一个检查点退出）
                return self._collect_with_grace(task_id)

            status = self._manager.get_status(task_id)
            if status in {
                TaskStatus.SUCCEEDED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
                TaskStatus.TIMED_OUT,
            }:
                return self._collect_now(task_id)

            if deadline is not None and time.monotonic() >= deadline:
                return None

            # 短轮询：让出 GIL，让子任务线程推进
            time.sleep(0.05)

    def _collect_now(self, task_id: str) -> SubagentResult | None:
        try:
            return self._manager.collect(task_id, timeout=0)
        except (KeyError, TimeoutError):
            return None

    def _collect_with_grace(self, task_id: str) -> SubagentResult | None:
        """取消后短暂等待子任务收口，取它的终态。"""
        grace_deadline = time.monotonic() + 2.0
        while time.monotonic() < grace_deadline:
            result = self._collect_now(task_id)
            if result is not None:
                return result
            time.sleep(0.05)
        return self._collect_now(task_id)

    def _build_record(
        self,
        task: Any,
        result: SubagentResult | None,
        *,
        after_message_count: int | None = None,
    ) -> dict[str, Any]:
        """构造可持久化的子任务记录。

        ``after_message_count`` 是时序锚点（见 _extract_message_anchor）：
        前端据此把卡片插到消息流里的当时位置。
        """
        if result is not None:
            status = result.terminal_status.name
            output = result.output
            error_type = result.error_type
            error_message = result.error_message
            started_at = result.started_at
            finished_at = result.finished_at
            duration = result.duration
            model_calls = result.model_calls
            tool_calls = result.tool_calls
        else:
            status = task.status.name
            output = ""
            error_type = None
            error_message = None
            started_at = getattr(task, "started_at", None)
            finished_at = getattr(task, "finished_at", None)
            duration = None
            model_calls = 0
            tool_calls = 0
        return build_subagent_run_record(
            task_id=task.task_id,
            instruction=task.instruction,
            status=status,
            output=output,
            error_type=error_type,
            error_message=error_message,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration,
            parent_run_id=task.parent_run_id,
            model_calls=model_calls,
            tool_calls=tool_calls,
            after_message_count=after_message_count,
        )

    def _fallback_message_anchor(self, parent_thread_id: str) -> int | None:
        """没有上下文锚点时的兜底：父线程消息数 + 1。

        父线程里**本轮的 user 消息尚未落库**（Agent 在最终回答时才
        一次性提交），因此"当前消息数 + 1"正好是那条 user 消息之后的位置。
        取不到父线程时返回 None（前端会退回末尾展示）。
        """
        if self._thread_store is None:
            return None
        try:
            thread = self._thread_store.get(parent_thread_id)
            return int(getattr(thread, "message_count", 0)) + 1
        except Exception:
            return None

    def _persist(self, parent_thread_id: str, record: dict[str, Any]) -> None:
        """把子任务记录写进父线程的 ThreadState。

        写失败**不影响**主流程：记录只是给人看的可追溯信息，
        不能因为落库失败就让整个 run 失败。
        """
        if self._thread_store is None:
            return
        try:
            thread = self._thread_store.get(parent_thread_id)
            append_subagent_run(thread, record)
            self._thread_store.save(thread)
        except Exception:
            pass


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
    thread_store: "InMemoryThreadStore | None" = None,
    model: "Model | None" = None,
    registry: "ToolRegistry | None" = None,
    parent_tool_names: tuple[str, ...] | None = None,
) -> list[Tool]:
    """创建 4 个 Subagent 工具。

    Args:
        manager: 子任务管理器。
        parent_run_id / parent_thread_id: 父运行的标识（工具调用时会
            从执行上下文覆盖为真实值）。
        thread_store: 共享的线程仓库 —— 用于反查项目绑定与落库子任务记录。
        model: 子任务使用的模型。**必须传入真实模型**，否则子代理会
            退化成 FakeModel（只会算数，不会真的干活）。
        registry: 子任务使用的工具注册表。
        parent_tool_names: 父任务本次运行真正可见的工具名 —— 子任务
            工具集 = 父可见 ∩ 服务端只读上限。None 表示父侧不限制。
    """
    return [
        Tool(
            name="subagent_submit",
            description=(
                "委派一个独立子任务给子 Agent 执行。"
                "子 Agent 在隔离上下文中工作，只能使用只读工具"
                "（计算、会话历史、项目只读检索），"
                "不能写文件、不能执行命令、不能继续派发子任务。"
                "本工具会等待子任务完成后返回结果。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "instruction": {
                        "type": "string",
                        "description": "子任务的指令（要独立完成的具体任务）",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "等待子任务完成的超时秒数",
                    },
                    "max_steps": {
                        "type": "integer",
                        "description": "子 Agent 最大步数",
                    },
                    "max_model_calls": {
                        "type": "integer",
                        "description": "子 Agent 最大模型调用次数",
                    },
                    "max_tool_calls": {
                        "type": "integer",
                        "description": "子 Agent 最大工具调用次数",
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
                # 注意：**没有** allowed_tools 字段 —— 子任务的工具集由
                # 服务端固定上限决定，模型不能通过参数扩大自己的权限。
                "additionalProperties": False,
            },
            handler=_SubmitHandler(
                manager,
                parent_run_id,
                parent_thread_id,
                thread_store=thread_store,
                model=model,
                registry=registry,
                parent_tool_names=parent_tool_names,
            ),
            needs_context=True,
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
