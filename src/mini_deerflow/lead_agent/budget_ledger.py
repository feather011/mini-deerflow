"""Phase 25B 持久化预算账本：根预算 + WorkItem 租约。

## 为什么不是第三套 Budget

本模块**不重新定义** model_calls / tool_calls / subagent_tasks / steps ——
那些继续由 `middleware.Budget`（项目唯一实现）持有与原子消耗，本模块通过
`LeadBudgetLedger.resources` **持有同一个实例**。Subagent 已经用
`shared_budget` 展示了同款复用（`SubagentBudget` 只是它的别名）。

## 根额度与租约的原子联动（关键不变量）

编排层新增一条**根信用额度**，WorkItem 的租约从它里面预留：

```
available = max_lease_credits
          - leased_consumed                      # 已被租约实际消费
          - Σ(active_lease.remaining_reserved)  # 已预留但还没花掉的
```

`BudgetLease.consume()` **不是**只改自己那一行——它必须在同一把锁内
先向根账本扣减，根账本扣不动就整笔失败：

```
lease._lock  持有
  └─ ledger._lock  持有
       ├─ 校验 available >= amount
       ├─ leased_consumed += amount
       └─ _persist()      # 落库；失败即抛错，外部调用不得继续
  lease.consumed += amount
```

因此「两个 Lease 各尝试消费 3、根额度 4」时，合计成功消费**必然 ≤ 4**：
第二个要么开不出预留，要么消费时撞 `available`。

`close()` 只释放 `remaining_reserved`，**`consumed` 永不归还**——
已经花掉的额度是既成事实，归还它会让重试风暴无限透支根额度。

## 两套视图，刻意不混

| 使用者 | 拿到什么 | 扣在哪 |
|---|---|---|
| Planner / Reviewer / Synthesizer | `ledger.resources`（现有 `Budget`） | 根资源计数，不占信用额度 |
| Executor / Subagent | `lease.view()`（`LeasedBudgetView`） | 该租约，进而联动根信用 |

`LeasedBudgetView` 实现了现有 `Budget` 消费方用到的 `consume_*` 接口，
但**只落到租约**——绝不允许同一笔消耗既扣 lease 又扣根 `resources`
（那是双扣，会让两个维度同时虚增）。

## 层次

```
LeadBudgetLedger（根账本，可持久化）
  ├── resources: Budget                 现有资源（不占信用额度）
  ├── 编排计数 + 并发槽                  原子 consume
  ├── 信用额度 max_lease_credits         租约的来源
  └── _leases: {lease_id: BudgetLease}
        └── view() -> LeasedBudgetView  Budget 兼容消费接口
```

本阶段只提供「关闭中断租约并归还未使用额度」的**基础操作**：
**不判断外部副作用是否已经发生**——那要结合审计事件（25F）再决定回滚还是重放。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from mini_deerflow.middleware import Budget, BudgetExceededError

from .errors import LeaseNotFoundError

__all__ = [
    "DEFAULT_MAX_BATCHES",
    "DEFAULT_MAX_CONCURRENCY",
    "DEFAULT_MAX_ITEMS_PER_BATCH",
    "DEFAULT_MAX_LEASE_CREDITS",
    "DEFAULT_MAX_REVISIONS_PER_ITEM",
    "DEFAULT_MAX_WALL_TIME_SECONDS",
    "DEFAULT_MAX_WORK_ITEMS",
    "BudgetLease",
    "LeadBudgetLedger",
    "LeasedBudgetView",
]

# 默认编排限制。测试**独立**写死期望值，不 import 这些常量——
# 否则改默认值会让测试跟着一起「通过」。
DEFAULT_MAX_BATCHES = 4
DEFAULT_MAX_WORK_ITEMS = 12
DEFAULT_MAX_ITEMS_PER_BATCH = 4
DEFAULT_MAX_CONCURRENCY = 2
DEFAULT_MAX_REVISIONS_PER_ITEM = 1
DEFAULT_MAX_WALL_TIME_SECONDS = 600.0
# 根信用额度：所有 WorkItem 租约的**总**消费量上限。
# 默认值未由规格指定，取一个足以覆盖默认 12 个 WorkItem 的量级。
DEFAULT_MAX_LEASE_CREDITS = 64

# ---------------------------------------------------------------------------
# 现有 Budget 资源 → 本模块使用的资源键
# ---------------------------------------------------------------------------
#
# 资源键直接用 `Budget` 的**计数器名**（`model_calls()` 等），
# 这样「available[r] = limit[r] - root_usage[r] - Σlease.remaining[r]」
# 三段都能在同名字段上读出，不需要脑内翻译。

#: 资源键 → Budget 的 max_* 属性名
RESOURCE_MAX_ATTR: dict[str, str] = {
    "steps": "max_steps",
    "model_calls": "max_model_calls",
    "tool_calls": "max_tool_calls",
    "subagent_tasks": "max_subagent_tasks",
    "summary_calls": "max_summary_calls",
}
#: 资源键 → Budget 的只读计数器方法名
RESOURCE_USAGE_METHOD: dict[str, str] = {
    "steps": "steps",
    "model_calls": "model_calls",
    "tool_calls": "tool_calls",
    "subagent_tasks": "subagent_tasks",
    "summary_calls": "summary_calls",
}
#: 资源键 → Budget 的原子 consume 方法名（显式映射，不做字符串推导：
#: `consume_subagent_task(depth)` 需要额外参数，推导法会漏掉这个差异）
RESOURCE_CONSUME_METHOD: dict[str, str] = {
    "steps": "consume_step",
    "model_calls": "consume_model_call",
    "tool_calls": "consume_tool_call",
    "subagent_tasks": "consume_subagent_task",
    "summary_calls": "consume_summary_call",
}

#: 租约可预留的资源（规格点名的四类；summary_calls 属压缩路径，
#: 由 Compactor 直接走根 Budget，不进租约）
LEASE_RESOURCES: tuple[str, ...] = (
    "steps",
    "model_calls",
    "tool_calls",
    "subagent_tasks",
)


# ---------------------------------------------------------------------------
# WorkItem 租约
# ---------------------------------------------------------------------------


@dataclass
class BudgetLease:
    """一个 Run 内、单个 WorkItem 的信用租约。

    生命周期：`open → consume* → close`。三态量满足恒等式
    `reserved == consumed + returned`（close 之后）。

    - **只能属于一个 run / work item**（构造时绑定，不可转让）
    - **Lease 内不得超过 reserved**（超了抛 `BudgetExceededError`）
    - **消费必须联动根账本**：根额度不足时整笔失败，绝不只减自己
    - **close 只归还未使用部分**（`reserved - consumed`）
    - **consumed 永远不能被归还**
    - **重复关闭幂等**：第二次 `close()` 返回 0
    """

    lease_id: str
    run_id: str
    work_id: str
    reserved: int
    opened_at: float = field(default_factory=time.time)
    consumed: int = 0
    returned: int = 0
    # 资源级预留/消费：`{"model_calls": 5, "tool_calls": 3, ...}`
    # 规格要求按资源类型分别维护——model 的预留不该占掉 tool 的额度。
    reserved_by_resource: dict[str, int] = field(default_factory=dict)
    consumed_by_resource: dict[str, int] = field(default_factory=dict)
    closed_at: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # 根账本回引用。consume() 必须在锁内经它扣减根 Budget——
    # 只改 self.consumed_by_resource 无法证明根预算不超卖。
    _ledger: "LeadBudgetLedger | None" = field(default=None, repr=False)

    def __post_init__(self) -> None:
        for name in ("lease_id", "run_id", "work_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} 不能为空")
        if isinstance(self.reserved, bool) or not isinstance(self.reserved, int):
            raise ValueError("reserved 必须是整数")
        if self.reserved < 0:
            raise ValueError(f"reserved 不能为负: {self.reserved}")
        for bucket_name, bucket in (
            ("reserved_by_resource", self.reserved_by_resource),
            ("consumed_by_resource", self.consumed_by_resource),
        ):
            if not isinstance(bucket, dict):
                raise ValueError(f"{bucket_name} 必须是字典")
            for key, value in bucket.items():
                if not isinstance(key, str) or not key:
                    raise ValueError(f"{bucket_name} 的键必须是非空字符串")
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"{bucket_name}[{key}] 必须是非负整数")

    @classmethod
    def open(
        cls,
        *,
        run_id: str,
        work_id: str,
        reserved: int,
        reserved_by_resource: dict[str, int] | None = None,
        now: float | None = None,
    ) -> "BudgetLease":
        resources = dict(reserved_by_resource or {})
        for key, value in resources.items():
            if key not in RESOURCE_CONSUME_METHOD:
                raise ValueError(f"未知资源: {key}")
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"资源 {key} 的预留必须是非负整数")
        return cls(
            lease_id=f"lease-{uuid.uuid4().hex[:12]}",
            run_id=run_id,
            work_id=work_id,
            reserved=reserved,
            reserved_by_resource=resources,
            opened_at=now if now is not None else time.time(),
        )

    # --- 状态查询 ---

    @property
    def active(self) -> bool:
        return self.closed_at is None

    @property
    def remaining_reserved(self) -> int:
        """已预留但尚未消费的**总信用**；它仍占着根信用额度，直到被消费或归还。"""
        with self._lock:
            return self.reserved - self.consumed

    def remaining_reserved_for(self, resource: str) -> int:
        """某资源上「已预留但未消费」的量——它占着该资源的根额度。"""
        with self._lock:
            return max(
                self.reserved_by_resource.get(resource, 0)
                - self.consumed_by_resource.get(resource, 0),
                0,
            )

    @property
    def is_exhausted(self) -> bool:
        with self._lock:
            return self.consumed >= self.reserved

    def view(self) -> "LeasedBudgetView":
        """返回 `Budget` 兼容的消费视图，只落在这条租约上。"""
        return LeasedBudgetView(self)

    # --- 操作 ---

    def consume(self, resource: str, amount: int = 1) -> None:
        """按资源原子扣减：**同时**动本租约与根 Budget，根超限则整笔失败。

        顺序（规格 6 的六步）：

        1. 校验该资源的剩余预留 `consumed[r] + amount <= reserved[r]`（本锁内）
        2. 校验根额度与通用信用池（账本锁内，只读，不动内存）
        3. 按「成功后的样子」算出候选状态快照（仍不动内存）
        4. 持久化钩子落库；抛错即整体失败，内存一处未变
        5. 落库成功 → 回写：根 Budget `consume_*()` + 信用 + 本租约计数
        6. 返回后调用方才允许真正调模型/工具

        锁序固定：`interrupt_all_leases()` 先在 ledger 锁内取快照再释放，
        才逐个 close，因此不会与本方法形成反向锁序。

        Raises:
            ValueError: amount 非正整数、或资源未登记。
            BudgetExceededError: 超过本租约该资源预留、根资源额度不足、或已关闭。
        """
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise ValueError("amount 必须是整数")
        if amount <= 0:
            raise ValueError(f"amount 必须为正: {amount}")
        if resource not in RESOURCE_CONSUME_METHOD:
            raise ValueError(f"未知资源: {resource}")
        with self._lock:
            if self.closed_at is not None:
                raise BudgetExceededError(
                    f"lease:{self.lease_id} 已关闭", self.reserved_by_resource.get(resource)
                )
            # 资源级预留优先；未单独预留的资源回退到通用信用约束
            # （通用 credit 是规格允许的「额外限制」，不替代资源级语义）
            if resource in self.reserved_by_resource:
                reserved = self.reserved_by_resource[resource]
                if self.consumed_by_resource.get(resource, 0) + amount > reserved:
                    raise BudgetExceededError(
                        f"lease:{self.lease_id}:{resource}", reserved
                    )
            else:
                if self.consumed + amount > self.reserved:
                    raise BudgetExceededError(
                        f"lease:{self.lease_id}", self.reserved
                    )

            ledger = self._ledger
            if ledger is not None:
                # 第 4~5 步全部交给账本：它在**自己锁内**先算出「本次成功后的
                # 候选快照」、先落库、落库成功才回写内存（含本租约的计数）。
                # 钩子抛错 → 这里原样抛出，内存一处未动。
                ledger.commit_lease_charge(self, resource, amount)
            else:
                # 没挂账本的租约（纯内存用法）只改自己
                self.consumed_by_resource[resource] = (
                    self.consumed_by_resource.get(resource, 0) + amount
                )
                self.consumed += amount

    def close(self, *, now: float | None = None) -> int:
        """关闭租约，归还未使用额度。幂等：重复关闭返回 0。

        资源级归还**不需要额外动作**：`available[r]` 只统计 `active` 租约的
        `remaining_reserved_for(r)`，一旦 `closed_at` 落上，该租约的资源预留
        自动退出占用。`reserved_by_resource` / `consumed_by_resource`
        保留原值不清理——那是审计事实，删了就查不出「这次跑花掉多少」。

        Returns:
            本次归还的总信用（首次 = reserved - consumed，之后恒为 0）。
            **consumed 永不归还。**
        """
        with self._lock:
            if self.closed_at is not None:
                return 0
            amount = self.reserved - self.consumed
            self.returned = amount
            self.closed_at = now if now is not None else time.time()
            return amount

    def to_dict(self) -> dict[str, object]:
        return {
            "lease_id": self.lease_id,
            "run_id": self.run_id,
            "work_id": self.work_id,
            "reserved": self.reserved,
            "consumed": self.consumed,
            "returned": self.returned,
            "reserved_by_resource": dict(self.reserved_by_resource),
            "consumed_by_resource": dict(self.consumed_by_resource),
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "BudgetLease":
        if not isinstance(data, dict):
            raise ValueError("BudgetLease 必须是字典")
        closed_at = data.get("closed_at")
        raw_reserved = data.get("reserved_by_resource", {})
        raw_consumed = data.get("consumed_by_resource", {})
        if not isinstance(raw_reserved, dict) or not isinstance(raw_consumed, dict):
            raise ValueError("reserved_by_resource/consumed_by_resource 必须是字典")
        lease = cls(
            lease_id=str(data.get("lease_id", "")),
            run_id=str(data.get("run_id", "")),
            work_id=str(data.get("work_id", "")),
            reserved=int(data.get("reserved", 0)),
            consumed=int(data.get("consumed", 0)),
            returned=int(data.get("returned", 0)),
            reserved_by_resource={str(k): int(v) for k, v in raw_reserved.items()},
            consumed_by_resource={str(k): int(v) for k, v in raw_consumed.items()},
            opened_at=float(data.get("opened_at", 0.0)),
            closed_at=float(closed_at) if closed_at is not None else None,
        )
        if lease.consumed < 0 or lease.returned < 0:
            raise ValueError("consumed/returned 不能为负")
        if lease.consumed > lease.reserved:
            raise ValueError(
                f"consumed({lease.consumed}) 超过 reserved({lease.reserved})"
            )
        # 资源级恒等式：**只校验显式声明过预留的资源**。
        # 未单独预留的资源走通用信用池，其 consumed_by_resource 是审计值，
        # 拿 0 当上限会把合法的通用消费判成损坏数据。
        for key, limit in lease.reserved_by_resource.items():
            used = lease.consumed_by_resource.get(key, 0)
            if used > limit:
                raise ValueError(
                    f"资源 {key} consumed({used}) 超过 reserved({limit})"
                )
        if lease.closed_at is not None and lease.returned != (
            lease.reserved - lease.consumed
        ):
            raise ValueError("已关闭租约的 returned 必须等于 reserved - consumed")
        return lease


class LeasedBudgetView:
    """租约的 `Budget` 兼容视图——按**资源类型**联动根 Budget。

    Executor / Subagent 拿它当 Budget 用（`consume_model_call()` 等）。
    每次消费同时落在两处：

    - **本租约**该资源的 `consumed[r]`（受预留约束）
    - **根 Budget** 同名资源（受 `max_*` 约束）

    这是「按资源类型维护租约预留」的关键：`model_calls` 的预留**不占**
    `tool_calls` 的额度，而租约的模型调用**必须**计入根 `max_model_calls`——
    否则 Executor 能靠租约绕过全局限制，把全局 Budget 变成摆设。

    既不是「只落租约」（漏扣根），也不是「两处各扣一次独立额度」（双扣）：
    同一笔消耗在根上只扣一次，在租约上受预留约束。
    """

    __slots__ = ("_lease",)

    def __init__(self, lease: BudgetLease) -> None:
        self._lease = lease

    @property
    def lease(self) -> BudgetLease:
        return self._lease

    def _consume(self, resource: str, amount: int = 1) -> None:
        self._lease.consume(resource, amount)

    def consume_step(self) -> None:
        self._consume("steps")

    def consume_model_call(self) -> None:
        self._consume("model_calls")

    def consume_tool_call(self) -> None:
        self._consume("tool_calls")

    def consume_summary_call(self) -> None:
        # 摘要压缩通常不进租约；预留为 0 时会立刻抛 BudgetExceededError，
        # 由调用方改用 ledger.resources 直扣
        self._consume("summary_calls")

    def consume_subagent_task(self, depth: int = 1) -> None:
        # 深度上限由根 Budget 的 max_subagent_depth 管；这里只记资源与信用
        self._consume("subagent_tasks")

    # --- 读取器：返回**本租约**该资源的用量（不是总信用） ---

    def _used(self, resource: str) -> int:
        with self._lease._lock:
            return self._lease.consumed_by_resource.get(resource, 0)

    def steps(self) -> int:
        return self._used("steps")

    def model_calls(self) -> int:
        return self._used("model_calls")

    def tool_calls(self) -> int:
        return self._used("tool_calls")

    def subagent_tasks(self) -> int:
        return self._used("subagent_tasks")

    def summary_calls(self) -> int:
        return self._used("summary_calls")

    def total_provider_calls(self) -> int:
        with self._lease._lock:
            return (
                self._lease.consumed_by_resource.get("model_calls", 0)
                + self._lease.consumed_by_resource.get("summary_calls", 0)
            )


# ---------------------------------------------------------------------------
# 根预算账本
# ---------------------------------------------------------------------------


@dataclass
class LeadBudgetLedger:
    """Lead 编排层的根预算账本（可持久化、可恢复）。

    `resources` 持有**现有的** `middleware.Budget` 实例——模型、工具、
    Subagent 的消耗全部记在它上面，本模块不复制那些字段。
    """

    # 编排限制（默认值见模块常量）
    max_batches: int = DEFAULT_MAX_BATCHES
    max_work_items: int = DEFAULT_MAX_WORK_ITEMS
    max_items_per_batch: int = DEFAULT_MAX_ITEMS_PER_BATCH
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    max_revisions_per_item: int = DEFAULT_MAX_REVISIONS_PER_ITEM
    max_wall_time_seconds: float = DEFAULT_MAX_WALL_TIME_SECONDS
    #: 所有 WorkItem 租约的**总**信用额度（跨 run 共享的根池）
    max_lease_credits: int = DEFAULT_MAX_LEASE_CREDITS

    # 现有资源预算（复用，不重建）
    resources: Budget = field(default_factory=Budget)

    # --- 运行时计数（原子变更，随账本持久化） ---
    batches_used: int = 0
    items_used: int = 0
    concurrency_in_use: int = 0
    peak_concurrency: int = 0
    started_at: float | None = None
    #: 各租约**累计**已消费（与 Σlease.consumed 相等，作为根额度账本）
    leased_consumed: int = 0

    _leases: dict[str, BudgetLease] = field(default_factory=dict, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    #: 持久化钩子：收到**候选状态快照**（尚未写进内存的下一步）。
    #: 抛错即阻止后续模型/工具调用——外部操作必须在账本落库之后才允许发生。
    #: 钩子必须只做落库、**不得**反过来读本账本（否则会与本锁形成反向锁序）。
    _persist: Callable[[dict[str, Any]], None] | None = field(
        default=None, repr=False
    )

    def __post_init__(self) -> None:
        for name in (
            "max_batches",
            "max_work_items",
            "max_items_per_batch",
            "max_concurrency",
            "max_revisions_per_item",
            "max_lease_credits",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数，收到 {value!r}")
        if self.max_wall_time_seconds <= 0:
            raise ValueError(
                f"max_wall_time_seconds 必须为正: {self.max_wall_time_seconds}"
            )
        if not isinstance(self.resources, Budget):
            raise TypeError(
                f"resources 必须是现有 middleware.Budget，收到 {type(self.resources).__name__}"
            )

    # ------------------------------------------------------------------
    # 持久化钩子
    # ------------------------------------------------------------------

    def bind_persistence(
        self, persist: Callable[[dict[str, Any]], None] | None
    ) -> None:
        """绑定「账本变更必须先落库」的钩子。

        调用方（通常是集成层）传 `lambda state: store.save_ledger_state(run_id, state)`。
        钩子收到的是**候选状态**（本次扣减成功后会是的样子），而不是账本本身：

        ```
        预检 → 算候选快照 → 钩子落库 → 成功才回写内存
        ```

        钩子抛错 → 消费抛错 → 调用方拿不到额度，外部模型/工具调用不会发生；
        同时内存用量与数据库用量**都还停在扣减前**。
        """
        if persist is not None and not callable(persist):
            raise TypeError("persist 必须是可调用对象或 None")
        with self._lock:
            self._persist = persist

    # ------------------------------------------------------------------
    # 资源级根额度（规格 6 的不变量）
    # ------------------------------------------------------------------
    #
    #   available[r] = limit[r] - root_usage[r] - Σ(active_lease.remaining_reserved_for(r))

    def _resource_limit(self, resource: str) -> int | None:
        """根 Budget 上某资源的上限；None 表示不设限。"""
        return getattr(self.resources, RESOURCE_MAX_ATTR[resource])

    def _resource_usage(self, resource: str) -> int:
        """根 Budget 上某资源的已用量。"""
        return getattr(self.resources, RESOURCE_USAGE_METHOD[resource])()

    def _outstanding_reserved(self, resource: str) -> int:
        """所有**活动**租约在该资源上已预留未消费的总量（须持锁调用）。"""
        return sum(
            lease.remaining_reserved_for(resource)
            for lease in self._leases.values()
            if lease.active
        )

    def available_for(self, resource: str) -> int:
        """某资源当前还能分配的量（含被租约占住的部分）。"""
        if resource not in RESOURCE_MAX_ATTR:
            raise ValueError(f"未知资源: {resource}")
        limit = self._resource_limit(resource)
        if limit is None:
            # 该资源不设上限 → 视为无界（返回一个足够大的哨兵）
            return 2**31
        with self._lock:
            return limit - self._resource_usage(resource) - self._outstanding_reserved(resource)

    def commit_lease_charge(
        self, lease: "BudgetLease", resource: str, amount: int
    ) -> None:
        """租约消费：**先落库、后改内存**（copy-on-write）。

        由 `BudgetLease.consume` 在已持有 `lease._lock` 时调用，因此锁序恒为
        `lease._lock → ledger._lock`。整个「预检 → 算候选快照 → 落库 → 回写」
        都在同一个 ledger 锁段内完成，中途不会有别的账本操作插进来。

        钩子拿到的是**候选**状态（还没写进内存的下一步），不是 `self`：
        这样钩子失败时内存一处未变，数据库也还停在上一个一致状态，
        「内存改了但库没写」和「库写了但内存没改」两种撕裂都不会出现。

        应用阶段（落库成功之后）只做已预检过的扣减，不再可能因额度抛错。

        Raises:
            BudgetExceededError: 根额度或通用信用池不足。
            任何钩子抛出的异常: 原样上抛，内存与数据库均未推进。
        """
        if resource not in RESOURCE_CONSUME_METHOD:
            raise ValueError(f"未知资源: {resource}")
        with self._lock:
            # --- 阶段 1：预检（全部在扣减之前，失败即不碰内存） ---
            limit = self._resource_limit(resource)
            if limit is not None and self._resource_usage(resource) + amount > limit:
                raise BudgetExceededError(resource, limit)
            if self.leased_consumed + amount > self.max_lease_credits:
                raise BudgetExceededError("lease_credits", self.max_lease_credits)
            depth = 1
            if (
                resource == "subagent_tasks"
                and self.resources.max_subagent_depth is not None
                and depth > self.resources.max_subagent_depth
            ):
                raise BudgetExceededError(
                    "subagent_depth", self.resources.max_subagent_depth
                )

            # --- 阶段 2：候选快照 + 落库（仍不动内存） ---
            persist = self._persist
            if persist is not None:
                persist(
                    self._candidate_state(
                        root_add={resource: amount},
                        leased_delta=amount,
                        lease_id=lease.lease_id,
                        lease_add={resource: amount},
                    )
                )

            # --- 阶段 3：落库成功，才回写内存 ---
            # `Budget.consume_*` 一次只扣 1 且自带原子上限检查，故逐次调用；
            # 上面已按 `usage + amount <= limit` 预检，循环不会中途抛错。
            consume = getattr(self.resources, RESOURCE_CONSUME_METHOD[resource])
            for _ in range(amount):
                consume(depth) if resource == "subagent_tasks" else consume()
            self.leased_consumed += amount
            lease.consumed_by_resource[resource] = (
                lease.consumed_by_resource.get(resource, 0) + amount
            )
            lease.consumed += amount

    def consume_direct(
        self, resource: str, amount: int = 1, *, depth: int = 1
    ) -> None:
        """**非租约**消费者（Planner / Reviewer / Synthesizer）扣根资源。

        必须先确认活动租约预留之外还有余量——否则会把已经租出去的额度
        抢走，让后面执行到一半的 WorkItem 撞超限。

        与 `commit_lease_charge` 同一套 copy-on-write 顺序：
        预检 → 候选快照 → 落库 → 成功才回写内存。
        """
        if resource not in RESOURCE_CONSUME_METHOD:
            raise ValueError(f"未知资源: {resource}")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise ValueError(f"amount 必须为正整数: {amount!r}")
        with self._lock:
            limit = self._resource_limit(resource)
            if limit is not None:
                usage = self._resource_usage(resource)
                outstanding = self._outstanding_reserved(resource)
                if usage + outstanding + amount > limit:
                    raise BudgetExceededError(resource, limit)
            if (
                resource == "subagent_tasks"
                and self.resources.max_subagent_depth is not None
                and depth > self.resources.max_subagent_depth
            ):
                raise BudgetExceededError(
                    "subagent_depth", self.resources.max_subagent_depth
                )

            persist = self._persist
            if persist is not None:
                persist(self._candidate_state(root_add={resource: amount}))

            consume = getattr(self.resources, RESOURCE_CONSUME_METHOD[resource])
            for _ in range(amount):
                consume(depth) if resource == "subagent_tasks" else consume()

    def _candidate_state(
        self,
        *,
        root_add: dict[str, int],
        leased_delta: int = 0,
        lease_id: str | None = None,
        lease_add: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """算出「本次扣减成功后」的状态快照，**不修改任何内存对象**。

        只能在已持有 `self._lock` 时调用；返回值可直接 JSON 序列化。
        """
        state = self.to_state()
        resources = state["resources"]
        for resource, delta in root_add.items():
            key = RESOURCE_USAGE_METHOD[resource]
            resources[key] = int(resources.get(key, 0)) + delta
        state["leased_consumed"] = int(state["leased_consumed"]) + leased_delta
        if lease_id is not None:
            for item in state["leases"]:
                if item.get("lease_id") != lease_id:
                    continue
                per_resource = dict(item.get("consumed_by_resource") or {})
                for resource, delta in (lease_add or {}).items():
                    per_resource[resource] = per_resource.get(resource, 0) + delta
                item["consumed_by_resource"] = per_resource
                item["consumed"] = int(item.get("consumed", 0)) + sum(
                    (lease_add or {}).values()
                )
        return state

    # ------------------------------------------------------------------
    # 编排消耗（原子：锁内 检查 → 扣减 → 抛错）
    # ------------------------------------------------------------------

    def consume_batch(self) -> int:
        """消耗一个批次名额，返回新的已用批次数。"""
        with self._lock:
            if self.batches_used >= self.max_batches:
                raise BudgetExceededError("batches", self.max_batches)
            self.batches_used += 1
            return self.batches_used

    def consume_work_item(self, count: int = 1) -> int:
        """消耗 `count` 个工作项名额，返回新的已用总数。

        同时校验「总量」与「单批上限」——两条限制都必须在同一次锁内判定，
        否则并发两个批次会各自以为没到顶。
        """
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError(f"count 必须为正整数: {count!r}")
        with self._lock:
            if count > self.max_items_per_batch:
                raise BudgetExceededError(
                    "items_per_batch", self.max_items_per_batch
                )
            if self.items_used + count > self.max_work_items:
                raise BudgetExceededError("work_items", self.max_work_items)
            self.items_used += count
            return self.items_used

    def acquire_slot(self) -> int:
        """占用一个并发槽，返回占用后的在用数。"""
        with self._lock:
            if self.concurrency_in_use >= self.max_concurrency:
                raise BudgetExceededError("concurrency", self.max_concurrency)
            self.concurrency_in_use += 1
            self.peak_concurrency = max(
                self.peak_concurrency, self.concurrency_in_use
            )
            return self.concurrency_in_use

    def release_slot(self) -> int:
        """释放一个并发槽。低于 0 说明配对错了——不静默兜底，直接报错。"""
        with self._lock:
            if self.concurrency_in_use <= 0:
                raise ValueError("并发槽释放次数超过占用次数（acquire/release 未配对）")
            self.concurrency_in_use -= 1
            return self.concurrency_in_use

    def begin_wall_time(self, now: float | None = None) -> None:
        """记录 Run 起始时刻（幂等：只在第一次调用时设定）。"""
        with self._lock:
            if self.started_at is None:
                self.started_at = now if now is not None else time.time()

    def wall_time_exceeded(self, now: float | None = None) -> bool:
        """是否已超过编排墙钟上限（未 start 视为未超时）。"""
        with self._lock:
            if self.started_at is None:
                return False
            moment = now if now is not None else time.time()
            return (moment - self.started_at) >= self.max_wall_time_seconds

    # ------------------------------------------------------------------
    # 租约
    # ------------------------------------------------------------------

    @property
    def available(self) -> int:
        """当前还能分配出去的根信用额度。

        `available = max_lease_credits - leased_consumed - Σ(活动租约的 remaining_reserved)`

        「已预留未消费」也算占用：它随时可能被花掉，不预留就等于透支。
        """
        with self._lock:
            return self._available_locked()

    def _available_locked(self) -> int:
        reserved_outstanding = sum(
            lease.reserved - lease.consumed
            for lease in self._leases.values()
            if lease.active
        )
        return self.max_lease_credits - self.leased_consumed - reserved_outstanding

    def open_lease(
        self,
        *,
        run_id: str,
        work_id: str,
        reserved: int,
        reserved_by_resource: dict[str, int] | None = None,
        now: float | None = None,
    ) -> BudgetLease:
        """原子预留并建立租约（预留与登记在同一把锁内）。

        校验两层额度，缺一不可：

        - 通用信用 `reserved` ≤ `max_lease_credits` 的可用部分
        - **每个资源** `reserved_by_resource[r]` ≤ `available_for(r)`——
          这一条保证「创建 Lease 只减少 available，不增加 root_usage」，
          且两个线程抢同一批资源预留时不会都拿到名额

        Raises:
            BudgetExceededError: 任一层额度不足。
            ValueError: 预留为负或资源未登记。
        """
        if isinstance(reserved, bool) or not isinstance(reserved, int):
            raise ValueError("reserved 必须是整数")
        if reserved < 0:
            raise ValueError(f"reserved 不能为负: {reserved}")
        resources = dict(reserved_by_resource or {})
        for key, value in resources.items():
            if key not in RESOURCE_CONSUME_METHOD:
                raise ValueError(f"未知资源: {key}")
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"资源 {key} 的预留必须是非负整数")

        with self._lock:
            if reserved > self._available_locked():
                raise BudgetExceededError("lease_credits", self.max_lease_credits)
            for key, want in resources.items():
                if want > self._available_for_locked(key):
                    raise BudgetExceededError(f"resource:{key}", self._resource_limit(key))
            lease = BudgetLease.open(
                run_id=run_id,
                work_id=work_id,
                reserved=reserved,
                reserved_by_resource=resources,
                now=now,
            )
            lease._ledger = self
            # 与账本共用同一把锁：`consume` 持 lease 锁后要进账本锁，
            # 而 `available_for` 持账本锁后要读各租约——两把独立的锁会形成
            # 反向锁序。共用一把 RLock 后两条路径都变成可重入，无从死锁。
            lease._lock = self._lock
            self._leases[lease.lease_id] = lease
            return lease

    def _available_for_locked(self, resource: str) -> int:
        """某资源可用量（须持锁调用）。"""
        limit = self._resource_limit(resource)
        if limit is None:
            return 2**31
        return limit - self._resource_usage(resource) - self._outstanding_reserved(resource)

    def get_lease(self, lease_id: str) -> BudgetLease:
        with self._lock:
            lease = self._leases.get(lease_id)
        if lease is None:
            raise LeaseNotFoundError(lease_id)
        return lease

    def active_leases(
        self, *, run_id: str | None = None, work_id: str | None = None
    ) -> tuple[BudgetLease, ...]:
        """列出**未关闭**的租约（25F 安全恢复的查询入口）。"""
        with self._lock:
            return tuple(
                lease
                for lease in self._leases.values()
                if lease.active
                and (run_id is None or lease.run_id == run_id)
                and (work_id is None or lease.work_id == work_id)
            )

    def all_leases(self) -> tuple[BudgetLease, ...]:
        with self._lock:
            return tuple(self._leases.values())

    def interrupt_lease(self, lease_id: str, *, now: float | None = None) -> int:
        """关闭一个中断的租约，归还未使用额度。

        **本阶段只做额度归还，不判断外部副作用是否已经发生**——
        真实副作用的核对要结合审计事件（25F）再决定回滚还是重放。
        """
        lease = self.get_lease(lease_id)
        return lease.close(now=now)

    def interrupt_all_leases(self, *, now: float | None = None) -> int:
        """关闭全部活动租约（Run 级中断），返回归还总额度。

        先在 ledger 锁内取快照、释放锁，再逐个 close——
        否则锁序会与 `consume()` 的 `lease → ledger` 相反，造成死锁。
        """
        with self._lock:
            targets = [lease for lease in self._leases.values() if lease.active]
        total = 0
        for lease in targets:
            total += lease.close(now=now)
        return total

    # ------------------------------------------------------------------
    # 持久化状态（供 Store 快照 + 重启恢复）
    # ------------------------------------------------------------------

    @property
    def reserved_total(self) -> int:
        """所有租约预留额度之和（含已关闭的）。"""
        with self._lock:
            return sum(lease.reserved for lease in self._leases.values())

    @property
    def reserved_active(self) -> int:
        """仍在活动中的租约预留总额（当前真实占着的量）。"""
        with self._lock:
            return sum(lease.reserved for lease in self._leases.values() if lease.active)

    def to_state(self) -> dict[str, Any]:
        """导出可 JSON 序列化的账本状态（不含密钥与 Prompt）。"""
        with self._lock:
            return {
                "max_batches": self.max_batches,
                "max_work_items": self.max_work_items,
                "max_items_per_batch": self.max_items_per_batch,
                "max_concurrency": self.max_concurrency,
                "max_revisions_per_item": self.max_revisions_per_item,
                "max_wall_time_seconds": self.max_wall_time_seconds,
                "max_lease_credits": self.max_lease_credits,
                "batches_used": self.batches_used,
                "items_used": self.items_used,
                "concurrency_in_use": self.concurrency_in_use,
                "peak_concurrency": self.peak_concurrency,
                "started_at": self.started_at,
                "leased_consumed": self.leased_consumed,
                "leases": [lease.to_dict() for lease in self._leases.values()],
                # 现有资源 Budget 的当前用量（限值与计数）
                "resources": {
                    "max_steps": self.resources.max_steps,
                    "max_model_calls": self.resources.max_model_calls,
                    "max_tool_calls": self.resources.max_tool_calls,
                    "max_subagent_tasks": self.resources.max_subagent_tasks,
                    "max_subagent_depth": self.resources.max_subagent_depth,
                    "max_summary_calls": self.resources.max_summary_calls,
                    "steps": self.resources.steps(),
                    "model_calls": self.resources.model_calls(),
                    "tool_calls": self.resources.tool_calls(),
                    "subagent_tasks": self.resources.subagent_tasks(),
                    "summary_calls": self.resources.summary_calls(),
                },
            }

    @classmethod
    def from_state(
        cls,
        data: dict[str, Any],
        *,
        resources: Budget | None = None,
    ) -> "LeadBudgetLedger":
        """从持久化状态恢复账本（含活动租约与根信用用量）。

        `resources` 传入时复用调用方给的 Budget 并回填其用量
        （`Budget.restore_usage` 要求它是**全新未消费**的实例）；
        否则新建一个并按存档重建限值与计数。
        """
        if not isinstance(data, dict):
            raise ValueError("账本状态必须是字典")
        raw_resources = data.get("resources", {})
        if not isinstance(raw_resources, dict):
            raise ValueError("resources 必须是字典")

        budget = resources if resources is not None else Budget(
            max_steps=raw_resources.get("max_steps"),
            max_model_calls=raw_resources.get("max_model_calls"),
            max_tool_calls=raw_resources.get("max_tool_calls"),
            max_subagent_tasks=raw_resources.get("max_subagent_tasks"),
            max_subagent_depth=raw_resources.get("max_subagent_depth"),
            max_summary_calls=raw_resources.get("max_summary_calls"),
        )
        # 恢复现有资源的用量（只做一次；Budget 自身会拒绝重复恢复）
        budget.restore_usage(
            steps=int(raw_resources.get("steps", 0)),
            model_calls=int(raw_resources.get("model_calls", 0)),
            tool_calls=int(raw_resources.get("tool_calls", 0)),
            subagent_tasks=int(raw_resources.get("subagent_tasks", 0)),
            summary_calls=int(raw_resources.get("summary_calls", 0)),
        )

        raw_leases = data.get("leases", [])
        if not isinstance(raw_leases, list):
            raise ValueError("leases 必须是数组")

        restored = cls(
            max_batches=int(data.get("max_batches", DEFAULT_MAX_BATCHES)),
            max_work_items=int(data.get("max_work_items", DEFAULT_MAX_WORK_ITEMS)),
            max_items_per_batch=int(
                data.get("max_items_per_batch", DEFAULT_MAX_ITEMS_PER_BATCH)
            ),
            max_concurrency=int(data.get("max_concurrency", DEFAULT_MAX_CONCURRENCY)),
            max_revisions_per_item=int(
                data.get("max_revisions_per_item", DEFAULT_MAX_REVISIONS_PER_ITEM)
            ),
            max_wall_time_seconds=float(
                data.get("max_wall_time_seconds", DEFAULT_MAX_WALL_TIME_SECONDS)
            ),
            max_lease_credits=int(
                data.get("max_lease_credits", DEFAULT_MAX_LEASE_CREDITS)
            ),
            resources=budget,
            batches_used=int(data.get("batches_used", 0)),
            items_used=int(data.get("items_used", 0)),
            concurrency_in_use=int(data.get("concurrency_in_use", 0)),
            peak_concurrency=int(data.get("peak_concurrency", 0)),
            leased_consumed=int(data.get("leased_consumed", 0)),
        )
        started = data.get("started_at")
        if started is not None:
            restored.started_at = float(started)
        for item in raw_leases:
            lease = BudgetLease.from_dict(item)  # type: ignore[arg-type]
            lease._ledger = restored
            lease._lock = restored._lock
            restored._leases[lease.lease_id] = lease
        return restored
