"""Agent：驱动模型与工具之间的调用循环，基于 ThreadStore 维护会话状态。"""

from __future__ import annotations

from typing import Callable, TypeVar

from mini_deerflow.context_assembler import AssembledContext, ContextAssembler
from mini_deerflow.memory_manager import MemoryProcessor
from mini_deerflow.memory_context import MemoryContextProvider
from mini_deerflow.middleware import (
    Budget,
    BudgetExceededError,
    ExecutionContext,
    MiddlewarePipeline,
    OperationType,
)
from mini_deerflow.models import FinalAnswer, Message, Model, ToolCall
from mini_deerflow.skill_runtime import SkillContextProvider
from mini_deerflow.thread_store import ThreadStore
from mini_deerflow.tools import ToolRegistry

T = TypeVar("T")


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
        # 24：可选。None = 走原有拼装逻辑，短会话行为与 Phase 23 完全一致。
        self._context_assembler = context_assembler
        self._last_assembled: AssembledContext | None = None

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
        self, thread_id: str, user_input: str, run_id: str = ""
    ) -> tuple[str, list[dict]]:
        """对指定 thread_id 执行一轮 Agent 循环；成功后原子写入，失败时 Thread 不变。

        Args:
            run_id: 上层 Run 的标识，仅用于 Trace 关联上下文组装与压缩事件。
                缺省为空串，保持既有调用方兼容。
        """
        thread = self._store.get(thread_id)  # ThreadNotFoundError 原样传播

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
            skill_context = self._skill_context_provider.build(user_input)

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
        model_tools = self._registry.tool_specs()

        trace: list[dict] = []

        for step in range(1, self._max_steps + 1):
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
            model_ctx = ExecutionContext(
                run_id="",  # 由 IntegratedAgent 在外层生成
                thread_id=thread_id,
                operation=OperationType.MODEL_CALL,
                operation_name="model_decide",
                budget=self._budget,
            )
            def _do_model_call(
                _msgs=model_messages,
                _tools=model_tools,
            ) -> ToolCall | FinalAnswer:
                # 真正发起调用前原子消费额度（成功/失败/Retry 每次都计）
                if self._budget is not None:
                    self._budget.consume_model_call()
                # 每轮都把当前允许的工具描述传下去，供应商适配层据此翻译成自己的 tools
                return self._model.decide(_msgs, _tools)
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
                tool_ctx = ExecutionContext(
                    run_id="",
                    thread_id=thread_id,
                    operation=OperationType.TOOL_CALL,
                    operation_name=decision.name,
                    budget=self._budget,
                )
                def _do_tool_call(
                    _name=decision.name,
                    _args=decision.arguments,
                ) -> str:
                    # 真正发起调用前原子消费额度（成功/失败都计）
                    if self._budget is not None:
                        self._budget.consume_tool_call()
                    return self._registry.execute(_name, _args)
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