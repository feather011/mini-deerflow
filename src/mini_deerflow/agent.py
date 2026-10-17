"""Agent：驱动模型与工具之间的调用循环，基于 ThreadStore 维护会话状态。"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import TYPE_CHECKING, Callable, Iterator, TypeVar

from mini_deerflow.context_assembler import AssembledContext, ContextAssembler
from mini_deerflow.memory_manager import MemoryProcessor
from mini_deerflow.memory_context import MemoryContextProvider
from mini_deerflow.middleware import (
    Budget,
    BudgetExceededError,
    CancellationToken,
    ExecutionContext,
    MiddlewarePipeline,
    OperationType,
)
from mini_deerflow.models import FinalAnswer, Message, Model, ToolCall
from mini_deerflow.skill_runtime import SkillContextProvider
from mini_deerflow.thread_store import ThreadStore
from mini_deerflow.tools import (
    ToolRegistry,
    ToolAllowList,
    filter_tool_specs,
    ensure_tool_allowed,
)

from mini_deerflow.tool_authorization import (
    ToolPolicyRegistry,
    check_tool_access,
    ToolAuthorizationError,
)
from mini_deerflow.tool_contracts import ToolCapability, ExecutorPrincipal

T = TypeVar("T")

PROJECT_TOOL_PREFIX = "project_"


def compute_thread_tool_allow_list(
    registry: ToolRegistry,
    project_id: str | None,
) -> ToolAllowList:
    """返回给定项目会话允许的工具名集合。

    - 普通会话（project_id=None）：只允许不带 project_ 前缀的工具
    - 项目会话（project_id 存在）：允许全部工具
    """
    if project_id is None:
        # 普通会话：过滤掉所有 project_* 工具
        return frozenset(
            spec.name
            for spec in registry.tool_specs()
            if not spec.name.startswith(PROJECT_TOOL_PREFIX)
        )
    # 项目会话：允许全部工具（由工具内部的 workspace 边界检查负责隔离）
    return None

# 项目会话提示词：当处于项目会话时，此文本注入到 skill_context 中
PROJECT_SESSION_PROMPT = """项目会话

这是一个项目会话。绑定仓库内的文件可通过以下工具访问：

- **只读检索**：project_read_file / project_glob / project_grep
  （仓库内相对路径，不允许绝对路径或 '..' 越界）
- **编码执行**（启用 Harness 时可用）：project_execute
  传入 task 描述，由 harnessagent 在仓库内执行；可以写文件、运行命令、修改代码。
  已有改动会被保留；不自动提交或推送。

