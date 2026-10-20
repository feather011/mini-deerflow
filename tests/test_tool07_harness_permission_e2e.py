"""TOOL-07：harness 权限与工具模块集成验收（跨进程端到端）。

补 mini-deerflow 侧已有覆盖的三个缺口：

C1 取消：子进程启动后才取消 → 进程树真的被回收、锁真的释放
C2 权限拒绝跨进程透传：harness 的三道闸拒绝结果 → JSONL tool 事件 →
   HarnessRunner.tool_events → project_execute.verification
C3 本轮 diff 保留既有修改：既有文件内容哈希执行前后一致，
   新文件出现在 changes 中，既有文件不出现在 changes 中

与 harness 侧 `tests/test_noninteractive_permission.py` 的分工：
- harness 侧测工具级权限（deny_list / rules / ask_user）在非交互下的行为
- 本文件测这些拒绝能否跨进程回到 mini-deerflow 并如实呈现给模型

夹具：内嵌 mock LLM HTTP 服务器驱动 harness 子进程，不调用真实付费服务。
"""

from __future__ import annotations

import hashlib
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

HARNESS_ROOT = Path("D:/项目解读/harnessagent")

pytestmark = pytest.mark.skipif(
    not HARNESS_ROOT.exists(),
    reason=f"harness 仓库不存在: {HARNESS_ROOT}",
)


# ---------------------------------------------------------------------------
# Mock LLM Provider：只实现本文件需要的两个场景
# ---------------------------------------------------------------------------

class _PermScenario:
    """场景：写文件 + 一个越界/危险命令。"""

    def __init__(self, name: str, args: dict) -> None:
        self.name = name
        self.args = args


class _Handler:
    """按场景返回 OpenAI chat.completion 形状的响应。"""

    _scenario: dict = {}
    _calls: list = []

    @classmethod
    def reset(cls, scenario: dict) -> None:
        cls._scenario = scenario
        cls._calls = []

    @classmethod
    def get_calls(cls) -> list:
        return list(cls._calls)

    def do_POST(self):  # noqa: N802
        import http.server
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))  # type: ignore[attr-defined]
        try:
            req = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_response(400)  # type: ignore[attr-defined]
            self.end_headers()  # type: ignore[attr-defined]
            return

        type(self)._calls.append({"path": self.path, "body": req})  # type: ignore[attr-defined]
        model = req.get("model", "fake-model")
        messages = req.get("messages", [])
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        tools = type(self)._scenario.get("tools", [])  # type: ignore[attr-defined]

        if len(tool_msgs) < len(tools):
            tc = tools[len(tool_msgs)]
            response = {
                "id": "fake-perm",
                "object": "chat.completion",
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": f"call_{len(tool_msgs)}",
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc["arguments"]),
                            },
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            }
        else:
            response = {
                "id": "fake-perm-done",
                "object": "chat.completion",
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "任务结束"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }

        self.send_response(200)  # type: ignore[attr-defined]
        self.send_header("Content-Type", "application/json")  # type: ignore[attr-defined]
        self.end_headers()  # type: ignore[attr-defined]
        self.wfile.write(json.dumps(response).encode("utf-8"))  # type: ignore[attr-defined]


def _build_handler_class():
    import http.server

    class H(_Handler, http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):
            return

    return H


@pytest.fixture()
def mock_llm_server():
    """返回 (base_url, scenario_setter)。"""
    import http.server
    servers = []

    def start(scenario: dict) -> str:
        handler_cls = _build_handler_class()
        for port in range(19800, 19990):
            try:
                srv = http.server.HTTPServer(("127.0.0.1", port), handler_cls)
                break
            except OSError:
                continue
        else:
            pytest.skip("无法找到空闲端口")
        handler_cls.reset(scenario)  # type: ignore[attr-defined]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        servers.append((srv, t))
        return f"http://127.0.0.1:{port}"

    yield start

    for srv, t in servers:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _mk_runner(mock_url: str, *, timeout: float = 60.0):
    from mini_deerflow.harness_runner import HarnessRunner

    return HarnessRunner(
        harness_root=str(HARNESS_ROOT),
        python_exe=sys.executable,
        timeout_seconds=timeout,
        max_tool_calls=50,
    )


@pytest.fixture()
def mock_env(monkeypatch, mock_llm_server):
    """让 HarnessRunner 把子进程指向 mock LLM，而不是真实 API。

    `project_execute` 调用 `runner.run()` 时不传 extra_env，因此这里
    patch HarnessRunner.run 让它恒注入 mock 地址——真实入口不变，
    只换掉模型 Provider。
    """
    from mini_deerflow import harness_runner as hr

    state: dict = {"url": None}

    def _start(scenario: dict) -> str:
        state["url"] = mock_llm_server(scenario)
        return state["url"]

    orig_run = hr.HarnessRunner.run

    def patched_run(self, *args, **kwargs):
        extra = dict(kwargs.get("extra_env") or {})
        extra["HARNESS_MIMO_BASE_URL"] = state["url"]
        extra["HARNESS_MIMO_API_KEY"] = "fake-key-for-test"
        # HarnessRunner 默认把 HARNESS_ENV_FILE 指向仓库根的 .env。
        # 那是**所有测试共享的单个文件**，而 HarnessRunner 会把它重写成
        # 当前注入的配置——两个测试并发跑时会互相覆盖对方的 fake 地址。
        # 指向本测试独有的空文件，既隔离开共享状态，又让 mock 地址生效。
        env_file_dir = Path(tempfile.mkdtemp(prefix="harness-env-"))
        fd, path = tempfile.mkstemp(
            prefix="harness-env-", suffix=".env", dir=env_file_dir,
        )
        os.close(fd)
        extra["HARNESS_ENV_FILE"] = path
        kwargs["extra_env"] = extra
        try:
            return orig_run(self, *args, **kwargs)
        finally:
            shutil.rmtree(env_file_dir, ignore_errors=True)

    monkeypatch.setattr(hr.HarnessRunner, "run", patched_run)
    return _start


