"""Phase 25A 领域异常。

设计遵循仓库既有风格（见 `context_errors.py` / `subagent_models.py`）：
每个异常对应**一个可判定的失败原因**，`__init__` 收上下文参数，
中文消息可直接进日志与上层错误响应。不在这里做任何 I/O。
"""

from __future__ import annotations


class LeadAgentError(Exception):
    """Lead Agent 领域异常基类。"""


class InvalidTransitionError(LeadAgentError):
    """非法状态转换。

    与 `subagent_models.InvalidTransitionError` 语义相同但**独立命名空间**：
    Lead Run 与 Subagent 任务的状态集合、恢复语义都不同，共用一个异常
    会让调用方无法按层区分该重试还是该判失败。
    """

    def __init__(self, entity_id: str, from_status: str, to_status: str) -> None:
        self.entity_id = entity_id
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"{entity_id} 状态 {from_status} 无法转换到 {to_status}"
        )


class TerminalStateError(InvalidTransitionError):
    """尝试离开终态。

    终态（SUCCEEDED / PARTIAL / FAILED / CANCELLED / TIMED_OUT）不可逆，
    这是单独一类异常而不是普通非法转换：调用方对它的正确反应是
    「忽略并保持现状」（例如超时的迟到回调），而不是重试。
    """


class RevisionLimitExceededError(LeadAgentError):
    """WorkItem 修订次数用尽。

    冻结设计：**最多首次执行 + 一次修订**。第二次执行仍失败必须落到
    FAILED 终态，不允许无限「再试一次」把一个坏任务拖成永不收敛。
    """

    def __init__(self, work_id: str, attempts: int) -> None:
        self.work_id = work_id
        self.attempts = attempts
        super().__init__(f"WorkItem {work_id} 已用尽修订次数（已尝试 {attempts} 次）")


# --- 滚动批次校验 ---


class BatchValidationError(LeadAgentError):
    """滚动批次校验失败基类。"""


class DuplicateWorkIdError(BatchValidationError):
    """work_id 重复（批次内重复，或与已验证历史重复）。"""

    def __init__(self, work_id: str) -> None:
        self.work_id = work_id
        super().__init__(f"work_id 重复: {work_id}")


class SelfDependencyError(BatchValidationError):
    """条目依赖自己。"""

    def __init__(self, work_id: str) -> None:
        self.work_id = work_id
        super().__init__(f"WorkItem {work_id} 依赖了自己")


class MissingDependencyError(BatchValidationError):
    """依赖既不在已验证历史，也不在当前批次。

    滚动批次不生成一次性完整 DAG，因此「引用了还不存在的未来任务」
    一定是规划错误——依赖只能指向**已验证的历史**或**本批次内**。
    """

    def __init__(self, work_id: str, missing: tuple[str, ...]) -> None:
        self.work_id = work_id
        self.missing = missing
        super().__init__(
            f"WorkItem {work_id} 的依赖不存在: {', '.join(missing)}"
        )


class CyclicDependencyError(BatchValidationError):
    """当前批次存在循环依赖。"""

    def __init__(self, cycle: tuple[str, ...]) -> None:
        self.cycle = cycle
        super().__init__(f"批次内存在循环依赖: {' -> '.join(cycle)}")


class EmptyGoalFieldError(BatchValidationError):
    """objective 或 acceptance criteria 为空。"""

    def __init__(self, field_name: str) -> None:
        self.field_name = field_name
        super().__init__(f"GoalContract.{field_name} 不能为空")


# --- 资源声明与冲突 ---


class ResourceConflictError(LeadAgentError):
    """两条执行路线声明的资源访问互相冲突，无法并行。

    保守原则：**读–读可并行，其余一律冲突；unknown side effect 强制冲突**。
    分不清读写时选择串行，代价只是慢；错误并行的代价是数据竞争。
    """

    def __init__(self, resource: str, left: str, right: str) -> None:
        self.resource = resource
        self.left = left
        self.right = right
        super().__init__(
            f"资源 {resource} 冲突: {left} 与 {right} 不能并行"
        )


