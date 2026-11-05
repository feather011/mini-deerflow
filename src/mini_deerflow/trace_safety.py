"""Trace 失败原因的安全提取（TRACE-01）。

放在 **core 层**而不是 api 层：`api/schemas/streaming.py` 依赖
`mini_deerflow.middleware`，middleware 反向 import api 会形成循环依赖。

Trace 与 SSE 是**两个消费面**，各自维护自己的安全策略：
SSE 的文案在 `api/schemas/streaming.py`（本模块**不动**它），
Trace 的文案在这里。两边都不放行未受控的原始异常文本。

安全策略（**全部受控，不依赖「异常消息本身安全」这一约定**）：

- `ModelError` 家族：按**具体子类**映射到固定文案 —— **不放行 `str(exc)`**。
  异常类型不构成安全保证：上游服务返回的错误正文可能带请求信息或敏感字符串，
  截断只能限长、不能保证安全。完整原文留在服务端日志里（`_LOGGER.exception`），
  Trace 只保留类型级结论；异常**类名**另有 `error_type` 字段承载。
- `BudgetExceededError`：`resource` 必须命中**允许列表**才放行，
  `limit` 是数值（安全）。不在表内的一律不放行 ——
  `lead_agent/budget_ledger.py` 的 `BudgetExceededError(f"lease:{lease_id}:{resource}", ...)`
  就是动态字符串，说明「resource 是固定枚举」只是约定、不是代码保证。
- `TimeoutError` 家族：固定文案「超时」。
- 其它异常（尤其第三方 SDK 异常）一律返回空串 —— 原始 message 可能带
  本地路径、凭据或用户数据。
"""

from __future__ import annotations

# 失败原因的长度上限：Trace 会被持久化成 JSONL，无界的异常文本会撑爆日志。
# 当前所有受控文案都很短，这里是兜底的最后一道闸。
_MAX_DETAIL_LEN = 200

# 预算资源的**允许列表**：与 middleware.Budget / agent.py 实际使用的取值一致。
# 新增预算资源时必须同步加进来 —— 这是有意的闸门，不在表内一律不放行。
_ALLOWED_BUDGET_RESOURCES = frozenset({
    "steps",
    "model_calls",
    "model_call_timeout",
    "tool_calls",
    "tool_call_cancelled",
    "subagent_tasks",
    "subagent_depth",
    "summary_calls",
})


def _truncate(text: str) -> str:
    if len(text) <= _MAX_DETAIL_LEN:
        return text
    return text[:_MAX_DETAIL_LEN] + "…"


def _exception_chain(exc: BaseException) -> list[BaseException]:
    """沿 `__cause__` / `__context__` 链收集异常（去重、保序）。

    上层常用 `raise ... from exc` 包装原始异常，不走链就取不到真正的原因。
    """
    chain: list[BaseException] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        chain.append(cur)
        cur = cur.__cause__ or cur.__context__
    return chain


def _model_error_detail(exc: BaseException) -> str:
    """`ModelError` 家族 → **固定文案**（按具体子类）。

    刻意不取 `str(exc)`：异常消息由各处构造，可能夹带上游响应正文。
    """
    from mini_deerflow.model_errors import (
        ModelAuthError,
        ModelConfigError,
        ModelEmptyResponseError,
        ModelError,
        ModelProtocolError,
        ModelRateLimitError,
        ModelServerError,
        ModelTimeoutError,
    )

    if isinstance(exc, ModelConfigError):
        return "模型配置错误"
    if isinstance(exc, ModelTimeoutError):
        return "模型调用超时"
    if isinstance(exc, ModelAuthError):
        return "模型鉴权失败"
    if isinstance(exc, ModelRateLimitError):
        return "模型调用被限流"
    if isinstance(exc, ModelServerError):
        return "模型服务端错误"
    if isinstance(exc, ModelEmptyResponseError):
        return "模型返回内容为空"
    if isinstance(exc, ModelProtocolError):
        return "模型响应不符合约定"
    if isinstance(exc, ModelError):
        # 未知子类：仍然只给通用结论，不放行消息。
        return "模型调用失败"
    return ""


def _budget_detail(exc: BaseException) -> str:
    """`BudgetExceededError` → 受控原因（resource 走允许列表）。"""
    resource = getattr(exc, "resource", "") or ""
    if resource not in _ALLOWED_BUDGET_RESOURCES:
        # 不在允许列表 → 整条不放行（resource 是任意字符串，不是固定枚举）。
        return ""
    limit = getattr(exc, "limit", None)
    text = f"预算耗尽：{resource}"
    if isinstance(limit, int):
        text += f"（上限: {limit}）"
    return text


def safe_trace_detail(exc: BaseException | None) -> str:
    """提取**可以安全展示**的失败原因；没有受控原因则返回空串。

    绝不放行未识别的原始异常 message —— 它可能带本地路径、凭据或用户数据。
    """
    if exc is None:
        return ""

    # 延迟导入：middleware 会 import 本模块，模块级互相 import 会形成循环。
    from mini_deerflow.middleware import BudgetExceededError
    from mini_deerflow.model_errors import ModelError

    chain = _exception_chain(exc)

    # 模型错误优先：类型级结论最具体（文案固定，不含原文）。
    for cur in chain:
        if isinstance(cur, ModelError):
            detail = _model_error_detail(cur)
            if detail:
                return _truncate(detail)

    # 预算耗尽：resource 走允许列表，limit 是数值。
    for cur in chain:
        if isinstance(cur, BudgetExceededError):
            detail = _budget_detail(cur)
            if detail:
                return _truncate(detail)

    # 超时：固定文案，不放行原始 message。
    for cur in chain:
        if isinstance(cur, TimeoutError):
            return "超时"

    return ""
