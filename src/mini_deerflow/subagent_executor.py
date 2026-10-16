"""Subagent 执行器（17B 阶段）。

提供 SubagentRunner（子任务执行上下文）和 SubagentManager（并发管理）。

设计原则：
- 不持有父 Agent 的可变状态
- 子任务使用独立工作消息列表
- 结果通过 SubagentResult 回收
- 不修改父 ThreadState
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, Future
from typing import TYPE_CHECKING, Callable

from mini_deerflow.agent import Agent
from mini_deerflow.models import FakeModel, FinalAnswer, Message, Model, ToolCall
from mini_deerflow.tool_contracts import ExecutorPrincipal

if TYPE_CHECKING:
    # 仅用于类型注解：运行时不导入，避免把上下文工程层拖进 Subagent 的导入链
    from mini_deerflow.context_assembler import ContextAssembler
from mini_deerflow.subagent_models import (
    SubagentContextPolicy,
    SubagentResult,
    SubagentTask,
    TaskStatus,
)
from mini_deerflow.thread_state import ThreadState
from mini_deerflow.thread_store import InMemoryThreadStore, ThreadStore
from mini_deerflow.tools import (
    Tool,
    ToolRegistry,
    build_allow_list,
    ensure_tool_allowed,
    filter_tool_specs,
)
from mini_deerflow.tool_authorization import ToolPolicyRegistry


# --- Budget ---
# 旧 SubagentBudget 类已合并到 middleware.Budget（项目唯一 Budget 实现）。
# 保留 SubagentBudget 名称作为兼容别名：所有属性与 Budget 等价。

from mini_deerflow.middleware import (  # noqa: E402, F401
    Budget as SubagentBudget,
    BudgetExceededError,
)


# --- Cancellation Token ---

class CancellationToken:
    """协作取消令牌。线程安全。"""
    __slots__ = ("_cancelled", "_lock")

    def __init__(self) -> None:
        self._cancelled = False
        self._lock = threading.Lock()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True

    def is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled


# --- SubagentRunner ---

@dataclass
class SubagentRunner:
    """子任务的执行上下文。

    持有独立的：
    - thread store
    - agent instance
    - 工作消息列表

    不持有父 Agent 的可变状态。
    """
    task: SubagentTask
    model: Model
    registry: ToolRegistry
    store: ThreadStore
    budget: SubagentBudget
    cancellation_token: CancellationToken
    # 用于 Trace 关联
    parent_run_id: str = ""
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    # 24：可选。传入则子代理走统一上下文工程，并在结束时回收投影状态。
    # 隔离靠 thread_id（`subagent-{task_id}`），不是靠另开一份 store。
    context_assembler: ContextAssembler | None = None
    # TOOL-02：子代理与父共享策略注册表，principal = MINI_SUBAGENT
    tool_policy_registry: "ToolPolicyRegistry | None" = None
    # TOOL-02：子任务从父 Agent 继承的已授予能力
    parent_granted_capabilities: frozenset = field(default_factory=frozenset)

    def run(self) -> SubagentResult:
        """执行子任务并返回结果。"""
        thread_id = f"subagent-{self.task.task_id}"

        try:
            # 如果还是 QUEUED（直接调用 SubagentRunner，未经过 Manager._run_task），
            # 转为 RUNNING。经过 Manager 时已在 _run_task 中转换过了。
            if self.task.status == TaskStatus.QUEUED:
                self.task.transition_to(TaskStatus.RUNNING)

            # 创建独立 Thread
            if self.store.exists(thread_id):
                self.store.delete(thread_id)
            self.store.create(thread_id)

            started_at = time.monotonic()
            if self.task.started_at is not None:
                started_at = self.task.started_at
            else:
                self.task.started_at = started_at

            # 注入取消检查到 Agent（通过包装 Model）
            wrapped_model = _CancellableModel(
                self.model,
                self.cancellation_token,
                self.budget,
            )

            # 使用 budget-aware agent；把 context_policy 里的工具白名单一并传下去，
            # 让 allowed_tools 在**运行时**真正生效（而不是只做序列化存取）。
            actual_agent = _BudgetAwareAgent(
                model=wrapped_model,
                registry=self.registry,
                store=self.store,
                max_steps=self.budget.max_steps,
                budget=self.budget,
                cancellation_token=self.cancellation_token,
                timeout_seconds=self.task.timeout_seconds,
                allowed_tools=(
                    self.task.context_policy.allowed_tools
                    if self.task.context_policy is not None
                    else None
                ),
                context_assembler=self.context_assembler,
                run_id=self.run_id,
                # TOOL-02：子代理与父共享策略注册表，principal = MINI_SUBAGENT
                tool_policy_registry=self.tool_policy_registry,
                # TOOL-02：子任务从父 Agent 继承的已授予能力
                parent_granted_capabilities=self.parent_granted_capabilities,
            )

            # 检查取消（在开始前）
            if self.cancellation_token.is_cancelled():
                return self._mark_cancelled_and_result()

            output = actual_agent.run(thread_id, self.task.instruction)

            # 检查取消（在完成后）
            if self.cancellation_token.is_cancelled():
                return self._mark_cancelled_and_result(output=output)

            self.task.transition_to(TaskStatus.SUCCEEDED)
            return SubagentResult.from_task(
                self.task,
                output=output,
                run_id=self.run_id,
            )

        except _CancelledError:
            return self._mark_cancelled_and_result()

        except _BudgetExhaustedError as e:
            self._safe_transition(TaskStatus.FAILED)
            return SubagentResult.from_task(
                self.task,
                output="",
                error_type="BudgetExhaustedError",
                error_message=str(e),
                run_id=self.run_id,
            )

        except Exception as e:
            self._safe_transition(TaskStatus.FAILED)
            return SubagentResult.from_task(
                self.task,
                output="",
                error_type=type(e).__name__,
                error_message=str(e),
                run_id=self.run_id,
            )

        finally:
            # 24：子代理生命周期结束（成功 / 取消 / 超时 / 失败）都要回收投影。
            # ThreadState 留在本 Runner 的局部 store 里，随对象一起销毁；
            # 但 ContextState 与向量索引写进了**共享** store，不显式删就成了
            # 指向已消失线程的孤儿数据。
            # 注意这只发生在整个子任务结束时，与「同一次 Run 内工具写入的
            # Note 在后续步骤失败后仍保留」并不冲突——后者指 Run 尚未结束。
            assembler = self.context_assembler
            if assembler is not None:
                try:
                    assembler.drop_thread_projections(thread_id)
                except Exception:  # 清理失败不应改变任务结果
                    pass

    def _safe_transition(self, to: TaskStatus) -> None:
        """安全地转换状态。"""
        try:
            if self.task.status in {
                TaskStatus.SUCCEEDED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
                TaskStatus.TIMED_OUT,
            }:
                return
            self.task.transition_to(to)
        except Exception:
            pass

    def _mark_cancelled_and_result(self, output: str = "") -> SubagentResult:
        """标记取消并返回结果。"""
        self._safe_transition(TaskStatus.CANCELLED)
        return SubagentResult.from_task(
            self.task,
            output=output,
            error_type="CancelledError",
            error_message="任务被取消",
            run_id=self.run_id,
        )


class _CancelledError(Exception):
    """协作取消信号。"""
    pass


class _BudgetExhaustedError(Exception):
    """预算耗尽信号。"""
    pass


class _CancellableModel:
    """包装 Model，在每次 decide 前检查取消并原子消费 model 预算。"""
    __slots__ = ("_model", "_token", "_budget")

    def __init__(self, model: Model, token: CancellationToken, budget: SubagentBudget) -> None:
        self._model = model
        self._token = token
        self._budget = budget

    def decide(self, messages: list[Message], tools=None) -> ToolCall | FinalAnswer:
        if self._token.is_cancelled():
            raise _CancelledError()
        # 在真正发起 model 调用前原子消费额度
        try:
            self._budget.consume_model_call()
        except BudgetExceededError as e:
            raise _BudgetExhaustedError(str(e)) from e
        # 必须把工具描述继续往下传：包装器若丢掉 tools，子代理的模型
        # 就看不到任何可调用工具，Agent 的工具循环会在子代理里静默失效。
        return self._model.decide(messages, tools)


class _BudgetAwareAgent:
    """Budget-aware Agent wrapper。"""
    _DEFAULT_MAX_STEPS = 10

    def __init__(
        self,
        model: Model,
        registry: ToolRegistry,
        store: ThreadStore,
        max_steps: int | None,
        budget: SubagentBudget,
        cancellation_token: CancellationToken,
        timeout_seconds: float | None = None,
        allowed_tools: tuple[str, ...] | None = None,
        context_assembler: "ContextAssembler | None" = None,
        run_id: str = "",
        # TOOL-02：子代理策略注册表（与父共享），principal = MINI_SUBAGENT
        tool_policy_registry: "ToolPolicyRegistry | None" = None,
        # TOOL-02：子任务从父 Agent 继承的已授予能力
        parent_granted_capabilities: frozenset = field(default_factory=frozenset),
    ) -> None:
        self._model = model
        self._registry = registry
        self._store = store
        # budget.max_steps 可能为 None（无限），runner 必须给出有限上限以便 step 循环终止。
        self._max_steps = max_steps if max_steps is not None else self._DEFAULT_MAX_STEPS
        self._budget = budget
        self._token = cancellation_token
        self._deadline: float | None = time.monotonic() + timeout_seconds if timeout_seconds else None
        # 工具白名单（SubagentContextPolicy.allowed_tools 的运行时形态）。
        # None = 不设限制（沿用传入的注册表）；frozenset() = 空白名单，全部禁止。
        self._allowed = build_allow_list(allowed_tools)
        # 24：与父 Agent 共用同一个 assembler 实例。隔离靠 thread_id
        # （`subagent-{task_id}`）而不是靠另一份 store——不同线程在同一个
        # ContextStore 里本来就是不同的行，反之若复制 store 才会有状态漂移风险。
        self._context_assembler = context_assembler
        # 仅用于 Trace 关联；子代理的 run_id 由 SubagentRunner 生成。
        self._run_id = run_id
        self._last_assembled: "object | None" = None
        # TOOL-02：子代理与父共享策略注册表
        self._tool_policy_registry = tool_policy_registry
        # TOOL-02：子任务从父 Agent 继承的已授予能力（由 subagent_submit handler 设置）
        self._parent_granted_capabilities: frozenset = parent_granted_capabilities
        # TOOL-02：子任务从父 Agent 继承的已授予能力（由 subagent_submit handler 设置）
        self._parent_granted_capabilities: frozenset = frozenset()

    def _check_deadline(self) -> None:
        """检查是否超时。"""
        if self._deadline is not None and time.monotonic() > self._deadline:
            raise _CancelledError()

    def run(self, thread_id: str, instruction: str) -> str:
        thread = self._store.get(thread_id)
        working_messages: list[Message] = list(thread.snapshot())
        # 本轮开始前的历史长度：其后新增的都属于本轮 pending
        history_len = len(working_messages)
        pending: list[Message] = []

        user_message = Message(role="user", content=instruction)
        working_messages.append(user_message)
        pending.append(user_message)

        # 21B：与主 Agent 同构——只把工具的**纯数据描述**交给模型，
        # 注册表（含 handler）留在本地执行。缺了它，真实模型在子代理里
        # 将看不到任何可调用工具，工具循环直接失效。
        #
        # 纵深防御第一层（暴露限制）：allowed_tools 之外的工具**根本不给模型看**。
        # 本地工具、mcp__* 工具、子代理工具一视同仁——这里按名字过滤，
        # 不区分来源，因此三类工具同时受限。
        model_tools = filter_tool_specs(self._registry.tool_specs(), self._allowed)

        for step in range(1, self._max_steps + 1):
            # 取消检查
            if self._token.is_cancelled():
                raise _CancelledError()
            # 超时检查
            self._check_deadline()
            # 步数预算
            try:
                self._budget.consume_step()
            except BudgetExceededError as e:
                raise _BudgetExhaustedError(str(e)) from e

            if self._context_assembler is not None:
                # 24：子代理走同一套上下文工程（摘要注入、按需压缩、笔记），
                # 但 thread_id 是 `subagent-{task_id}`，状态与父线程完全隔离。
                # 子代理不注入 Memory/Skill（沿用既有行为），因此两者留空。
                assembled = self._context_assembler.assemble(
                    thread_id=thread_id,
                    thread=thread,
                    pending=tuple(working_messages[history_len:]),
                    tool_specs=model_tools,
                    run_id=self._run_id,
                )
                model_messages = list(assembled.context.messages)
                self._last_assembled = assembled
            else:
                model_messages = list(working_messages)
            decision = self._model.decide(model_messages, model_tools)

            if isinstance(decision, FinalAnswer):
                assistant_message = Message(role="assistant", content=decision.content)
                pending.append(assistant_message)
                thread.extend(pending)
                self._store.save(thread)
                return decision.content

            if isinstance(decision, ToolCall):
                # 工具调用前原子消费额度（成功/失败都计入）
                try:
                    self._budget.consume_tool_call()
                except BudgetExceededError as e:
                    raise _BudgetExhaustedError(str(e)) from e

                # 21B：与主 Agent 一致——先落 tool_use（带 call_id），
                # 再落紧邻的 tool 结果，供下一轮 decide() 按 call_id 配对还原。
                # 落库的 tool 消息不带 id，持久化形状与 21B 之前一致。
                working_messages.append(Message(
                    role="assistant",
                    content=f"调用工具 {decision.name}，参数: {decision.arguments}",
                    tool_call_id=decision.id,
                    tool_name=decision.name,
                    tool_arguments=dict(decision.arguments),
                ))

                # 纵深防御第二层（执行限制）：模型可能凭上下文里的名字
                # "凭记忆"伪造一个未授权工具（暴露限制挡不住这种情况），
                # 所以执行前必须再校验一次，越权直接抛 ToolNotAllowedError。
                ensure_tool_allowed(decision.name, self._allowed)

                # TOOL-02 第三层：细粒度授权（MINI_SUBAGENT principal + 继承能力）
                if self._tool_policy_registry is not None:
                    from mini_deerflow.tool_authorization import check_tool_access
                    authority = self._tool_policy_registry.get_authority(
                        thread_id=thread_id,
                        run_id=self._run_id,
                        project_id=None,
                        granted_capabilities=self._parent_granted_capabilities,  # 子代理由父授权决定
                        principal=ExecutorPrincipal.MINI_SUBAGENT,
                    )
                    auth_decision = check_tool_access(
                        self._tool_policy_registry, decision.name, authority
                    )
                    if auth_decision.is_denied or auth_decision.is_confirm:
                        from mini_deerflow.tool_authorization import ToolAuthorizationError
                        raise ToolAuthorizationError(auth_decision)

                # TOOL-02：构造执行上下文，包含父授权能力（供 subagent_submit handler 提取并传递）
                from mini_deerflow.middleware import ExecutionContext
                tool_ctx = ExecutionContext(
                    thread_id=thread_id,
                    run_id=self._run_id,
                    granted_capabilities=self._parent_granted_capabilities,
                    cancellation_token=self._token,
                    budget=self._budget,
                )
                result = self._registry.execute(decision.name, decision.arguments, context=tool_ctx)
                # 与主 Agent 一致：工具往返只留在 working_messages（本轮喂模型），
                # **不写入 pending**——Thread 只提交 user 与最终 assistant，
                # 避免落库一条没有配对 tool_use 的孤儿 tool_result。
                working_messages.append(
                    Message(role="tool", content=result, tool_call_id=decision.id)
                )

        raise RuntimeError(f"Subagent 在 {self._max_steps} 步内未完成")


# --- SubagentManager ---

class SubagentManager:
    """Subagent 并发管理器。

    使用 ThreadPoolExecutor(max_workers=max_concurrency) 实现真正有界 worker pool。
    queued 任务不创建额外线程，只在 executor 队列中等待。

    父子预算关系：
    - 父 Agent 持有 shared_budget（与 Manager 共享的 Budget 实例）
    - submit 时 consume_subagent_task(depth) 原子扣减额度 + 验证深度
    - 子任务执行 Model/Tool 时使用同一个 budget 实例，因此所有子任务
      共享父级的总 model_calls / tool_calls 额度
    """
    def __init__(
        self,
        max_concurrency: int = 1,
        max_depth: int = 3,
        max_tasks_per_parent: int = 10,
        default_timeout: float | None = 300.0,
        default_budget: SubagentBudget | None = None,
        shared_budget: SubagentBudget | None = None,
        context_assembler: ContextAssembler | None = None,
        tool_policy_registry: "ToolPolicyRegistry | None" = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency 必须 >= 1")
        if max_depth < 0:
            raise ValueError("max_depth 不能为负")
        if max_tasks_per_parent < 1:
            raise ValueError("max_tasks_per_parent 必须 >= 1")

        self._max_concurrency = max_concurrency
        self._max_depth = max_depth
        self._max_tasks_per_parent = max_tasks_per_parent
        self._default_timeout = default_timeout
        self._default_budget = default_budget or SubagentBudget()
        # 父子共享 Budget（若提供）。submit / 子任务 Model/Tool 都会 consume 它。
        self._shared_budget = shared_budget
        # 24：所有子代理共用父级的 assembler 实例。每个子任务的
        # thread_id 不同（`subagent-{task_id}`），因此摘要、Task Notes、
        # 压缩水位与向量索引各自独立，父线程的 ContextState 不会被触碰。
        self._context_assembler = context_assembler
        # TOOL-02：子代理持有与父相同的策略注册表，principal = MINI_SUBAGENT
        self._tool_policy_registry = tool_policy_registry

        self._lock = threading.Lock()
        self._tasks: dict[str, SubagentTask] = {}
        self._results: dict[str, SubagentResult] = {}
        self._tokens: dict[str, CancellationToken] = {}
        self._started = False
        self._shutdown = False

        # ThreadPoolExecutor 是真正的 worker pool
        self._executor = ThreadPoolExecutor(max_workers=max_concurrency)
        self._futures: dict[str, Future] = {}

        # 任务计数器（用于 max_tasks_per_parent）
        self._parent_task_counts: dict[str, int] = {}

    def submit(
        self,
        instruction: str,
        parent_run_id: str,
        parent_thread_id: str,
        *,
        parent_task_id: str | None = None,
        depth: int = 1,
        timeout_seconds: float | None = None,
        budget: SubagentBudget | None = None,
        model: Model | None = None,
        registry: ToolRegistry | None = None,
        context_policy: SubagentContextPolicy | None = None,
        # TOOL-02：父 Agent 已授予子任务的能力集合
        parent_granted_capabilities: frozenset = field(default_factory=frozenset),
    ) -> SubagentTask:
        """提交一个子任务。

        若 Manager 配置了 shared_budget，会在提交时原子 consume_subagent_task(depth)：
        - 子任务数 / 深度超限时抛 BudgetExceededError
        - 子任务执行 Model/Tool 时使用同一个 shared_budget 实例
        """
        if self._shutdown:
            raise RuntimeError("Manager 已关闭，不能提交新任务")

        # 父子共享 Budget：原子扣减 + 深度校验
        if self._shared_budget is not None:
            self._shared_budget.consume_subagent_task(depth)

        # 深度检查（向后兼容，即使没有 shared_budget 也允许 Manager 内部 max_depth 限制）
        if depth > self._max_depth:
            raise ValueError(
                f"子任务深度 {depth} 超过上限 {self._max_depth}"
            )

        # 父任务子任务数检查
        parent_key = parent_task_id or parent_run_id
        with self._lock:
            count = self._parent_task_counts.get(parent_key, 0)
            if count >= self._max_tasks_per_parent:
                raise ValueError(
                    f"父任务 {parent_key} 的子任务数已达上限 {self._max_tasks_per_parent}"
                )
            self._parent_task_counts[parent_key] = count + 1

        task_id = f"sa-{uuid.uuid4().hex[:12]}"
        created_at = time.monotonic()

        task = SubagentTask(
            task_id=task_id,
            parent_run_id=parent_run_id,
            parent_thread_id=parent_thread_id,
            instruction=instruction,
            status=TaskStatus.QUEUED,
            created_at=created_at,
            parent_task_id=parent_task_id,
            depth=depth,
            timeout_seconds=timeout_seconds or self._default_timeout,
            context_policy=context_policy,
            # TOOL-02：父 Agent 已授予子任务的能力集合
            parent_granted_capabilities=parent_granted_capabilities,
        )

        token = CancellationToken()

        with self._lock:
            self._tasks[task_id] = task
            self._tokens[task_id] = token

        # 提交到 ThreadPoolExecutor（不是每任务一线程）
        future = self._executor.submit(
            self._run_task,
            task,
            token,
            model,
            registry,
            budget,
        )
        with self._lock:
            self._futures[task_id] = future

        return task

    def _run_task(
        self,
        task: SubagentTask,
        token: CancellationToken,
        model: Model | None,
        registry: ToolRegistry | None,
        budget: SubagentBudget | None,
    ) -> None:
        """在 worker 线程中执行任务。"""
        # 先在锁内做状态确认和转换，释放锁后再处理取消分支（避免重入死锁）
        should_cancel = False
        task_obj: SubagentTask | None = None
        with self._lock:
            task_obj = self._tasks.get(task.task_id)
            if task_obj is None:
                return
            if self._tokens[task_obj.task_id].is_cancelled():
                should_cancel = True
            else:
                # 转换到 RUNNING
                task_obj.transition_to(TaskStatus.RUNNING)

        if should_cancel:
            self._mark_task_cancelled(task.task_id)
            return

        try:
            # 父子共享 Budget 优先：所有 Model/Tool 调用都受其约束
            effective_budget = budget or self._shared_budget or self._default_budget
            # 创建执行器并运行
            runner = SubagentRunner(
                task=task_obj,
                model=model or FakeModel(),
                registry=registry or ToolRegistry(),
                store=InMemoryThreadStore(),
                budget=effective_budget,
                cancellation_token=token,
                parent_run_id=task.parent_run_id,
                context_assembler=self._context_assembler,
                # TOOL-02：子代理与父共享策略注册表
                tool_policy_registry=self._tool_policy_registry,
                # TOOL-02：子任务从父 Agent 继承的已授予能力
                parent_granted_capabilities=task.parent_granted_capabilities,
            )

            result = runner.run()

            with self._lock:
                self._tasks[task.task_id] = runner.task
                self._results[task.task_id] = result

        except Exception as e:
            with self._lock:
                task_obj_ref = self._tasks.get(task.task_id) if task_obj is None else task_obj
                if task_obj_ref and task_obj_ref.status in {
                    TaskStatus.QUEUED,
                    TaskStatus.RUNNING,
                }:
                    try:
                        task_obj_ref.transition_to(TaskStatus.FAILED)
                    except Exception:
                        pass
                if task.task_id not in self._results:
                    self._results[task.task_id] = SubagentResult.from_task(
                        task_obj_ref or task,
                        output="",
                        error_type=type(e).__name__,
                        error_message=str(e),
                    )

    def get_status(self, task_id: str) -> TaskStatus:
        """查询任务状态。未知任务抛出 KeyError。"""
        with self._lock:
            return self._tasks[task_id].status

    def list_tasks(self) -> list[SubagentTask]:
        """列出所有任务（快照）。"""
        with self._lock:
            return list(self._tasks.values())

    def collect(self, task_id: str, timeout: float = 0) -> SubagentResult:
        """回收任务结果。

        timeout=0：立即返回（未完成则抛出 KeyError）
        timeout>0：等待最多 timeout 秒
        """
        deadline: float | None = time.monotonic() + timeout if timeout > 0 else None

        while True:
            with self._lock:
                if task_id not in self._results:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError(f"等待结果超时: {task_id}")
                    # 未完成，继续等待
                    pass
                else:
                    return self._results[task_id]

            if deadline is None:
                raise KeyError(f"任务未完成: {task_id}")

            # 短暂等待后重试
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"等待结果超时: {task_id}")
            time.sleep(min(0.1, remaining))

    def collect_many(self, task_ids: list[str], timeout: float = 0) -> dict[str, SubagentResult]:
        """批量回收结果。"""
        results: dict[str, SubagentResult] = {}
        deadline: float | None = time.monotonic() + timeout if timeout > 0 else None

        for task_id in task_ids:
            while True:
                with self._lock:
                    if task_id in self._results:
                        results[task_id] = self._results[task_id]
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                remaining = (deadline - time.monotonic()) if deadline else 0.1
                time.sleep(min(0.1, max(0, remaining)))
                if deadline and time.monotonic() >= deadline:
                    break

        return results

    def cancel(self, task_id: str) -> bool:
        """请求取消任务。返回是否成功接受取消请求。"""
        with self._lock:
            if task_id not in self._tasks:
                return False
            status = self._tasks[task_id].status
            if status not in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                return False
            self._tokens[task_id].cancel()
            return True

    def shutdown(self, cancel_pending: bool = True) -> None:
        """关闭管理器。

        关闭 ThreadPoolExecutor（不再创建新线程）。running 任务通过 cancel_pending
        协作取消，queue 中的任务随 executor.shutdown 一并清除。
        """
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True

        if cancel_pending:
            with self._lock:
                for task_id, task in self._tasks.items():
                    if task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                        self._tokens[task_id].cancel()

        # 关闭 executor。wait=True 等所有 running 任务结束（协作取消后会快速退出）
        self._executor.shutdown(wait=True)

    def _store_result(self, result: SubagentResult) -> None:
        with self._lock:
            self._results[result.task_id] = result

    def _mark_task_cancelled(self, task_id: str) -> None:
        with self._lock:
            if task_id not in self._tasks:
                return
            task = self._tasks[task_id]
            if task.status in {
                TaskStatus.SUCCEEDED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
                TaskStatus.TIMED_OUT,
            }:
                return  # 已是终态
            try:
                task.transition_to(TaskStatus.CANCELLED)
            except Exception:
                pass
            self._results[task_id] = SubagentResult.from_task(
                task,
                output="",
                error_type="CancelledError",
                error_message="任务被取消",
            )
