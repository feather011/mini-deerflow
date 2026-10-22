"""TOOL-07 R1：进程树强制回收的夹具证据。

审查意见 R1 的要求：进程组隔离保留，但"根进程退出"不能替代"整组退出"——
忽略 SIGTERM 的后代必须被强制回收。本文件用**伪造的 harness 入口**
（临时目录里的 `harness/noninteractive.py`）驱动真实的 HarnessRunner 通道：
`python -m harness.noninteractive` → 派生后代进程 → 后代写心跳文件。

不依赖真实 Provider、不外呼、不需要 harness 仓库：
伪造入口只做"派生子进程 + 写心跳"，与模型无关。

三个场景：
- normal      ：后代可被正常终止
- ignore_term ：后代忽略 SIGTERM（POSIX 语义；Windows 上由 Job 强杀覆盖）
- root_exits  ：根进程先退出，后代被孤儿化（"root 退出 ≠ 组退出"）

每个场景都断言：宿主进程存活、后代停止（心跳不再增长）、根进程被 reap、
项目锁已释放。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest

from mini_deerflow.harness_runner import HarnessRunner

# ---------------------------------------------------------------------------
# 伪造的 harness 入口
# ---------------------------------------------------------------------------

_FAKE_INIT = '"""伪造 harness 包（仅测试用）。"""\n'

_FAKE_NONINTERACTIVE = '''"""伪造的 harness 非交互入口（仅用于进程树回收夹具）。

真实 harness 需要 LLM 凭证；本夹具要证明的是"进程树被真正回收"，
与模型无关，因此用一个不联网的假入口：它派生一个后代进程
（后代持续写心跳文件），然后按 FAKE_TREE_MODE 决定自己的结局。
"""

import json
import os
import subprocess
import sys
import time

_CHILD = r"""
import os, signal, sys, time
mode = os.environ.get("FAKE_TREE_MODE", "normal")
work = os.environ["FAKE_TREE_DIR"]
if mode == "ignore_term":
    try:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    except (AttributeError, ValueError, OSError):
        pass
os.makedirs(work, exist_ok=True)
n = 0
while True:
    n += 1
    with open(os.path.join(work, "heartbeat.txt"), "w", encoding="utf-8") as fh:
        fh.write(str(n))
    if n == 1:
        with open(os.path.join(work, "child_ready.txt"), "w", encoding="utf-8") as fh:
            fh.write("ready")
    time.sleep(0.1)
