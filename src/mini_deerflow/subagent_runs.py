"""Subagent 任务记录（持久化层）。

## 为什么需要单独一层

子任务的**执行**由 `SubagentManager` 负责（进程内、线程池）。但「子任务做过什么、
终态是什么、结果是什么」必须**离开进程**才能在页面刷新后重新读出——
否则刷新一次，用户就看不到刚才派发过的子任务了（G6）。

因此这里定义**可持久化的记录形状**，并提供写入 / 读取助手。

## 保存位置

记录写在**父线程**的 `ThreadState.subagent_runs` 里，与父会话同库同表
（`threads.state_json`）。选它而不是新建一张表的理由：

- 子任务本来就属于某个父会话；随父会话一起保存/读取，生命周期天然一致。
- 复用既有 `ThreadStore`（内存 + SQLite 两种实现），不引入第二套持久化机制。

## 覆盖风险（必须知道）

`Agent.run()` 在给出 FinalAnswer 时会 `save(thread)`，而 `thread` 是**运行开始
时的快照**。子任务记录是在运行途中写进 store 的 —— 若不处理，收尾保存会把
刚写进去的记录抹掉。修复见 `agent.py::_merge_latest_run_result`
（规则与 `last_run_result` 一致：**store 里的最新值优先**）。

## 内容边界

记录里只放**受控字段**：指令摘录、终态、结果摘录、时间戳、失败原因类型。
不放完整工具参数、不放原始异常 message（可能带路径 / 凭据片段）。
"""

from __future__ import annotations

import time
from typing import Any

from mini_deerflow.thread_state import ThreadState


#: 指令与结果摘录的最大长度。
#:
#: 记录是为了**让人看懂发生了什么**，不是全文存档。截断保证
#: 一条 5MB 的子任务输出不会把会话状态撑爆（历史上 `last_run_result`
#: 曾涨到 5.18 MB，见 V1 实战实测记录）。
MAX_INSTRUCTION_CHARS = 500
MAX_OUTPUT_CHARS = 4000


def _truncate(text: str, limit: int) -> str:
    """按字符数截断，保留可读前缀并标注被截断。"""
    if len(text) <= limit:
        return text
    return text[:limit] + "…（已截断）"


def build_subagent_run_record(
    *,
    task_id: str,
    instruction: str,
    status: str,
    output: str = "",
    error_type: str | None = None,
    error_message: str | None = None,
    started_at: float | None = None,
    finished_at: float | None = None,
    duration_seconds: float | None = None,
    parent_run_id: str = "",
    tool_calls: int = 0,
    model_calls: int = 0,
    after_message_count: int | None = None,
) -> dict[str, Any]:
    """构造一条可持久化的子任务记录。

    只含受控字段：``error_message`` 传进来时会被截断，但**调用方应只传
    受控文案**（如「子任务超时」），不要传原始异常 message。

    ``after_message_count`` 是**时序锚点**：该子任务卡应插在父会话的
    第 N 条消息之后（N = 锚点之前有多少条消息）。前端用它把卡片插到
    消息流里的**当时位置**，而不是永远挂在会话末尾。
    为 None 表示"位置未知"（旧记录或异常路径），前端会退回到末尾展示。
    """
    record: dict[str, Any] = {
        "task_id": task_id,
        "instruction": _truncate(instruction, MAX_INSTRUCTION_CHARS),
        "status": status,
        "output": _truncate(output, MAX_OUTPUT_CHARS),
        "tool_calls": tool_calls,
        "model_calls": model_calls,
        "recorded_at": time.time(),
    }
    if parent_run_id:
        record["parent_run_id"] = parent_run_id
    if error_type:
        record["error_type"] = error_type
    if error_message:
        record["error_message"] = _truncate(error_message, 500)
    if started_at is not None:
        record["started_at"] = started_at
    if finished_at is not None:
        record["finished_at"] = finished_at
    if duration_seconds is not None:
        record["duration_seconds"] = duration_seconds
    if after_message_count is not None:
        record["after_message_count"] = int(after_message_count)
    return record


def append_subagent_run(
    thread: ThreadState,
    record: dict[str, Any],
    *,
    max_records: int = 50,
) -> None:
    """把一条子任务记录追加到线程状态。

    同一 ``task_id`` 重复写入时**替换**旧记录（后写为准）——
    同一任务先写「运行中」再写「已完成」是正常路径。

    记录数超过 ``max_records`` 时丢弃**最旧的**一条：会话状态有界，
    不会因为反复派发而无限增长。
    """
    runs = thread.subagent_runs
    task_id = record.get("task_id")
    for i, existing in enumerate(runs):
        if existing.get("task_id") == task_id:
            runs[i] = record
            break
    else:
        runs.append(record)
    while len(runs) > max_records:
        runs.pop(0)


def find_subagent_run(
    thread: ThreadState,
    task_id: str,
) -> dict[str, Any] | None:
    """按 task_id 找一条记录；找不到返回 None。"""
    for record in thread.subagent_runs:
        if record.get("task_id") == task_id:
            return record
    return None


def collapse_run_anchors(
    thread: ThreadState,
    run_id: str,
) -> int:
    """把某一轮的子任务卡锚点收到该轮的消息边界（返回改动的条数）。

    ## 为什么需要这一步

    ``Agent.run`` 只在给出最终回答时提交本轮的 user / assistant 消息。
    父任务被取消（或失败）时**本轮消息不落库** —— 但子任务卡已经在运行途中
    写进了 ``thread.subagent_runs``，锚点还是「本轮 user 消息之后」
    （= 本轮开始前的消息数 + 1）。

    下一轮消息落库后，这个锚点正好指向**下一轮的提问** ——
    卡片因此串到下一轮下面，看起来像属于下一轮。

    ## 修法（自带护栏）

    锚点大于线程当前消息条数，说明它指向一条**并不存在**的消息
    （本轮消息没落库）。把它收到当前消息条数（本轮的起点），
    卡片就停在轮次边界上，不会被后面落库的消息拖走。

    护栏让这条规则**只影响没落库的那一轮**：已提交的一轮，
    锚点 = 消息数 - 1，不大于消息数，**原样不动**。
    """
    message_count = int(getattr(thread, "message_count", 0))
    changed = 0
    for record in thread.subagent_runs:
        if record.get("parent_run_id") != run_id:
            continue
        anchor = record.get("after_message_count")
        if isinstance(anchor, bool) or not isinstance(anchor, int):
            continue
        if anchor > message_count:
            record["after_message_count"] = message_count
            changed += 1
    return changed