class UnknownResourceError(BatchValidationError):
    """资源声明缺失或无法规范化。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"资源声明非法: {reason}")


# --- LeadRunStore（25B）---
#
# 预算相关的超限**不在此处定义**：继续复用 middleware.BudgetExceededError，
# 保持「项目只有一个超预算异常」的既有语义（冻结原则）。


class LeadStoreError(LeadAgentError):
    """LeadRunStore 基类异常。"""


class LeadRunAlreadyExistsError(LeadStoreError):
    """创建了重复的 run_id。"""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"LeadRun 已存在: {run_id}")


class LeadRunNotFoundError(LeadStoreError):
    """按 run_id 查不到记录。"""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"LeadRun 不存在: {run_id}")


class LeadRunConflictError(LeadStoreError):
    """CAS 写入时 revision 不匹配。

    绝不静默覆盖：两个并发写者同时改一份 Run 状态时，后写者必须
    读-改-写重试，而不是把对方的批次进度、租约与预算冲掉。
    """

    def __init__(self, run_id: str, *, expected: int, actual: int | None) -> None:
        self.run_id = run_id
        self.expected = expected
        self.actual = actual
        actual_text = "记录不存在" if actual is None else str(actual)
        super().__init__(
            f"LeadRun 版本冲突: {run_id} expected_revision={expected} "
            f"actual={actual_text}"
        )


class LeadStoreClosedError(LeadStoreError):
    """Store 已关闭后仍被调用。

    与 SQLite 实现的 `ContextStore 已关闭` 同语义：明确失败，
    而不是返回内存里的过期数据。
    """

    def __init__(self) -> None:
        super().__init__("LeadRunStore 已关闭")


class LeadStoreDataCorruptedError(LeadStoreError):
    """持久化数据损坏或 schema 版本不兼容。

    **明确失败，不静默覆盖**：损坏的 Run 状态若被当成空状态覆盖写入，
    审计链与预算账本会一起消失，而调用方还以为是一次正常保存。
    """

    def __init__(self, run_id: str, reason: str) -> None:
        self.run_id = run_id
        self.reason = reason
        super().__init__(f"LeadRun 数据损坏: {run_id} ({reason})")


class LeaseNotFoundError(LeadStoreError):
    """按 lease_id 查不到租约。"""

    def __init__(self, lease_id: str) -> None:
        self.lease_id = lease_id
        super().__init__(f"BudgetLease 不存在: {lease_id}")


class EventRunMismatchError(LeadStoreError):
    """提交的事件不属于这份状态。

    **领域异常而不是 ValueError**：这是「拿错了审计事件」——调用方真正
    该做的与参数格式错误完全不同（要换事件重来，不是修输入）。
    校验发生在写事务之前，因此快照不动、revision 不增、事件不落库。
    """

    def __init__(self, event_run_id: str, state_run_id: str) -> None:
        self.event_run_id = event_run_id
        self.state_run_id = state_run_id
        super().__init__(
            f"事件 run_id({event_run_id}) 与状态 run_id({state_run_id}) 不一致"
        )


class LeadStoreWriteError(LeadStoreError):
    """SQLite 写入失败，且**不是**并发竞争。

    与 `LeadRunConflictError` 的分界线就是「能不能重读重试」：
    拿不到写锁、sequence 撞 UNIQUE → 冲突（可重试）；
    表结构错、CHECK 错、磁盘 I/O 错、别的约束 → 这一类（重试也没用）。
    把后者谎报成冲突会让上层无限重试一个必然失败的操作。
    """

    def __init__(self, run_id: str, reason: str) -> None:
        self.run_id = run_id
        self.reason = reason
        super().__init__(f"LeadRun 写入失败: {run_id} ({reason})")


# --- LeadPlanner（25C）---


class LeadPlannerError(LeadAgentError):
    """LeadPlanner 领域异常基类。

    与执行/调度/Subagent 异常**分层**：调用方可以单独捕获 Planner 失败并
    决定「回到 PLANNING / 落到 FAILED / 暂停等待用户」，不会与
    `BudgetExceededError` / `BatchValidationError` / `RevisionLimitExceededError`
    混淆。
    """


class LeadPlannerSchemaError(LeadPlannerError):
    """模型输出**不是合法 JSON**或**不匹配 PlannerOutput 三选一 schema**。

    这是结构性失败：模型生成了非结构化文本，Planner 解析不到任何合法决策。
    触发重规划（attempt < max_attempts）时会附带原始文本 + 期望 schema 给模型
    重新生成。
    """

    def __init__(self, reason: str, *, raw_text: str = "") -> None:
        self.reason = reason
        self.raw_text = raw_text
        suffix = f"（原始: {raw_text[:200]!r}）" if raw_text else ""
        super().__init__(f"Planner 输出无法解析: {reason}{suffix}")


class LeadPlannerDeterministicError(LeadPlannerError):
    """Planner 输出**结构合法**但**确定性校验失败**（如 `validate_batch` 不通过）。

    区别于 `LeadPlannerSchemaError`：这里我们**有**一个结构化决策，但
    `validate_batch` / 字段域校验等规则拒绝了它。重规划时把**具体错误类型与
    消息**回填给模型，而不是只说"重写"。
    """

    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        self.message = message
        super().__init__(f"Planner 确定性校验失败 [{kind}]: {message}")


class LeadPlannerBudgetError(LeadPlannerError):
    """预算耗尽或 Budget 持久化钩子失败，**模型未被调用**。

    与 `BudgetExceededError` 同源不同语义：调用方拿到这个异常应当
    立即停止规划，而不是重试（重试也是同一种撞限）。
    """

    def __init__(self, reason: str, *, resource: str | None = None) -> None:
        self.reason = reason
        self.resource = resource
        suffix = f"（资源: {resource}）" if resource else ""
        super().__init__(f"Planner 预算不足: {reason}{suffix}")


class LeadPlannerMaxAttemptsError(LeadPlannerError):
    """`max_planning_attempts=2` 用尽仍失败。

    Planner 不直接修改 `LeadRunState`；调用方应当映射到 `LeadRunStatus.FAILED`
    并保留最后一次失败的错误信息。
    """

    def __init__(self, attempts: int, last_error: LeadPlannerError) -> None:
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"Planner 已用尽 {attempts} 次尝试仍失败：{type(last_error).__name__}: {last_error}"
        )