"""


def _emit(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\\n")
    sys.stdout.flush()


def main():
    mode = os.environ.get("FAKE_TREE_MODE", "normal")
    work = os.environ.get("FAKE_TREE_DIR", "")
    child = subprocess.Popen([sys.executable, "-c", _CHILD])
    _emit({"type": "started", "task_id": "fake", "child_pid": child.pid})
    ready = os.path.join(work, "child_ready.txt")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not os.path.exists(ready):
        time.sleep(0.05)
    _emit({"type": "tool_start", "task_id": "fake", "seq": 1, "name": "bash"})
    if mode == "root_exits":
        # 根进程先死：后代被孤儿化（"root 退出 ≠ 组退出"）
        os._exit(0)
    _emit({"type": "done", "task_id": "fake", "tool_calls": 1})
    while True:
        time.sleep(0.1)


main()
'''


def _make_fake_harness_root(tmp_path: Path) -> Path:
    root = tmp_path / "fakeharness"
    pkg = root / "harness"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(_FAKE_INIT, encoding="utf-8")
    (pkg / "noninteractive.py").write_text(_FAKE_NONINTERACTIVE, encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# 夹具工具
# ---------------------------------------------------------------------------

def _heartbeat(work: Path) -> int:
    path = work / "heartbeat.txt"
    if not path.exists():
        return -1
    try:
        return int(path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return -1


def _wait_for(predicate, timeout: float, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _assert_descendant_stopped(work: Path, label: str) -> None:
    """后代必须停止：心跳计数在 1.2s 内不再增长。"""
    first = _heartbeat(work)
    assert first >= 1, f"{label}：后代从未启动（心跳文件缺失），夹具无效"
    time.sleep(1.2)
    second = _heartbeat(work)
    assert second == first, (
        f"{label}：后代仍在运行，心跳 {first} → {second}（进程树未被回收）"
    )


def _run_project_execute(runner, *, repo_path: Path, token, on_event=None):
    """通过真实 project_execute 入口执行一次（拿到项目锁语义）。"""
    from mini_deerflow.project_store import InMemoryProjectStore
    from mini_deerflow.project_tools import create_project_execute_tool
    from mini_deerflow.runtime import _get_project_lock
    from mini_deerflow.thread_store import InMemoryThreadStore

    unique = uuid.uuid4().hex[:8]
    pid = f"p-{unique}"
    tid = f"t-{unique}"

    store = InMemoryThreadStore()
    project_store = InMemoryProjectStore()
    project_store.create(
        project_id=pid, name="tree", project_root=repo_path,
        workspace_path=repo_path, legacy_repo_path=None,
    )
    store.create(tid, project_id=pid)

    tool = create_project_execute_tool(
        thread_store=store,
        project_store=project_store,
        runner=runner,
        get_lock=_get_project_lock,
    )
    ctx = type("Ctx", (), {
        "thread_id": tid, "run_id": f"r-{unique}", "project_id": pid,
        "cancellation_token": token, "on_event": on_event,
    })()
    raw = tool.handler({"task": "派生一个后代进程", "context": ""}, ctx)
    return json.loads(raw), pid


# ---------------------------------------------------------------------------
# R1 夹具
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["normal", "ignore_term"])
def test_取消_后代被强制回收_宿主存活_锁释放(tmp_path, mode):
    """取消后：后代停止（含忽略 SIGTERM 的）、根进程被 reap、锁释放、宿主存活。"""
    from mini_deerflow.middleware import CancellationToken
    from mini_deerflow.runtime import _get_project_lock

    work = tmp_path / "work"
    work.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    fake_root = _make_fake_harness_root(tmp_path)

    runner = HarnessRunner(
        harness_root=str(fake_root),
        python_exe=sys.executable,
        timeout_seconds=60.0,
        max_tool_calls=10,
    )
    runner.set_extra_env({"FAKE_TREE_MODE": mode, "FAKE_TREE_DIR": str(work)})

    token = CancellationToken()
    holder: dict = {}

    def _invoke():
        try:
            holder["result"], holder["pid"] = _run_project_execute(
                runner, repo_path=repo, token=token,
            )
        except Exception as exc:  # noqa: BLE001
            holder["error"] = exc

    thread = threading.Thread(target=_invoke, daemon=True)
    thread.start()

    # 等后代真正起来（ready 标记由后代自己写）
    assert _wait_for(lambda: (work / "child_ready.txt").exists(), 30.0), (
        f"[{mode}] 后代进程未在 30s 内启动，夹具无效"
    )
    assert _heartbeat(work) >= 1, f"[{mode}] 后代心跳未启动"

    token.cancel()
    thread.join(timeout=40)
    assert not thread.is_alive(), f"[{mode}] 取消后工具未在 40s 内返回"
    assert "error" not in holder, f"[{mode}] 工具异常: {holder.get('error')}"

    result = holder["result"]
    assert result.get("ok") is False, f"[{mode}] 取消后应 ok=false: {result}"
    assert result.get("status") == "stopped", (
        f"[{mode}] 终态应为 stopped，实际: {result.get('status')}"
    )

    # 后代必须停止（忽略 SIGTERM 的也必须被强制回收）
    _assert_descendant_stopped(work, f"[{mode}]")

    # 根进程被 reap
    proc = runner._last_process
    assert proc is not None, f"[{mode}] HarnessRunner 未记录子进程句柄"
    assert proc.poll() is not None, f"[{mode}] 根进程未被收割: pid={proc.pid}"

    # 项目锁已释放
    lock = _get_project_lock(holder["pid"])
    assert lock.acquire(blocking=False), f"[{mode}] 取消后项目锁未释放"
    lock.release()

    # 宿主存活：还能再起一个子进程（进程组没被误杀）
    import subprocess
    out = subprocess.run(
        [sys.executable, "-c", "print('host-alive')"],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0 and "host-alive" in out.stdout, (
        f"[{mode}] 宿主进程异常: {out.stdout!r} {out.stderr!r}"
    )


def test_根进程先退出_孤儿后代仍被回收(tmp_path):
    """根进程先死时，"root 退出"不能替代"整组退出"：后代必须被回收。"""
    from mini_deerflow.middleware import CancellationToken
    from mini_deerflow.runtime import _get_project_lock

    work = tmp_path / "work"
    work.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    fake_root = _make_fake_harness_root(tmp_path)

    runner = HarnessRunner(
        harness_root=str(fake_root),
        python_exe=sys.executable,
        timeout_seconds=60.0,
        max_tool_calls=10,
    )
    runner.set_extra_env({"FAKE_TREE_MODE": "root_exits", "FAKE_TREE_DIR": str(work)})

    token = CancellationToken()
    holder: dict = {}
    thread = threading.Thread(
        target=lambda: holder.update(
            zip(("result", "pid"), _run_project_execute(
                runner, repo_path=repo, token=token,
            ))
        ),
        daemon=True,
    )
    thread.start()

    assert _wait_for(lambda: (work / "child_ready.txt").exists(), 30.0), (
        "后代进程未在 30s 内启动，夹具无效"
    )
    thread.join(timeout=40)
    assert not thread.is_alive(), "根进程退出后工具未在 40s 内返回"

    result = holder["result"]
    assert result.get("ok") is False, f"缺终态不应报成功: {result}"

    # 后代被孤儿化，但必须被整组回收
    _assert_descendant_stopped(work, "[root_exits]")

    proc = runner._last_process
    assert proc is not None
    assert proc.poll() is not None, f"根进程未被收割: pid={proc.pid}"

    lock = _get_project_lock(holder["pid"])
    assert lock.acquire(blocking=False), "终态后项目锁未释放"
    lock.release()


def test_超时_后代被强制回收_锁释放(tmp_path):
    """超时路径同样必须回收整棵进程树并释放锁。"""
    from mini_deerflow.middleware import CancellationToken
    from mini_deerflow.runtime import _get_project_lock

    work = tmp_path / "work"
    work.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    fake_root = _make_fake_harness_root(tmp_path)

    runner = HarnessRunner(
        harness_root=str(fake_root),
        python_exe=sys.executable,
        timeout_seconds=1.0,
        max_tool_calls=10,
    )
    runner.set_extra_env({"FAKE_TREE_MODE": "ignore_term", "FAKE_TREE_DIR": str(work)})

    token = CancellationToken()
    holder: dict = {}
    thread = threading.Thread(
        target=lambda: holder.update(
            zip(("result", "pid"), _run_project_execute(
                runner, repo_path=repo, token=token,
            ))
        ),
        daemon=True,
    )
    thread.start()

    assert _wait_for(lambda: (work / "child_ready.txt").exists(), 30.0), (
        "后代进程未在 30s 内启动，夹具无效"
    )
    thread.join(timeout=40)
    assert not thread.is_alive(), "超时后工具未在 40s 内返回"

    result = holder["result"]
    assert result.get("ok") is False, f"超时后应 ok=false: {result}"
    assert result.get("status") == "timeout", (
        f"终态应为 timeout，实际: {result.get('status')}"
    )

    _assert_descendant_stopped(work, "[timeout]")

    proc = runner._last_process
    assert proc is not None
    assert proc.poll() is not None, f"超时后根进程未被收割: pid={proc.pid}"

    lock = _get_project_lock(holder["pid"])
    assert lock.acquire(blocking=False), "超时后项目锁未释放"
    lock.release()