主 Agent 负责讨论、澄清、派发和汇总；具体写文件/改代码/运行命令交给 harness。
模型不需要自己模拟执行结果——成功与否以 tool 返回值为准。
"""


class ProjectSessionSkillContextProvider:
    """项目会话 Skill 上下文提供者。

    检测当前是否为项目会话（thread_id 对应项目存在），若是则返回
    PROJECT_SESSION_PROMPT，否则返回空字符串。

    通常与其他 SkillContextProvider 组合使用（拼接）。
    """

    def __init__(
        self,
        thread_store: ThreadStore,
        project_store: "ProjectStore",
    ) -> None:
        self._thread_store = thread_store
        self._project_store = project_store

    def build(self, user_input: str, thread_id: str = "") -> str:
        """根据 thread_id 检测是否为项目会话，返回对应提示词。"""
        if not thread_id:
            return ""
        try:
            thread = self._thread_store.get(thread_id)
        except Exception:
            return ""
        if not thread.project_id:
            return ""
        try:
            self._project_store.get(thread.project_id)
            return PROJECT_SESSION_PROMPT
        except Exception:
            return ""


from mini_deerflow.project_store import ProjectStore


class Agent:
    """Agent 通过依赖注入接收 Model、ToolRegistry 和 ThreadStore，不依赖具体实现。"""

    def __init__(
        self,
        model: Model,
        registry: ToolRegistry,
        store: ThreadStore,
        max_steps: int = 10,
        context_provider: MemoryContextProvider | None = None,
        memory_processor: MemoryProcessor | None = None,
        skill_context_provider: SkillContextProvider | None = None,
        middleware_pipeline: MiddlewarePipeline | None = None,
        budget: Budget | None = None,
        context_assembler: ContextAssembler | None = None,
        model_timeout_seconds: float = 0.0,
        tool_policy_registry: "ToolPolicyRegistry | None" = None,
        run_principal: ExecutorPrincipal = ExecutorPrincipal.MAIN_AGENT,
    ):
        self._model = model
        self._registry = registry
        self._store = store
        self._max_steps = max_steps
        self._context_provider = context_provider
        self._memory_processor = memory_processor
        self._skill_context_provider = skill_context_provider
        self._middleware_pipeline = middleware_pipeline
        self._budget = budget
        self._context_assembler = context_assembler
        self._last_assembled: "AssembledContext | None" = None
        self._model_timeout_seconds = model_timeout_seconds
        # TOOL-02：可选细粒度授权策略注册表。None 时回退到 ensure_tool_allowed 白名单检查。
        self._tool_policy_registry = tool_policy_registry
        # TOOL-02：执行主体的身份（MAIN_AGENT / MINI_SUBAGENT 等）
        self._run_principal = run_principal

    @property
    def last_assembled(self) -> "AssembledContext | None":
        """最近一次 assemble() 的结果，供 IntegratedAgent 读取快照用。"""
        return self._last_assembled

    def _run_via_pipeline(
        self,
        ctx: ExecutionContext,
        action: Callable[[], T],
    ) -> T:
        """通过 MiddlewarePipeline 执行（含 RetryAgainError 重试）。

        没有 pipeline 时直接执行 action()。
        """
        if self._middleware_pipeline is None:
            return action()
        return self._middleware_pipeline.execute_with_retry(ctx, action)

    def run(
        self, thread_id: str, user_input: str, run_id: str = "",
        cancellation_token: CancellationToken | None = None,
    ) -> tuple[str, list[dict]]:
        """对指定 thread_id 执行一轮 Agent 循环；成功后原子写入，失败时 Thread 不变。

        Args:
            run_id: 上层 Run 的标识，仅用于 Trace 关联上下文组装与压缩事件。
                缺省为空串，保持既有调用方兼容。
            cancellation_token: 可选取消令牌；主任务停止时置位，Agent 在每步开始检查。
        """
        thread = self._store.get(thread_id)  # ThreadNotFoundError 原样传播

        # 25：计算本轮允许的工具集（暴露限制 + 执行限制的基础）
        allowed = compute_thread_tool_allow_list(self._registry, thread.project_id)

        # 构建上下文消息（只调用一次 context_provider.build()）
        memory_context_messages: list[Message] = []
        memory_context_text = ""
        if self._context_provider is not None:
            memory_context = self._context_provider.build()

            if not isinstance(memory_context, str):
                raise TypeError(
                    "MemoryContextProvider.build() 必须返回字符串"
                )

            memory_context = memory_context.strip()
            memory_context_text = memory_context
            if memory_context:
                memory_context_messages.append(
                    Message(role="system", content=memory_context)
                )

        # 构建 Skill 上下文（只调用一次 skill_context_provider.build()）
        skill_context_messages: list[Message] = []
        skill_context_text = ""
        if self._skill_context_provider is not None:
            skill_context = self._skill_context_provider.build(user_input, thread_id)

            if not isinstance(skill_context, str):
                raise TypeError(
                    "SkillContextProvider.build() 必须返回字符串"
                )

            skill_context = skill_context.strip()
            skill_context_text = skill_context
            if skill_context:
                skill_context_messages.append(
                    Message(role="system", content=skill_context)
                )

        # 读取历史为快照；working_messages 是本轮累计（喂给模型），pending 是待提交的本轮增量
        working_messages: list[Message] = list(thread.snapshot())
        # 本轮开始前的历史长度：working_messages 中该位置之后的都属「本轮 pending」
        history_len = len(working_messages)
        pending: list[Message] = []

        user_message = Message(role="user", content=user_input)
        working_messages.append(user_message)
        pending.append(user_message)

        # 21B：本轮可用工具的**纯数据描述**。注册表整体不外传——
        # Tool 带 handler（可执行对象），交给模型适配层等于把可执行能力泄漏到传输层。
        # 25：按会话权限过滤（暴露限制）。
        model_tools = filter_tool_specs(self._registry.tool_specs(), allowed)

        # TOOL-02：策略可见性过滤——DENY 工具隐藏，ALLOW/CONFIRM 可见
        # 用当前会话默认能力做可见性判定：模型不应看到自己无权调用的工具
        if self._tool_policy_registry is not None:
            # 根据会话类型构造默认能力
            if thread.project_id is None:
                session_caps = frozenset({
                    ToolCapability.COMPUTE,
                    ToolCapability.SESSION_HISTORY_READ,
                    ToolCapability.SESSION_STATE_WRITE,
                })
                session_project_id: str | None = None
            else:
                session_caps = frozenset({
                    ToolCapability.PROJECT_READ,
                    ToolCapability.PROJECT_EXEC_DELEGATE,
                    ToolCapability.COMPUTE,
                    ToolCapability.SESSION_HISTORY_READ,
                    ToolCapability.SESSION_STATE_WRITE,
                })
                session_project_id = thread.project_id
            visible_names: set[str] = set()
            for spec in model_tools:
                authority = self._tool_policy_registry.get_authority(
                    thread_id=thread_id,
                    run_id=run_id or "",
                    project_id=session_project_id,
                    granted_capabilities=session_caps,
                )
                decision = check_tool_access(self._tool_policy_registry, spec.name, authority)
                # DENY 隐藏；ALLOW/CONFIRM 均可见（CONFIRM 需用户确认，但不屏蔽工具）
                if decision.is_denied:
                    continue
                visible_names.add(spec.name)
            model_tools = [s for s in model_tools if s.name in visible_names]

        trace: list[dict] = []

        for step in range(1, self._max_steps + 1):
            # 步骤预算：每步开始前扣减，超限抛 BudgetExceededError
            if self._budget is not None:
                self._budget.consume_step()

            # 停止检查：主任务停止时在步骤开始前退出
            if cancellation_token is not None and cancellation_token.is_cancelled():
                raise BudgetExceededError("steps", self._budget.max_steps)
            # 组合发送给模型的完整消息列表：Memory → Skill → history → user
            # 每次循环重新组合以包含新增的 tool messages
            if self._context_assembler is not None:
                # 24：统一入口负责计量、注入摘要/笔记、按需压缩。
                # pending = working_messages 中本轮新增的部分（user + 工具往返）
                assembled = self._context_assembler.assemble(
                    thread_id=thread_id,
                    thread=thread,
                    pending=tuple(working_messages[history_len:]),
                    memory_context=memory_context_text,
                    skill_context=skill_context_text,
                    tool_specs=model_tools,
                    run_id=run_id,
                )
                model_messages = list(assembled.context.messages)
                self._last_assembled = assembled
            else:
                model_messages = [
                    *memory_context_messages,
                    *skill_context_messages,
                    *working_messages,
                ]

            # Model call：通过 middleware_pipeline 包装（原子 consume 预算）
            # 25C: 使用 stream_decide() 实现 token 流式推送
            token_accumulator: list[str] = []
            model_ctx = ExecutionContext(
                run_id="",  # 由 IntegratedAgent 在外层生成
                thread_id=thread_id,
                operation=OperationType.MODEL_CALL,
                operation_name="model_decide",
                budget=self._budget,
                token_accumulator=token_accumulator,
            )
            def _do_model_call(
                _msgs=model_messages,
                _tools=model_tools,
                _acc=token_accumulator,
            ) -> ToolCall | FinalAnswer:
                # 25C: 如果模型实现了 stream_decide()，用流式路径；
                # 否则用普通 decide()（FakeModel 等不支持流式的模型）
                if hasattr(self._model, "stream_decide"):
                    return _do_model_call_stream(_msgs, _tools, _acc)
                else:
                    result = self._model.decide(_msgs, _tools)
                    _acc.append(result.content if isinstance(result, FinalAnswer) else "")
                    return result

            def _do_model_call_stream(
                _msgs: list[Message],
                _tools: list[ToolSpec] | None,
                _acc: list[str],
            ) -> ToolCall | FinalAnswer:
                # 真正发起调用前原子消费额度（成功/失败/Retry 每次都计）
                if self._budget is not None:
                    self._budget.consume_model_call()

                if self._model_timeout_seconds > 0 and cancellation_token is not None:
                    timer = threading.Timer(
                        self._model_timeout_seconds,
                        cancellation_token.cancel,
                    )
                    timer.start()
                    try:
                        return _consume_stream(_msgs, _tools, _acc)
                    finally:
                        timer.cancel()
                else:
                    return _consume_stream(_msgs, _tools, _acc)

            def _consume_stream(
                _msgs: list[Message],
                _tools: list[ToolSpec] | None,
                _acc: list[str],
            ) -> ToolCall | FinalAnswer:
                result: ToolCall | FinalAnswer | None = None
                for event in self._model.stream_decide(_msgs, _tools):  # type: ignore[union-attr]
                    etype, evalue = event
                    if etype == "token":
                        _acc.append(evalue)  # type: ignore[arg-type]
                    elif etype == "stop":
                        pass
                    elif etype == "decision":
                        result = evalue  # type: ignore[assignment]
                if result is None:
                    # 流结束但没有决策（理论上不应该发生）
                    return FinalAnswer(content="".join(_acc))
                return result

            # 模型调用返回后检查是否因超时/停止而被取消
            if cancellation_token is not None and cancellation_token.is_cancelled():
                raise BudgetExceededError(
                    "model_call_timeout",
                    None,
                )
            decision = self._run_via_pipeline(model_ctx, _do_model_call)

            if isinstance(decision, FinalAnswer):
                assistant_message = Message(role="assistant", content=decision.content)
                pending.append(assistant_message)
                trace.append({
                    "step": step,
                    "action": "final_answer",
                    "detail": decision.content,
                })
                # 原子提交：先全部 extend，再 save
                thread.extend(pending)
                self._store.save(thread)

                # 成功后处理记忆（只处理本轮 user Message）
                if self._memory_processor is not None:
                    self._process_memory(thread_id, user_message, trace)

                return decision.content, trace

            if isinstance(decision, ToolCall):
                tool_description = (
                    f"调用工具 {decision.name}，参数: {decision.arguments}"
                )
                trace.append({
                    "step": step,
                    "action": "model_decision",
                    "detail": tool_description,
                })

                # 21B：先把模型发出的 tool_use 还原成一条 assistant 消息，
                # 与下面的 tool 结果**按 call_id 配对**。缺了这条，本轮
                # decide() 无法重建 tool_use/tool_result，供应商会拒收该请求。
                #
                # **只进 working_messages**（本轮喂给模型）：pending（落库）
                # 只保留 user 与最终 assistant，工具往返不进 ThreadState，
                # 详见下方 tool 结果处的注释。
                working_messages.append(Message(
                    role="assistant",
                    content=tool_description,
                    tool_call_id=decision.id,
                    tool_name=decision.name,
                    tool_arguments=dict(decision.arguments),
                ))

                # Tool call：通过 middleware_pipeline 包装（含 budget 检查/记录）
                # R1 round 4：tool_ctx.run_id 取 Agent.run 入口的 run_id，
                # 保证确认请求绑定的 run_id 与 IntegratedAgent/AgentRunService 一致。
                tool_ctx = ExecutionContext(
                    run_id=run_id or "",
                    thread_id=thread_id,
                    operation=OperationType.TOOL_CALL,
                    operation_name=decision.name,
                    budget=self._budget,
                    cancellation_token=cancellation_token,
                )
                def _do_tool_call(
                    _name=decision.name,
                    _args=decision.arguments,
                    _ctx=tool_ctx,
                ) -> str:
                    # 25：三层纵深防御
                    # 第一层：确保工具已注册（未知名 → ToolNotFoundError）
                    self._registry.get(_name)  # KeyError → ToolNotFoundError
                    # 第二层：确保工具在允许清单内（已注册但越权 → ToolNotAllowedError）
                    ensure_tool_allowed(_name, allowed)
                    # TOOL-02 第三层：细粒度授权策略（能力/主体/确认）
                    # 仅当注册了 policy_registry 时启用；None 时回退到前两层
                    if self._tool_policy_registry is not None:
                        # 根据会话类型授予默认基础能力
                        if thread.project_id is None:
                            # 普通会话：calculator / history / task_note
                            default_caps = frozenset({
                                ToolCapability.COMPUTE,
                                ToolCapability.SESSION_HISTORY_READ,
                                ToolCapability.SESSION_STATE_WRITE,
                            })
                        else:
                            # 项目会话：项目读写 + 普通会话基础能力
                            default_caps = frozenset({
                                ToolCapability.PROJECT_READ,
                                ToolCapability.PROJECT_EXEC_DELEGATE,
                                ToolCapability.COMPUTE,
                                ToolCapability.SESSION_HISTORY_READ,
                                ToolCapability.SESSION_STATE_WRITE,
                            })
                        authority = self._tool_policy_registry.get_authority(
                            thread_id=thread_id,
                            run_id=getattr(_ctx, "run_id", "") or "",
                            project_id=thread.project_id,
                            granted_capabilities=default_caps,
                            principal=self._run_principal,
                        )
                        auth_decision = check_tool_access(self._tool_policy_registry, _name, authority)
                        if auth_decision.is_denied:
                            raise ToolAuthorizationError(auth_decision)
                        if auth_decision.is_confirm:
                            # R1 round 4：使用真实 run_id 创建 pending，挂起等待 API 决议
                            # - run_id 取自当前 ExecutionContext（与运行入口一致）
                            # - 等待 outcome 决定返回真实工具结果或合成错误
                            cm = getattr(self, "_confirmation_manager", None)
                            runtime_obj = getattr(self, "_runtime", None)
                            if cm is not None:
                                from mini_deerflow.tool_contracts import (
                                    ExecutorPrincipal as _EP,
                                )
                                principal = _EP[
                                    authority.principal.name
                                ] if hasattr(authority.principal, "name") else authority.principal
                                # R1 round 4：run_id 贯通——优先取 ExecutionContext
                                # 的 run_id（与 IntegratedAgent.run 传入的一致），
                                # 回退到 authority.run_id（per-tool-call 构造），
                                # 最后才用 "main"（向后兼容）。
                                real_run_id = (
                                    getattr(_ctx, "run_id", None)
                                    or authority.run_id
                                    or "main"
                                )
                                pending = cm.create(
                                    run_id=real_run_id,
                                    thread_id=thread_id,
                                    project_id=thread.project_id,
                                    principal=principal,
                                    tool_name=_name,
                                    arguments=_args,
                                )
                                # 把 request_id 写入决策 metadata
                                try:
                                    auth_decision.metadata["request_id"] = pending.request_id
                                except Exception:
                                    pass
                                # R1 round 4：runtime 提供 wait 接口时，
                                # 在 Agent 线程中挂起等待 API 决议，
                                # 而非直接 raise 结束本 run。
                                if runtime_obj is not None and hasattr(
                                    runtime_obj, "wait_for_confirmation"
                                ):
                                    remaining = max(
                                        0.0,
                                        pending.expires_at - time.time(),
                                    )
                                    slot = runtime_obj.wait_for_confirmation(
                                        request_id=pending.request_id,
                                        run_id=real_run_id,
                                        thread_id=thread_id,
                                        tool_name=_name,
                                        ttl_seconds=remaining,
                                        cancellation_token=getattr(
                                            _ctx, "cancellation_token", None
                                        ),
                                    )
                                    outcome = slot.get("outcome", "expired")
                                    if outcome == "executed":
                                        # 工具实际执行结果回灌到 working_messages，
                                        # 让模型继续推进。
                                        return slot.get("result") or ""
                                    # 拒绝/过期/取消/失败：合成错误结果反馈给模型
                                    err = slot.get("error", "confirmation " + outcome)
                                    return json.dumps(
                                        {
                                            "ok": False,
                                            "error": f"confirmation_{outcome}: {err}",
                                            "unknown": True,
                                        }
                                    )
                            raise ToolAuthorizationError(auth_decision)
                        # 授权通过：记录已授予能力，供 handler（如 subagent_submit）读取并传递
                        _ctx.granted_capabilities = default_caps
                    # 真正发起调用前原子消费额度（成功/失败都计）
                    if self._budget is not None:
                        self._budget.consume_tool_call()
                    return self._registry.execute(_name, _args, context=_ctx)
                result = self._run_via_pipeline(tool_ctx, _do_tool_call)
                trace.append({
                    "step": step,
                    "action": "tool_call",
                    "detail": {"name": decision.name, "args": decision.arguments, "result": result},
                })

                # 喂给模型的这条带 call_id，与上面的 assistant tool_use 配对。
                #
                # **不写入 pending**：Thread 只提交 user 与最终 assistant。
                # 中间 tool 消息若单独落库（不带 call_id），会成为一条
                # "孤儿 tool_result"——下一轮 working_messages 里没有与之配对的
                # tool_use，供应商会拒收该轮请求。工具往返只保留在
                # working_messages（本轮喂给模型）、Trace 与 SSE 事件里。
                working_messages.append(
                    Message(role="tool", content=result, tool_call_id=decision.id)
                )

        raise RuntimeError(f"Agent 在 {self._max_steps} 步内未完成")

    def _process_memory(
        self,
        thread_id: str,
        user_message: Message,
        trace: list[dict],
    ) -> None:
        """处理本轮用户消息中的记忆提取和保存。"""
        try:
            saved_memories = self._memory_processor.extract_and_save(
                source_thread_id=thread_id,
                messages=[user_message],
            )

            if saved_memories:
                trace.append({
                    "step": len(trace) + 1,
                    "type": "memory_saved",
                    "count": len(saved_memories),
                    "memory_ids": [
                        memory.memory_id
                        for memory in saved_memories
                    ],
                })
        except Exception as error:
            trace.append({
                "step": len(trace) + 1,
                "type": "memory_warning",
                "error": str(error),
            })