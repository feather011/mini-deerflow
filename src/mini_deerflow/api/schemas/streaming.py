"""SSE RunEvent Schema（19D 阶段）。

定义流式事件模型。所有事件走同一形状，event_type 区分语义。
绝不暴露 prompt 全文、模型凭据、完整工具参数、Traceback。
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


# 终态事件：每个 run 只能有其中一个
TERMINAL_EVENT_TYPES = frozenset(
    {"run.completed", "run.failed", "run.timed_out", "run.cancelled"}
)


class RunEventType(str, Enum):
    """RunEvent 事件类型枚举。"""

    RUN_STARTED = "run.started"
    MODEL_STARTED = "model.started"
    MODEL_COMPLETED = "model.completed"
    MODEL_FAILED = "model.failed"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    RUN_RESULT = "run.result"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_TIMED_OUT = "run.timed_out"
    RUN_CANCELLED = "run.cancelled"
    RUN_MODEL_TOKEN = "model.token"  # token 流式推送事件
    # Subagent 首版：子任务生命周期事件。
    #
    # 子代理不走中间件管线（没有 pipeline），因此这些事件由
    # SubagentManager **手工**发布到 broker，与父运行的 SSE 同一条流。
    SUBAGENT_STARTED = "subagent.started"
    SUBAGENT_COMPLETED = "subagent.completed"
    SUBAGENT_FAILED = "subagent.failed"


class RunEvent(BaseModel):
    """单条 RunEvent；与 SSE 一一对应。"""

    run_id: str
    thread_id: str
    event_type: RunEventType
    sequence: int = Field(ge=0)
    timestamp: float = Field(default_factory=lambda: time.time())

    # 终态事件携带；其余事件为 None
    answer: str | None = None
    error: str | None = None

    # 可选 metadata；只放确定安全的字段（如 attempt）
    attempt: int | None = None
    operation: str | None = None
    tool_name: str | None = None
    duration_seconds: float | None = None

    # run.result 事件携带 project_execute 的结构化结果（JSON 字符串形式）
    tool_result: str | None = None
    # model.token 事件携带 token 流式推送的增量文本
    delta: str | None = None

    # Subagent 首版：子任务事件携带的字段。
    subagent_task_id: str | None = None
    """子任务 ID（sa-xxxxxxxxxxxx）。"""
    subagent_status: str | None = None
    """子任务终态：SUCCEEDED / FAILED / CANCELLED / TIMED_OUT。"""
    subagent_instruction: str | None = None
    """子任务指令摘录（前端卡片展示用）。"""
    subagent_output: str | None = None
    """子任务结果摘录（前端卡片展示用）。"""


def encode_sse(event: RunEvent) -> str:
    """单条 RunEvent → SSE 消息块。

    格式：
        id: <sequence>
        event: <event_type>
        data: <JSON>

        （空行结尾）
    """
    # Pydantic 已过滤敏感字段；这里再 dump 一次确保只暴露对外字段
    payload = event.model_dump(exclude_none=True)
    data = _json_dumps(payload)
    return f"id: {event.sequence}\nevent: {event.event_type.value}\ndata: {data}\n\n"


def encode_comment(text: str) -> str:
    """SSE 注释行；用于心跳或 keepalive，浏览器不解析。"""
    return f": {text}\n\n"


def _json_dumps(obj: object) -> str:
    """标准 JSON dump；ensure_ascii=False 保留 Unicode。"""
    import json

    return json.dumps(obj, ensure_ascii=False)


def is_terminal(event_type: RunEventType) -> bool:
    """判断事件是否为终态事件。"""
    return event_type.value in TERMINAL_EVENT_TYPES


def terminal_status_from_exception(exc: Exception) -> RunEventType:
    """根据异常类型推断终态事件类型。

    TimeoutError → run.timed_out
    AgentRunCancelledError → run.cancelled
    其余 → run.failed
    """
    from mini_deerflow.api.services.agent_runs import AgentRunCancelledError

    if isinstance(exc, TimeoutError):
        return RunEventType.RUN_TIMED_OUT
    if isinstance(exc, AgentRunCancelledError):
        return RunEventType.RUN_CANCELLED
    return RunEventType.RUN_FAILED


# 类型别名供外部引用
RunEventStatus = Literal[
    "succeeded",
    "failed",
    "timed_out",
    "cancelled",
]

# 失败原因的最大展示长度：避免超长文本撑爆事件面板
_MAX_FAILURE_DETAIL = 300


def _truncate(text: str) -> str:
    """按 `_MAX_FAILURE_DETAIL` 截断文本。"""
    if len(text) > _MAX_FAILURE_DETAIL:
        return text[:_MAX_FAILURE_DETAIL] + "…"
    return text


def _exception_chain(exc: BaseException) -> list[BaseException]:
    """异常自身 + `__cause__` 链（去重，防环）。"""
    chain: list[BaseException] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        chain.append(cur)
        cur = cur.__cause__
    return chain


def _tool_failure_parts(exc: BaseException | None) -> tuple[str, str] | None:
    """工具类异常 → (稳定错误码, 安全说明)；不是工具类异常返回 None。

    **绝不透传 `str(exc)`**：`ToolExecutionError` 的 message 里嵌着 handler
    抛出的原始异常，那可能是文件系统路径、内部栈或凭据片段。

    错误码是稳定的字面量，前端可据此区分「模型叫错了名字」「越权调用」
    「授权未过」与「工具执行失败」，无需解析文案。

    **项目工具不在本函数的正常覆盖范围内**：项目工具的失败被
    `project_tools._as_tool_result` 转成 `ok:false` 的 JSON 结果，
    异常**不逃出 handler** —— 业务失败是**可恢复**的（模型改参即可重试），
    因此**不会**产生 `tool.failed` 事件。项目工具的稳定错误码在**结果
    JSON 的 `code` 字段**里（见 `project_tools.PROJECT_TOOL_CODES`），
    消费者是模型，不是事件流。

    下面两个 `ProjectToolError` 分支只是**异常万一逃逸时的兜底**（例如
    直接调用 handler 而不经过注册表），不是项目工具失败的正常路径。
    """
    if exc is None:
        return None
    # 延迟导入：避免 api.schemas 反向依赖领域模块时产生循环导入
    from mini_deerflow.project_tools import (
        ProjectToolStateError,
        ProjectToolUsageError,
    )
    from mini_deerflow.tool_authorization import ToolAuthorizationError
    from mini_deerflow.tools import (
        ToolExecutionError,
        ToolNotAllowedError,
        ToolNotFoundError,
    )

    chain = _exception_chain(exc)

    # 项目工具错误优先：它的 message 具体且受控，比外层的
    # "工具执行失败" 更有诊断价值（后者是 registry 的统一包装）。
    for cur in chain:
        if isinstance(cur, ProjectToolUsageError):
            return ("PROJECT_TOOL_USAGE_ERROR", str(cur).strip())
        if isinstance(cur, ProjectToolStateError):
            return ("PROJECT_TOOL_STATE_ERROR", str(cur).strip())

    for cur in chain:
        if isinstance(cur, ToolNotFoundError):
            return ("TOOL_NOT_FOUND", "工具不存在或未注册")
        if isinstance(cur, ToolNotAllowedError):
            return ("TOOL_NOT_ALLOWED", "工具不在本次允许清单内")
        if isinstance(cur, ToolAuthorizationError):
            # reason_code 是内部稳定码（metadata_missing / tool_disabled …），
            # 不含敏感信息；用它让使用者知道该走哪条路。
            code = getattr(getattr(cur, "decision", None), "reason_code", "")
            suffix = f"（{code}）" if code else ""
            return ("TOOL_AUTHORIZATION_DENIED", f"工具调用未通过授权检查{suffix}")
        if isinstance(cur, ToolExecutionError):
            return ("TOOL_EXECUTION_FAILED", "工具执行失败")

    return None


def safe_failure_detail(exc: BaseException | None) -> str:
    """提取**可以安全展示给客户端**的失败原因；没有则返回空串。

    四类来源：

    - `ModelError` 家族：只含状态码 / 供应商 / 模型名（见 `model_errors.py`
      的模块约定：异常消息只包含非敏感信息，绝不携带 API Key）
    - `ProjectToolError` 家族：只含参数校验与会话状态的描述
      （**兜底**：项目工具的失败正常走 `ok:false` 结果，不产生事件）
    - 工具类异常：给**稳定错误码 + 固定说明**，不放行原始 message
    - 预算耗尽（`AgentBudgetExceededError` / `BudgetExceededError`）：
      只取 `.resource` 与 `.limit` 两个**代码内受控属性**拼出原因，
      不放行 `str(exc)`（复审 B3：否则 SSE 只剩异常类型名，
      `model_calls` 与上限丢失，消费方无从判断原因）

    其它异常（尤其第三方 SDK 异常）一律**不**展示 message —— 沿用既有保守策略。

    沿 `__cause__` 链查找：`AgentRunFailedError` 用 `raise ... from exc`
    包装了原始异常，不走链就取不到真正的原因。
    """
    if exc is None:
        return ""
    # 延迟导入：避免 api.schemas 反向依赖领域模块时产生循环导入
    from mini_deerflow.api.services.agent_runs import AgentBudgetExceededError
    from mini_deerflow.middleware import BudgetExceededError
    from mini_deerflow.model_errors import ModelError
    from mini_deerflow.project_tools import ProjectToolError

    chain = _exception_chain(exc)

    # 模型错误优先：它的 message 本就安全且信息量最大。
    for cur in chain:
        if isinstance(cur, ModelError):
            text = str(cur).strip()
            if text:
                return _truncate(text)

    # 预算耗尽：resource（固定字符串集合）与 limit（数值）是受控属性，
    # 拼装成因；两者都取属性、不取 str(exc)。
    for cur in chain:
        if isinstance(cur, (AgentBudgetExceededError, BudgetExceededError)):
            resource = getattr(cur, "resource", "") or ""
            limit = getattr(cur, "limit", None)
            if resource:
                text = f"预算耗尽：{resource}"
                if limit is not None:
                    text += f"（上限: {limit}）"
                return _truncate(text)

    # 工具类错误：稳定错误码 + 安全说明（不放行原始 message）。
    parts = _tool_failure_parts(exc)
    if parts is not None:
        code, detail = parts
        return _truncate(f"{code}：{detail}" if detail else code)

    # 项目工具错误：message 在产生端已受控。
    for cur in chain:
        if isinstance(cur, ProjectToolError):
            text = str(cur).strip()
            if text:
                return _truncate(text)

    return ""


def tool_failure_text(tool_name: str | None, exc: BaseException | None) -> str:
    """工具失败的客户端文案：工具名 + 稳定错误码 + 安全说明。

    与 `failure_text` 的区别：这里**总是**给出稳定错误码，即使异常不属于
    已知家族 —— 未识别的工具异常只给类名，不给原始 message。
    """
    name = tool_name or "未知工具"
    parts = _tool_failure_parts(exc)
    if parts is None:
        cls = exc.__class__.__name__ if exc is not None else "UnknownError"
        return f"工具 {name} 调用失败（{cls}）"
    code, detail = parts
    text = f"工具 {name} 调用失败（{code}）"
    if detail:
        text += f"：{_truncate(detail)}"
    return text


def failure_text(prefix: str, exc: BaseException | None) -> str:
    """`prefix` 加上可安全展示的失败原因；没有原因时只返回 `prefix`。"""
    detail = safe_failure_detail(exc)
    return f"{prefix}：{detail}" if detail else prefix


def safe_failure_message(exc: Exception) -> str:
    """生成对客户端安全的失败文案。

    策略：固定短语 + 类型名；**只在异常消息按约定不含敏感信息时**
    追加具体原因（见 `safe_failure_detail`），其余情况绝不暴露原始
    message / Traceback / 凭据。
    """
    return failure_text(f"Run 失败 ({exc.__class__.__name__})", exc)