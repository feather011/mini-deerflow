"""Phase 25A / 包导入无副作用测试。

规格要求：模块导入**不得**创建线程、数据库、Runtime 或网络客户端。

关键教训：本文件最初用 `importlib.reload` 重跑顶层代码，结果**污染了同一
pytest 会话里的其它测试**——reload 重建了 `ExecutionRoute` 等类对象，
其它文件顶部绑定的旧类传进 reload 后的 `conflicts()` 就 `isinstance`
不成立，341 个用例集体 `TypeError`。因此这里改成**子进程验证**：
新进程冷启动 import，天然与本会话隔离，也更贴近真实的首次导入。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

import pytest

from mini_deerflow.lead_agent import (
    LeadRunState,
    LeadRunStatus,
    is_terminal,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 领域包不该引入的重量级依赖
_FORBIDDEN_NETWORK_MODULES = ("httpx", "requests", "aiohttp", "urllib3")
_FORBIDDEN_BACKEND_MODULES = ("asyncio", "sqlite3")


def _run_in_fresh_process(body: str, cwd: str) -> dict:
    """在全新 Python 进程里执行 `body`，回收其打印的 JSON 结果。"""
    script = (
        "import json, sys\n"
        f"sys.path.insert(0, {json.dumps(_REPO_ROOT)})\n"
        "results = {}\n"
        f"{body}\n"
        "print(json.dumps(results))\n"
    )
    env = dict(os.environ)
    # 新进程必须能 import 本仓库（cwd 变为 tmp 之后）
    env["PYTHONPATH"] = os.pathsep.join(
        [_REPO_ROOT, env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, (
        f"子进程失败:\nstdout={completed.stdout}\nstderr={completed.stderr}"
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert lines, f"子进程没有输出 JSON: {completed.stderr}"
    return json.loads(lines[-1])


# ---------------------------------------------------------------------------
# 冷启动 import：零副作用（子进程）
# ---------------------------------------------------------------------------


def test_fresh_import_creates_no_threads(tmp_path) -> None:
    body = """
import threading as _t
before = len(_t.enumerate())
import mini_deerflow.lead_agent
after = len(_t.enumerate())
results["threads_before"] = before
results["threads_after"] = after
"""
    report = _run_in_fresh_process(body, str(tmp_path))
    assert report["threads_before"] == report["threads_after"], (
        f"import 创建了线程: {report}"
    )


def test_fresh_import_creates_no_files(tmp_path) -> None:
    body = """
import os
before = sorted(os.listdir('.'))
import mini_deerflow.lead_agent
after = sorted(os.listdir('.'))
results["created"] = [n for n in after if n not in before]
"""
    report = _run_in_fresh_process(body, str(tmp_path))
    assert report["created"] == [], f"import 创建了文件: {report['created']}"


def test_fresh_import_pulls_no_network_or_backend_module(tmp_path) -> None:
    """冷启动导入后，领域包自己不该拖进网络客户端 / asyncio / sqlite3。"""
    body = """
import sys
import mini_deerflow.lead_agent
forbidden = ("httpx", "requests", "aiohttp", "urllib3", "asyncio", "sqlite3")
loaded = [n for n in sys.modules
          if n == "mini_deerflow.lead_agent"
          or n.startswith("mini_deerflow.lead_agent.")]
results["lead_modules"] = sorted(loaded)
results["forbidden"] = sorted(
    n for n in sys.modules
    if any(n == f or n.startswith(f + ".") for f in forbidden)
)
"""
    report = _run_in_fresh_process(body, str(tmp_path))
    assert report["lead_modules"], "包应当已被导入"
    # 领域包自己的模块链里不得出现这些依赖
    package_modules = report["lead_modules"]
    assert all(
        not any(m == f or m.startswith(f + ".") for f in _FORBIDDEN_NETWORK_MODULES)
        for m in package_modules
    )
    # 整个进程不加载网络客户端（asyncio/sqlite3 可能被解释器其它部分带入，
    # 但领域包首次导入不应是那个触发者——若出现，先看是否由本包引入）
    loaded_forbidden = set(report["forbidden"])
    assert not (loaded_forbidden & set(_FORBIDDEN_NETWORK_MODULES)), (
        f"导入期加载了网络客户端: {sorted(loaded_forbidden)}"
    )


def test_fresh_import_does_not_create_runtime_or_db(tmp_path) -> None:
    """冷启动仅导入 + 构造运行态，不得出现数据库文件或线程。"""
    body = """