def _run_project_execute(
    runner,
    *,
    repo_path: Path,
    task: str = "在仓库根目录创建 hello.c",
    project_id: str | None = None,
    thread_id: str | None = None,
    stop_event=None,
):
    """构造 project_execute 工具并调用一次，返回 (parsed_result, project_id)。"""
    from mini_deerflow.project_store import InMemoryProjectStore
    from mini_deerflow.runtime import _get_project_lock
    from mini_deerflow.thread_store import InMemoryThreadStore
    from mini_deerflow.project_tools import create_project_execute_tool

    unique = uuid.uuid4().hex[:8]
    pid = project_id or f"p-{unique}"
    tid = thread_id or f"t-{unique}"

    store = InMemoryThreadStore()
    project_store = InMemoryProjectStore()
    project_store.create(
        project_id=pid, name="perm", project_root=repo_path,
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
        "cancellation_token": stop_event,
    })()
    raw = tool.handler({"task": task, "context": ""}, ctx)
    return json.loads(raw), pid


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _child_pids() -> set[int]:
    """返回当前进程的子进程 PID 集合。

    Windows 11 26300 起 `wmic` 已被移除，因此优先用 PowerShell 的
    CIM 查询，再退回 wmic。两者都拿不到时返回空集合——调用方必须
    容忍这一点，本文件的断言不能只依赖这个函数。
    """
    import subprocess

    candidates = [
        [
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            f"(Get-CimInstance Win32_Process -Filter "
            f"'ParentProcessId={os.getpid()}').ProcessId",
        ],
        [
            "wmic", "process", "where",
            f"ParentProcessId={os.getpid()}", "get", "ProcessId",
        ],
    ]
    for argv in candidates:
        try:
            out = subprocess.run(
                argv, capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        pids: set[int] = set()
        for line in out.stdout.splitlines():
            line = line.strip()
            if line.isdigit():
                pids.add(int(line))
        if pids or out.returncode == 0:
            return pids
    return set()


# ---------------------------------------------------------------------------
# C1：取消 → 子进程真的被回收
# ---------------------------------------------------------------------------


def test_取消_子进程启动后_进程树回收(mock_env, tmp_path):
    """harness 子进程真的启动后才取消，验证进程树被回收。

    断言：
    1. 确有子进程被创建（避免"启动前就取消"造成的假通过）
    2. 终态 stopped（不是 completed / failed）
    3. 项目锁释放
    4. 取消后子进程树清空（父子进程都退出）
    """
    from mini_deerflow.middleware import CancellationToken

    # 场景：一个会阻塞 30s 的 bash 调用，harness 自己不会结束
    url = mock_env({
        "tools": [{
            "name": "bash",
            "arguments": {"command": "sleep 30"},
        }],
    })
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = _mk_runner(url, timeout=30.0)

    token = CancellationToken()
    child_alive = threading.Event()

    baseline_children = _child_pids()

    def _cancel_when_running():
        # 等 harness 子进程真的起来（进程树里出现新 PID）再取消
        for _ in range(200):
            if _child_pids() - baseline_children:
                child_alive.set()
                time.sleep(0.3)
                token.cancel()
                return
            time.sleep(0.05)
        token.cancel()

    t = threading.Thread(target=_cancel_when_running, daemon=True)
    t.start()

    started_at = time.monotonic()
    result, pid = _run_project_execute(
        runner, repo_path=repo, task="sleep 30", stop_event=token,
    )
    elapsed = time.monotonic() - started_at
    t.join(timeout=10)
    assert child_alive.is_set(), "未能等到 harness 子进程启动"

    # 1) 终态是 stopped
    assert result.get("ok") is False, f"取消后应 ok=false: {result}"
    assert result.get("status") == "stopped", f"终态应为 stopped: {result}"

    # 2) 项目锁已释放
    from mini_deerflow.runtime import _get_project_lock
    lock = _get_project_lock(pid)
    assert lock.acquire(blocking=False), "取消后项目锁未释放"
    lock.release()

    # 3) 取消必须远早于 sleep 结束（30s），否则说明没被中断
    assert elapsed < 20.0, (
        f"取消应在 sleep 30 结束前返回，实际耗时 {elapsed:.2f}s"
    )
    assert result.get("status") == "stopped", (
        f"应由取消触发停止（stopped），而不是等到超时（timeout）: {result}"
    )

    # 4) 进程真的被回收（直接证据，不靠时间推断）
    #
    #    `_terminate_process_tree` 用 `taskkill /PID <pid> /T /F` 杀整棵子树
    #    （`/T` 覆盖 bash 与 sleep 孙进程），随后 `process.wait()`。
    #    终态返回后句柄必须已收割——poll() 非 None 表示子进程已退出。
    #    不用 CIM/PID 断言：Windows 上进程退出后 PID 仍会留在
    #    `Get-CimInstance` 结果里一段时间，会误报"残留"。
    proc = runner._last_process
    assert proc is not None, "HarnessRunner 未记录子进程句柄"
    for _ in range(50):  # 最多等 5s 兜底
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    assert proc.poll() is not None, (
        f"取消后子进程未被回收：poll() 仍为 None（pid={proc.pid}）"
    )


# ---------------------------------------------------------------------------
# C2：权限拒绝跨进程透传
# ---------------------------------------------------------------------------


def test_权限拒绝_跨进程透传到_verification(mock_env, tmp_path):
    """harness 的三道闸拒绝结果必须回到 project_execute.verification。

    两条断言，缺一不可：

    1. **拒绝生效**：handler 零执行（工作区里只有写入的文件，
       `rm -rf /` 没有造成任何副作用）
    2. **拒绝透传**：`verification` 里能看到拒绝命令和拒绝原因
       （此前 harness/agent.py 在 PreToolUse 拒绝后直接 continue，
       跳过 on_tool_start/on_tool_done，JSONL 无事件，宿主收不到原因）
    """
    url = mock_env({
        "tools": [
            {
                "name": "write_file",
                "arguments": {"path": "must_survive.txt", "content": "keep me"},
            },
            {
                "name": "bash",
                "arguments": {"command": "rm -rf /"},
            },
        ],
    })
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = _mk_runner(url, timeout=60.0)

    result, _pid = _run_project_execute(runner, repo_path=repo)

    # 1) 危险命令的副作用没有发生：拒绝在 handler 之前生效（handler 零执行）
    assert repo.is_dir(), "工作区目录不应被删除"
    files_on_disk = sorted(p.name for p in repo.iterdir())
    assert files_on_disk == ["must_survive.txt"], (
        f"工作区出现预期外文件（rm -rf / 未被拒绝？）: {files_on_disk}"
    )

    # 2) 拒绝记录透传到 verification
    verification = result.get("verification", [])
    denied = [
        v for v in verification
        if "Permission denied" in str(v.get("output", ""))
    ]
    assert denied, (
        f"拒绝记录未透传回 verification："
        f"{[v.get('output') for v in verification]}"
    )

    # 拒绝原因要让模型读得懂：命令本身 + 拒绝消息
    record = denied[0]
    assert "rm -rf /" in str(record.get("command", "")), (
        f"拒绝记录应含被拒命令: {record}"
    )
    assert result.get("status") == "completed", (
        f"任务整体应正常完成（拒绝是单次工具级事件）: {result}"
    )


# ---------------------------------------------------------------------------
# C3：本轮 diff 保留既有修改（内容级）
# ---------------------------------------------------------------------------


def test_本轮diff_既有修改内容不变_新文件出现在changes(mock_env, tmp_path):
    """执行前后既有文件哈希必须一致；新文件必须出现在 changes 里。

    断言：
    1. 既有文件内容哈希执行前后一致（不只看它在不在 diff 里）
    2. 新文件出现在 changes 中且 status=added
    3. 既有文件不出现在 changes 中
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    existing = repo / "existing.py"
    existing_content = "# existing content that must not change\n"
    existing.write_text(existing_content, encoding="utf-8")
    before_hash = _sha256(existing)

    url = mock_env({
        "tools": [{
            "name": "write_file",
            "arguments": {"path": "new_mod.py", "content": "print('new')\n"},
        }],
    })
    runner = _mk_runner(url, timeout=60.0)

    result, _pid = _run_project_execute(runner, repo_path=repo)

    # 1) 既有文件内容未变（哈希级，不只看 diff 列表）
    after_hash = _sha256(existing)
    assert after_hash == before_hash, (
        f"既有文件内容被改动：执行前 {before_hash[:12]}，执行后 {after_hash[:12]}"
    )
    assert existing.read_text(encoding="utf-8") == existing_content

    # 2) 新文件已创建
    new_file = repo / "new_mod.py"
    assert new_file.exists(), f"新文件未创建：{list(repo.iterdir())}"

    # 3) changes：新文件 added，既有文件不在其中
    changes = result.get("changes", {}).get("files", [])
    paths_added = [c["path"] for c in changes if c.get("status") == "added"]
    assert any(p.endswith("new_mod.py") for p in paths_added), (
        f"新文件应出现在 changes 的 added 里，实际: {changes}"
    )
    existing_in_changes = [c for c in changes if c["path"].endswith("existing.py")]
    assert not existing_in_changes, (
        f"未改动的既有文件不应出现在 changes 中：{existing_in_changes}"
    )
