"""Phase 24 `task_note` 工具：Agent 主动维护当前任务状态。

三个刻意的设计约束：

1. **thread_id 不出现在参数 schema 里。** 它从 `ExecutionContext` 取。
   模型侧任何 `{"thread_id": "..."}` 都不会被读取——否则一句话就能跨会话
   读写别人的笔记。
2. **写入即持久化。** handler 内部直接走 `cas_update(store, ...)`，
   本轮 Run 后续失败、超时或取消都不会丢掉已写入的 Note，
   只能靠 `source_run_id` 事后审计。
3. **不支持硬删除。** 只有 add / update / resolve / list。
   删掉的约束不会有人记得，而「约束丢失」正是长会话最难查的故障。

领域异常（超限、不存在、非法字段）由 `ContextState` / `TaskNote` 抛出；
本模块捕获后转成模型可读的错误 JSON——库层仍然严格抛错，工具层不吞信息。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from mini_deerflow.context_errors import (
    ContextConflictError,
    ContextNotFoundError,
    TaskNoteError,
)
from mini_deerflow.context_shared import (
    context_attr,
    emit_event,
    err_response,
    ok_response,
)

from mini_deerflow.context_state import (
    ContextState,
    TaskNote,
    TaskNoteCategory,
    TaskNoteStatus,
)
from mini_deerflow.context_store import ContextStore, cas_update
from mini_deerflow.middleware import TraceEvent, TraceSink
from mini_deerflow.tools import Tool

__all__ = ["TASK_NOTE_TOOL_NAME", "build_task_note_tool"]

TASK_NOTE_TOOL_NAME = "task_note"

_ACTIONS = ("add", "update", "resolve", "list")

# --- list 输出上限（规格 7.6：500 条 Note 不能一次全部进入工具结果）---
#
# Note 总量上限是 500、单条内容上限 2000 字符——若 list 无条件返回全部，
# 最坏情况是 100 万字符直接灌进本轮上下文，把其它一切挤出去，
# 而模型要的通常只是「当前还挂着哪几条」。因此：
#   1. 默认只返回 active（已 resolved 的多数时候不需要）
#   2. 条数封顶
#   3. 单条内容截断
#   4. 整体输出再封一道字符预算
LIST_DEFAULT_LIMIT = 20
LIST_MAX_LIMIT = 50
LIST_CONTENT_CHARS = 160
LIST_MAX_OUTPUT_CHARS = 6000
_LIST_STATUSES = ("active", "resolved", "all")

_TASK_NOTE_PARAMETERS: dict[str, object] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(_ACTIONS),
            "description": "要执行的操作",
        },
        "note_id": {
            "type": "string",
            "description": "笔记 ID。update / resolve 必填；add 可省略则自动生成。",
        },
        "category": {
            "type": "string",
            "enum": [c.value for c in TaskNoteCategory],
            "description": "add 必填的类别",
        },
        "content": {
            "type": "string",
            "description": "笔记正文；add / update 必填",
        },
        "limit": {
            "type": "integer",
            "description": (
                f"list 最多返回多少条，1..{LIST_MAX_LIMIT}，"
                f"默认 {LIST_DEFAULT_LIMIT}"
            ),
        },
        "offset": {
            "type": "integer",
            "description": "list 分页偏移，从 0 开始，默认 0",
        },
        "status": {
            "type": "string",
            "enum": list(_LIST_STATUSES),
            "description": "list 过滤状态，默认只返回 active",
        },
    },
    "required": ["action"],
    "additionalProperties": False,
    # 刻意不暴露 thread_id：见模块 docstring 第 1 条
}

_TASK_NOTE_DESCRIPTION = (
    "维护当前任务的长期状态笔记（constraint / decision / failure / next_step）。"
    "用于跨多轮保留约束、已做决策、失败尝试与下一步，"
    "避免长会话中被压缩掉的细节彻底消失。"
    "支持 add / update / resolve / list；笔记不可删除，只能 resolve。"
)


def _ensure_state(store: ContextStore, thread_id: str) -> None:
    """确保存在 ContextState；并发创建冲突时改走读取。"""
    if store.exists(thread_id):
        return
    try:
        store.create(thread_id)
    except ContextConflictError:
        pass


def build_task_note_tool(
    store: ContextStore,
    *,
    trace_sink: TraceSink | None = None,
) -> Tool:
    """构造绑定到给定 ContextStore 的 `task_note` 工具。

    工具通过闭包持有 store——注册时机由 runtime 决定，
    因此模块 import 阶段不接触任何数据库。
    """

    def _emit_change(
        *, run_id: str, thread_id: str, action: str, note_id: str | None,
        category: str | None, status: str | None, active_count: int,
    ) -> None:
        if trace_sink is None:
            return
        trace_sink.emit(
            TraceEvent(
                event_id=uuid.uuid4().hex,
                parent_id=None,
                run_id=run_id,
                thread_id=thread_id,
                task_id=None,
                operation="TOOL_CALL",
                name="context.task_note.changed",
                start=time.monotonic(),
                end=time.monotonic(),
                status="success",
                # 只写元数据，绝不写 Note 正文
                metadata={
                    "action": action,
                    "note_id": note_id,
                    "category": category,
                    "status": status,
                    "active_count": active_count,
                },
            )
        )

    def handler(
        arguments: dict[str, object], context: object | None = None
    ) -> str:
        action = arguments.get("action")
        if not isinstance(action, str) or action not in _ACTIONS:
            return err_response(str(action) if action is not None else "", "action 非法")

        thread_id = context_attr(context, "thread_id")
        if not thread_id:
            # 拿不到当前线程就拒绝执行，绝不落到某个默认线程上
            raise TaskNoteError("无法从执行上下文获取 thread_id，拒绝操作 task_note")
        run_id = context_attr(context, "run_id") or ""

        try:
            return _dispatch(
                store=store,
                thread_id=thread_id,
                run_id=run_id,
                action=action,
                arguments=arguments,
                emit=_emit_change,
            )
        except TaskNoteError as exc:
            return err_response(action, str(exc))
        except ContextNotFoundError:
            if action == "list":
                return ok_response(action="list", notes=[], active_count=0)
            return err_response(action, f"ContextState 不存在: {thread_id}")

    def _dispatch(
        *,
        store: ContextStore,
        thread_id: str,
        run_id: str,
        action: str,
        arguments: dict[str, object],
        emit: Any,
    ) -> str:
        _ensure_state(store, thread_id)

        if action == "list":
            state = store.get(thread_id)

            limit_raw = arguments.get("limit", LIST_DEFAULT_LIMIT)
            offset_raw = arguments.get("offset", 0)
            status_raw = arguments.get("status", "active")
            if not isinstance(limit_raw, int) or isinstance(limit_raw, bool):
                return err_response(action, "limit 必须是整数", kind="usage_error")
            if not isinstance(offset_raw, int) or isinstance(offset_raw, bool):
                return err_response(action, "offset 必须是整数", kind="usage_error")
            if not 1 <= limit_raw <= LIST_MAX_LIMIT:
                return err_response(
                    action,
                    f"limit 必须在 1..{LIST_MAX_LIMIT}，收到 {limit_raw}",
                    kind="usage_error",
                )
            if offset_raw < 0:
                return err_response(action, f"offset 不能为负，收到 {offset_raw}", kind="usage_error")
            if not isinstance(status_raw, str) or status_raw not in _LIST_STATUSES:
                return err_response(
                    action,
                    f"status 必须是 {' / '.join(_LIST_STATUSES)}，收到 {status_raw!r}",
                    kind="usage_error",
                )

            pool = state.task_notes
            if status_raw == "active":
                pool = state.active_notes
            elif status_raw == "resolved":
                pool = state.resolved_notes

            page = pool[offset_raw : offset_raw + limit_raw]

            # 序列化 + 两道输出上限：单条截断，整体再封字符预算
            items: list[dict[str, object]] = []
            used_chars = 0
            budget_exhausted = False
            for note in page:
                content = note.content
                if len(content) > LIST_CONTENT_CHARS:
                    content = content[:LIST_CONTENT_CHARS] + "…"
                entry: dict[str, object] = {
                    "note_id": note.note_id,
                    "category": note.category.value,
                    "status": note.status.value,
                    "content": content,
                    "source_run_id": note.source_run_id,
                }
                entry_chars = len(json.dumps(entry, ensure_ascii=False))
                if used_chars + entry_chars > LIST_MAX_OUTPUT_CHARS:
                    budget_exhausted = True
                    break
                items.append(entry)
                used_chars += entry_chars

            returned_through = offset_raw + len(page)
            has_more = returned_through < len(pool) or budget_exhausted
            return ok_response(
                action="list",
                notes=items,
                offset=offset_raw,
                limit=limit_raw,
                status=status_raw,
                returned=len(items),
                has_more=has_more,
                truncated=budget_exhausted,
                pool_count=len(pool),
                active_count=len(state.active_notes),
                total_count=len(state.task_notes),
            )

        if action == "add":
            content = arguments.get("content")
            if not isinstance(content, str) or not content.strip():
                return err_response(action, "add 需要非空 content")
            category_raw = arguments.get("category")
            try:
                category = TaskNoteCategory(category_raw)
            except ValueError:
                return err_response(action, f"category 非法: {category_raw!r}")
            note_id = arguments.get("note_id")
            if isinstance(note_id, str) and note_id.strip():
                resolved_id = note_id.strip()
            else:
                resolved_id = f"note-{uuid.uuid4().hex[:8]}"

            def _add(state: ContextState) -> ContextState:
                note = TaskNote.create(
                    note_id=resolved_id,
                    category=category,
                    content=content,
                    source_run_id=run_id or None,
                )
                return state.add_note(note)

            saved = cas_update(store, thread_id, _add)
            emit(
                run_id=run_id,
                thread_id=thread_id,
                action="add",
                note_id=resolved_id,
                category=category.value,
                status=TaskNoteStatus.ACTIVE.value,
                active_count=len(saved.active_notes),
            )
            return ok_response(
                action="add",
                note_id=resolved_id,
                category=category.value,
                active_count=len(saved.active_notes),
                total_count=len(saved.task_notes),
            )

        note_id = arguments.get("note_id")
        if not isinstance(note_id, str) or not note_id.strip():
            return err_response(action, f"{action} 需要 note_id")
        target_id = note_id.strip()

        if action == "resolve":
            def _resolve(state: ContextState) -> ContextState:
                existing = state.find_note(target_id)
                if existing is None:
                    raise TaskNoteError(f"TaskNote 不存在: {target_id}")
                return state.update_note(existing.resolve())

            saved = cas_update(store, thread_id, _resolve)
            found = saved.find_note(target_id)
            emit(
                run_id=run_id,
                thread_id=thread_id,
                action="resolve",
                note_id=target_id,
                category=found.category.value if found else None,
                status=TaskNoteStatus.RESOLVED.value,
                active_count=len(saved.active_notes),
            )
            return ok_response(
                action="resolve",
                note_id=target_id,
                status=TaskNoteStatus.RESOLVED.value,
                active_count=len(saved.active_notes),
            )

        # action == "update"
        content = arguments.get("content")
        if not isinstance(content, str) or not content.strip():
            return err_response(action, "update 需要非空 content")
        category_raw = arguments.get("category")

        def _update(state: ContextState) -> ContextState:
            existing = state.find_note(target_id)
            if existing is None:
                raise TaskNoteError(f"TaskNote 不存在: {target_id}")
            if existing.status is TaskNoteStatus.RESOLVED:
                raise TaskNoteError(
                    f"TaskNote 已 resolve，不能修改: {target_id}（可新建一条）"
                )
            updated = existing.with_content(content)
            if category_raw is not None:
                try:
                    updated = updated.with_category(TaskNoteCategory(category_raw))
                except ValueError as exc:
                    raise TaskNoteError(f"category 非法: {category_raw!r}") from exc
            return state.update_note(updated)

        saved = cas_update(store, thread_id, _update)
        found = saved.find_note(target_id)
        emit(
            run_id=run_id,
            thread_id=thread_id,
            action="update",
            note_id=target_id,
            category=found.category.value if found else None,
            status=found.status.value if found else None,
            active_count=len(saved.active_notes),
        )
        return ok_response(
            action="update",
            note_id=target_id,
            category=found.category.value if found else None,
            active_count=len(saved.active_notes),
        )

    return Tool(
        name=TASK_NOTE_TOOL_NAME,
        description=_TASK_NOTE_DESCRIPTION,
        parameters=dict(_TASK_NOTE_PARAMETERS),
        handler=handler,
        # 打开 context 通道：thread_id 只能来自执行上下文
        needs_context=True,
    )