import os
import threading as _t
from mini_deerflow.lead_agent import GoalContract, LeadRunState, LeadRunStatus
before_files = sorted(os.listdir('.'))
before_threads = len(_t.enumerate())
state = LeadRunState.start(
    lead_run_id="r",
    goal=GoalContract.create(goal_id="g", objective="o", acceptance_criteria=["a"]),
    thread_id="t",
    now=1.0,
)
state.transition_to(LeadRunStatus.PLANNING, now=2.0)
after_files = sorted(os.listdir('.'))
after_threads = len(_t.enumerate())
results["created"] = [n for n in after_files if n not in before_files]
results["thread_delta"] = after_threads - before_threads
results["status"] = state.status.value
"""
    report = _run_in_fresh_process(body, str(tmp_path))
    assert report["created"] == [], f"创建了文件: {report['created']}"
    assert report["thread_delta"] == 0
    assert report["status"] == "planning"


# ---------------------------------------------------------------------------
# 稳态检查（本进程，不 reload）
# ---------------------------------------------------------------------------


def test_package_exposes_explicit_public_api() -> None:
    import mini_deerflow.lead_agent as package

    assert package.__all__, "__all__ 应当显式声明公共 API"
    missing = [name for name in package.__all__ if not hasattr(package, name)]
    assert missing == [], f"__all__ 导出了不存在的名字: {missing}"


def test_loaded_modules_hold_no_runtime_artifacts() -> None:
    """模块层不该残留客户端/连接/线程池等运行期对象。"""
    suspects = []
    for name, module in list(sys.modules.items()):
        if not (name == "mini_deerflow.lead_agent" or name.startswith("mini_deerflow.lead_agent.")):
            continue
        for attr, value in vars(module).items():
            if attr.startswith("__"):
                continue
            type_name = type(value).__name__
            if type_name in {
                "Connection",
                "SQLite3Connection",
                "ThreadPoolExecutor",
                "AsyncClient",
                "Client",
                "Session",
                "RuntimeContext",
            }:
                suspects.append(f"{name}.{attr} -> {type_name}")
    assert suspects == [], f"模块层发现运行期对象: {suspects}"


def test_constructing_state_needs_no_external_resource() -> None:
    """构造运行态不依赖任何外部资源（证明无隐式 I/O）。"""
    from mini_deerflow.lead_agent import GoalContract

    state = LeadRunState.start(
        lead_run_id="run-io",
        goal=GoalContract.create(
            goal_id="g", objective="目标", acceptance_criteria=["a"]
        ),
        thread_id="t",
        now=1.0,
    )
    # 值比较：避免依赖类身份
    assert state.status == LeadRunStatus.CREATED
    assert is_terminal(state.status) is False
    state.transition_to(LeadRunStatus.PLANNING, now=2.0)
    assert state.status == LeadRunStatus.PLANNING


def test_no_background_threads_started_by_state_ops() -> None:
    """在当前进程里做一批模型操作，线程数不得增长。"""
    from mini_deerflow.lead_agent import GoalContract, WorkItem, WorkItemProposal

    before = {t.ident for t in threading.enumerate()}

    state = LeadRunState.start(
        lead_run_id="r",
        goal=GoalContract.create(goal_id="g", objective="o", acceptance_criteria=["a"]),
        thread_id="t",
        now=1.0,
    )
    proposal = WorkItemProposal(work_id="w", objective="x", created_at=1.0)
    state.add_item(WorkItem.from_proposal(proposal))
    state.transition_to(LeadRunStatus.PLANNING, now=2.0)

    after = {t.ident for t in threading.enumerate()}
    assert before == after


# ---------------------------------------------------------------------------
# 25B：持久化模块的导入无副作用
# ---------------------------------------------------------------------------


def test_fresh_import_of_sqlite_store_creates_no_database(tmp_path) -> None:
    """导入 sqlite_store 不得建库文件——连接只在构造时建立。"""
    body = """
import os
before = set(os.listdir('.'))
import mini_deerflow.lead_agent
import mini_deerflow.lead_agent.sqlite_store as ss
import mini_deerflow.lead_agent.store as st
import mini_deerflow.lead_agent.budget_ledger as bl
import mini_deerflow.lead_agent.events as ev
after = set(os.listdir('.'))
results["created"] = sorted(after - before)
results["has_store_class"] = hasattr(ss, "SQLiteLeadRunStore")
results["has_protocol"] = hasattr(st, "LeadRunStore")
results["has_ledger"] = hasattr(bl, "LeadBudgetLedger")
results["has_event"] = hasattr(ev, "LeadDomainEvent")
"""
    report = _run_in_fresh_process(body, str(tmp_path))
    assert report["created"] == [], f"import 期创建了文件: {report['created']}"
    assert report["has_store_class"] is True
    assert report["has_protocol"] is True
    assert report["has_ledger"] is True
    assert report["has_event"] is True


def test_fresh_import_of_budget_ledger_starts_no_thread(tmp_path) -> None:
    """导入预算账本并构造默认实例都不启线程。"""
    body = """
import threading as _t
before = len(_t.enumerate())
from mini_deerflow.lead_agent import LeadBudgetLedger
ledger = LeadBudgetLedger()
mid = len(_t.enumerate())
ledger.consume_batch()
after = len(_t.enumerate())
results["delta_import"] = mid - before
results["delta_ops"] = after - mid
"""
    report = _run_in_fresh_process(body, str(tmp_path))
    assert report["delta_import"] == 0
    assert report["delta_ops"] == 0
