"""Phase 25C（架构收口）LeadPlanner 与 Root Budget 之间的最小适配边界。

## 为什么需要这一层

`DefaultLeadPlanner` 直接吃 `Budget` 时（25C 初版），
`Budget.consume_model_call` 只是内存字段自增——
**没有持久化、没有跨进程恢复、没有"钩子失败即不调模型"的契约**。
25D 把 Planner 接上真实 `LeadRunStore` 时，必须有一个**显式接口**来：

1. 定义「Planner 调用模型前的扣费契约」—— `RootBudget.consume_model_call()`
2. 把"已消耗额度"作为值快照暴露给 `BudgetSnapshot`
3. 让 LeadRunStore 持久化在 Planner 这条路径上**也**生效（通过
   `LeadBudgetLedger.bind_persistence` 已有路径走 `consume_direct`）

## 不做的事

- 不重写 `Budget` 或 `LeadBudgetLedger`
- 不在 Planner 路径上挂 `BudgetMiddleware`（避免 25D 接入 Orchestrator 后双扣）
- 不让 Planner 写 Store、Lease、LeadRunState
- 不引入 LangGraph / async
"""

from __future__ import annotations

from typing import ClassVar, Protocol, runtime_checkable

from .budget_ledger import LeadBudgetLedger

__all__ = [
    "BudgetRootBudget",
    "LeadLedgerRootBudget",
    "RootBudget",
]


# ---------------------------------------------------------------------------
# 契约
# ---------------------------------------------------------------------------


@runtime_checkable
class RootBudget(Protocol):
    """LeadPlanner 可见的 Root Budget 接口。

    `consume_model_call` 必须在调用模型**之前**完成：
    - 扣减该 LeadRun 已分得的 model_calls 额度
    - 把扣减额持久化到该 LeadRun 的预算账本（重启后能恢复）
    - 持久化失败 → 抛 `BudgetExceededError` **或** 其子类以外的领域异常；
      Planner 把它包成 `LeadPlannerBudgetError`，并保证 `model.decide` 未发生

    `snapshot()` 返回不可变值快照（**不**持 Budget 引用）供 Planner 拼上下文。
    """

    def consume_model_call(self) -> None: ...

    def snapshot(self) -> Any: ...


# ---------------------------------------------------------------------------
# 适配器 1：纯内存（向后兼容 25C 初版测试）
# ---------------------------------------------------------------------------


class BudgetRootBudget:
    """把仓库既有 `Budget` 适配成 `RootBudget`——**不**持久化（保留 25C 初版行为）。

    适合 25C 测试与"还没接 LeadRunStore 的临时用法"；生产 LeadRun 应走
    `LeadLedgerRootBudget`。两条路径**不**可混——一个 Planner 一辈子只用
    一个 RootBudget，避免双扣。

    ## 装配边界（25D 集成契约）

    **`_FOR_TESTING_ONLY = True`** —— 仅供 25C 测试与本地 mock 使用。
    25E 集成层（Orchestrator 构造 Planner）应**强制**校验
    `isinstance(root_budget, LeadLedgerRootBudget)`，否则拒绝装配。
    该标记**不是**类型系统的强制（用 `ClassVar` 声明）——它是**契约
    信号**，靠集成层 `isinstance` 检查与 AST 守卫（见
    `test_budget_root_budget_marked_for_testing_only`）一起守住。
    """

    # 装配边界信号：见上方 docstring
    _FOR_TESTING_ONLY: ClassVar[bool] = True

    def __init__(self, budget: Any) -> None:
        # Budget 的 _lock / _model_calls 在原对象内；这里只持引用
        self._budget = budget

    def consume_model_call(self) -> None:
        self._budget.consume_model_call()

    def snapshot(self) -> Any:
        # 复用 planner 的 build_budget_snapshot 工厂（懒加载以避免循环依赖）
        from .planner import build_budget_snapshot

        return build_budget_snapshot(self._budget)

    # 便捷读方法：测试期间经常需要断言已用量——
    # 通过适配器直接透传，不让调用方去摸私有字段。
    def model_calls(self) -> int:
        return self._budget.model_calls()

    def tool_calls(self) -> int:
        return self._budget.tool_calls()


# ---------------------------------------------------------------------------
# 适配器 2：LeadLedger（持久化路径，给 25D 用）
# ---------------------------------------------------------------------------


class LeadLedgerRootBudget:
    """把 25B 的 `LeadBudgetLedger` 适配成 `RootBudget`——**有**持久化。

    实现路径：
    - `consume_model_call` 走 `LeadBudgetLedger.consume_direct("model_calls")`
      该路径在 budget_ledger.py 已经实现 copy-on-write：
      预检 → 算候选快照 → 调 `persist(candidate)` → 成功才回写内存
    - 若 Orchestrator 已经 `bind_persistence(lambda state: store.save_ledger_state(...))`，
      `persist` 失败 → `LeadBudgetLedger.consume_direct` 抛错 → Planner 收上来
      包成 `LeadPlannerBudgetError`，模型**未被调用**（因为 Planner 在 `consume`
      返回后才调 `model.decide`，见 planner.py:714）
    - 重启恢复路径：Orchestrator `LeadBudgetLedger.from_state(persisted)` → 调用
      `resources.restore_usage(...)` 把上次写库的 `model_calls_used` 回填

    注意：`LeadBudgetLedger.consume_direct("model_calls")` 还会顺带把
    `root_usage["model_calls"]` 加 1——这正是我们要的语义（每次真实模型调用
    必须在 Root Budget 上记账一次）。
    """

    def __init__(self, ledger: LeadBudgetLedger) -> None:
        if not isinstance(ledger, LeadBudgetLedger):
            raise TypeError(
                f"ledger 必须是 LeadBudgetLedger，收到 {type(ledger).__name__}"
            )
        self._ledger = ledger

    def consume_model_call(self) -> None:
        # consume_direct("model_calls") 内部已经做：
        #   1. 校验 usage + outstanding + amount ≤ limit
        #   2. 若 bind_persistence 已挂，先调 persist(candidate)，失败则抛
        #   3. 成功才回写内存
        # 任何步骤抛错（包括 BudgetExceededError、persist 抛的 RuntimeError）
        # 都会原样传播，Planner 在调用 model.decide 之前就会拿到。
        self._ledger.consume_direct("model_calls", 1)

    def snapshot(self) -> Any:
        # LeadBudgetLedger 没有 build_budget_snapshot 这种工厂——
        # 但 LeadBudgetLedger.resources 就是 Budget，所以走同样的接口
        from .planner import build_budget_snapshot

        return build_budget_snapshot(self._ledger.resources)
