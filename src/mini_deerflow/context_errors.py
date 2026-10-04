"""Phase 24 上文工程领域异常。

设计约束：
- 每个异常对应一个**可判定的失败原因**，调用方能据此分流（重试 / 降级 / 抛出）。
- 不做「尽力而为」的隐式兜底：静默吞掉上下文错误会让 Agent 带着残缺上下文继续跑，
  这比直接失败更危险。
"""

from __future__ import annotations


class ContextEngineeringError(Exception):
    """上下文工程层的基础异常。"""


class ContextConflictError(ContextEngineeringError):
    """ContextStore CAS 写入时 revision 不匹配。

    绝不静默覆盖：两个并发 Run 同时改一份 ContextState 时，后写者必须
    读-改-写重试，而不是把对方的 TaskNotes / 摘要冲掉。
    """

    def __init__(
        self,
        thread_id: str,
        *,
        expected: int,
        actual: int | None,
    ) -> None:
        self.thread_id = thread_id
        self.expected = expected
        self.actual = actual
        actual_text = "不存在" if actual is None else str(actual)
        super().__init__(
            f"ContextState 版本冲突: thread={thread_id} "
            f"expected_revision={expected} actual_revision={actual_text}"
        )


class ContextNotFoundError(ContextEngineeringError):
    """按 thread_id 查不到 ContextState。"""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        super().__init__(f"ContextState 不存在: {thread_id}")


class ContextDataCorruptedError(ContextEngineeringError):
    """持久化的 ContextState 无法解析。

    与 Thread 数据损坏分开：两者恢复路径不同（ContextState 可从原消息重建，
    Thread 不能），混成一个异常会让调用方无法选择正确的降级动作。
    """

    def __init__(self, thread_id: str, reason: str) -> None:
        self.thread_id = thread_id
        self.reason = reason
        super().__init__(f"ContextState 数据损坏: {thread_id} ({reason})")


class ContextSchemaVersionError(ContextEngineeringError):
    """遇到未知 schema_version，且没有可用迁移。

    不做「乐观降级解析」：带新版字段的投影被旧代码当成旧格式读，
    会静默丢掉压缩水位——那正是本阶段要防的那类故障。
    """

    def __init__(self, thread_id: str, *, found: int, supported: int) -> None:
        self.thread_id = thread_id
        self.found = found
        self.supported = supported
        super().__init__(
            f"ContextState schema_version 不兼容: found={found} "
            f"supported={supported} thread={thread_id}"
        )


class MalformedMessageSequenceError(ContextEngineeringError):
    """消息序列无法合法分组。

    三类：孤立 `tool_result`（前面没有配对的 tool_call）、悬空工具链
    （有 tool_call 却没有后续 assistant 收尾）、user/system 插进工具链中间。

    绝不「尽力拼一个组」发给模型——供应商会拒收孤儿 tool_result，
    或者模型会看到半截工具调用，两种都会让整轮失败得很难查。
    """

    def __init__(self, reason: str, *, index: int) -> None:
        self.reason = reason
        self.index = index
        super().__init__(f"消息序列无法合法分组: {reason}（index={index}）")


class ContextWindowExceededError(ContextEngineeringError):
    """必要内容自身就超过硬 Token 上限。

    这是**不可降级**的失败：当前用户输入、安全规则、活跃工具回合都不能裁。
    调用方要么扩容 policy，要么拒绝本轮运行。
    """

    def __init__(self, *, required_tokens: int, hard_limit: int) -> None:
        self.required_tokens = required_tokens
        self.hard_limit = hard_limit
        super().__init__(
            f"必要上下文超过硬上限: required={required_tokens} hard_limit={hard_limit}"
        )


class SummaryFormatError(ContextEngineeringError):
    """摘要模型连续两次返回非法 JSON。

    第一次格式错误会发起一次修复请求；第二次仍失败就抛本异常，
    绝不用正则「尽量猜」出一个看似可用的摘要。
    """

    def __init__(self, reason: str, *, attempts: int) -> None:
        self.reason = reason
        self.attempts = attempts
        super().__init__(f"摘要格式非法（已尝试 {attempts} 次）: {reason}")


class SummaryInvalidResponseError(SummaryFormatError):
    """摘要模型返回了 ToolCall 而不是 JSON 文本。

    摘要路径只允许 Raw Model 调用，禁止工具执行 / Subagent / 再压缩，
    因此 ToolCall 是明确的非法响应，不进入格式修复流程。
    """


class TaskNoteError(ContextEngineeringError):
    """Task Note 领域错误基类。"""


class TaskNoteNotFoundError(TaskNoteError):
    """按 note_id 查不到 Note。"""

    def __init__(self, note_id: str) -> None:
        self.note_id = note_id
        super().__init__(f"TaskNote 不存在: {note_id}")


class TaskNoteValidationError(TaskNoteError, ValueError):
    """Note 字段非法：未知 category/status、内容超限、超容量。

    同时继承 `ValueError`：领域内构造参数校验与既有调用方的
    `except ValueError` 都能接住，不必在两种异常之间二选一。
    """


class TaskNoteLimitExceededError(TaskNoteValidationError):
    """Note 数量或长度超过上限。

    明确抛错而不是静默逐出旧 Note——被静默丢弃的约束不会有人记得，
    而「约束丢失」正是长会话最难查的故障。
    """

    def __init__(self, limit_name: str, limit: int, actual: int) -> None:
        self.limit_name = limit_name
        self.limit = limit
        self.actual = actual
        super().__init__(
            f"TaskNote 超限: {limit_name} limit={limit} actual={actual}"
        )


class HistoryIndexError(ContextEngineeringError):
    """历史向量索引操作失败。"""


class HistoryIndexLagError(HistoryIndexError):
    """索引水位落后且补齐失败。

    此时**不返回伪造的完整搜索结果**——让调用方知道结果不完整，
    由调用方决定重试或降级。
    """

    def __init__(self, thread_id: str, *, indexed_through: int, available: int) -> None:
        self.thread_id = thread_id
        self.indexed_through = indexed_through
        self.available = available
        super().__init__(
            f"历史索引落后且补齐失败: thread={thread_id} "
            f"indexed_through={indexed_through} available={available}"
        )


class HistoryReferenceError(ContextEngineeringError):
    """history_read / history_search 收到非法 reference 或越界参数。"""


__all__ = [
    "ContextConflictError",
    "ContextDataCorruptedError",
    "ContextEngineeringError",
    "ContextNotFoundError",
    "ContextSchemaVersionError",
    "ContextWindowExceededError",
    "HistoryIndexError",
    "HistoryIndexLagError",
    "HistoryReferenceError",
    "MalformedMessageSequenceError",
    "SummaryFormatError",
    "SummaryInvalidResponseError",
    "TaskNoteError",
    "TaskNoteLimitExceededError",
    "TaskNoteNotFoundError",
    "TaskNoteValidationError",
]
