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

隔离策略：
- 每个测试用独立的 _MockServer 实例（scenario/calls 是实例变量，不共享）
- 每个测试用独立的临时 .env 文件（HarnessRunner(..., env_file=...) 注入）
- harness/config.py 已改为懒加载（首次 load_config() 时才读 .env）
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest

# 跨仓库依赖（harnessagent）的位置从环境变量读取；
_DEFAULT_HARNESS_REPO = "D:/项目解读/harnessagent"
HARNESS_REPO_ROOT = Path(os.environ.get("HARNESS_REPO_ROOT", _DEFAULT_HARNESS_REPO))
HARNESS_LOCKIN_SHA = os.environ.get(
    "HARNESS_LOCKIN_SHA", "89fa546ea7a30cbeefb0f6292b928bd2cb01ce26"
)

pytestmark = pytest.mark.skipif(
    not HARNESS_REPO_ROOT.exists(),
    reason=f"harness 仓库不存在: {HARNESS_REPO_ROOT}（设 HARNESS_REPO_ROOT 指向本地副本）",
)


def _assert_harness_baseline_locked() -> None:
    """R3：拒绝透传与正常执行必须基于固定基线 SHA 重跑。"""
    try:
        actual = subprocess.run(
            ["git", "-C", str(HARNESS_REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"无法检查 harness 基线 SHA: {exc}")
    if actual.returncode != 0:
        pytest.skip(f"harness 仓库无 git 上下文: {actual.stderr.strip()}")
    head = actual.stdout.strip()
    expected_prefix = HARNESS_LOCKIN_SHA[:12]
    actual_prefix = head[:12]
    if actual_prefix != expected_prefix:
        pytest.skip(
            f"harness HEAD={actual_prefix} 与锁定基线 {expected_prefix} 不一致；"
            f"checkout 89fa546 或设 HARNESS_LOCKIN_SHA 覆盖"
        )


pytestmark_lock = pytest.fixture(autouse=True)(_assert_harness_baseline_locked)


# ---------------------------------------------------------------------------
# Mock LLM HTTP Server（每个实例独立，不共享类变量）
# ---------------------------------------------------------------------------

class _MockServer:
    """内嵌 HTTP 服务器，持有 mock LLM 逻辑。

    每个实例独立持有 scenario/calls（实例变量），避免多 server 并发时
    场景被共享类变量污染。handler 通过 self.server._mock_server 访问实例。
    """

    def __init__(self) -> None:
        self._srv: object | None = None
        self._thread: threading.Thread | None = None
        self._port: int = 0
        self._scenario: dict = {}
        self._calls: list = []

    def get_calls(self) -> list:
        return list(self._calls)

    def start(self, scenario: dict) -> str:
        """启动服务器，返回 base_url。"""
        import http.server

        self._scenario = scenario
        self._calls = []

        class _SingleUseHTTPServer(http.server.HTTPServer):
            # 不允许复用地址：端口被占用时直接 bind 失败（扫描下一个），
            # 否则 Windows 上多实例可共享同一端口，请求会落到别的实例，
            # 表现为"拿到别的用例的 scenario"这类跨实例污染。
            allow_reuse_address = False

        def _make_handler():
            class H(http.server.BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.0"

                def log_message(self, *args):
                    return

                @property
                def _mock(self) -> "_MockServer":
                    return self.server._mock_server  # type: ignore[attr-defined]

                def do_POST(self):  # noqa: N802
                    body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                    try:
                        req = json.loads(body.decode("utf-8"))
                    except json.JSONDecodeError:
                        self.send_response(400)
                        self.end_headers()
                        return
                    self._mock._calls.append({"body": req})
                    tools = self._mock._scenario.get("tools", [])
                    tool_msgs = [m for m in req.get("messages", []) if m.get("role") == "tool"]
                    if len(tool_msgs) < len(tools):
                        tc = tools[len(tool_msgs)]
                        resp = {
                            "id": "fake-perm",
                            "object": "chat.completion",
                            "model": req.get("model", "fake-model"),
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
                        resp = {
                            "id": "fake-perm-done",
                            "object": "chat.completion",
                            "model": req.get("model", "fake-model"),
                            "choices": [{
                                "index": 0,
                                "message": {"role": "assistant", "content": "任务结束"},
                                "finish_reason": "stop",
                            }],
                            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                        }
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(resp).encode("utf-8"))

            return H

        handler_cls = _make_handler()
        for port in range(19800, 19990):
            try:
                self._srv = _SingleUseHTTPServer(("127.0.0.1", port), handler_cls)
                self._srv._mock_server = self  # type: ignore[attr-defined]
                self._port = port
                break
            except OSError:
                continue
        else:
            pytest.skip("无法找到空闲端口")

        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self._port}"

    def stop(self) -> None:
        if self._srv is not None:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _mk_runner(scenario: dict, *, timeout: float = 60.0):
    """启动 mock LLM server，写入 .env 文件，通过 HarnessRunner(env_file=...) 注入。

    隔离策略：
    - 每个 runner 独立 mock server（实例变量隔离）
    - 每个 runner 独立 .env 文件（HarnessRunner.run 传给子进程，
      harness/config.py 的 load_dotenv(override=True) 加载测试 key + mock URL）
    - 不依赖 monkeypatch，纯参数注入
    """
    from mini_deerflow.harness_runner import HarnessRunner

    # 启动独立 mock server
    server = _MockServer()
    url = server.start(scenario)

    # 写入测试控制的 .env（HarnessRunner.run 通过 HARNESS_ENV_FILE 传给子进程）
    env_dir = Path(tempfile.mkdtemp(prefix="harness-env-"))
    env_file = env_dir / ".env"
    env_file.write_text(
        # load_config() 读 MIMO_API_KEY（HARNESS_MIMO_API_KEY 是误名，实际应为 MIMO_API_KEY）
        # base_url 由 HARNESS_MIMO_BASE_URL 控制
        f"HARNESS_MIMO_BASE_URL={url}\n"
        f"MIMO_API_KEY=fake-key-for-test\n",
        encoding="utf-8",
    )

    runner = HarnessRunner(
        harness_root=str(HARNESS_REPO_ROOT),
        python_exe=sys.executable,
        timeout_seconds=timeout,
        max_tool_calls=50,
        env_file=str(env_file),
    )
    runner._test_env_dir = env_dir  # type: ignore[attr-defined]
    runner._test_server = server  # type: ignore[attr-defined]
    return runner


def _cleanup_runner(runner) -> None:
    srv = getattr(runner, "_test_server", None)
    if srv is not None:
        srv.stop()
    env_dir = getattr(runner, "_test_env_dir", None)
    if env_dir is not None:
        shutil.rmtree(str(env_dir), ignore_errors=True)

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


# ---------------------------------------------------------------------------
# C1：取消 → 子进程真的被回收
# ---------------------------------------------------------------------------

def test_取消_子进程启动后_进程树回收(tmp_path):
    """harness 子进程真的启动后才取消，验证进程树被回收。"""
    from mini_deerflow.middleware import CancellationToken

    sentinel = "cancelled_sentinel.txt"
    bash_cmd = f"sleep 5 && echo done > {sentinel} && sleep 25"

    scenario = {
        "tools": [{
            "name": "bash",
            "arguments": {"command": bash_cmd},
        }],
    }
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = _mk_runner(scenario, timeout=30.0)

    try:
        token = CancellationToken()

        cancel_signal = threading.Event()
        cancel_at = [None]
        seen_tool_start = [False]

        def _canceller():
            deadline = time.monotonic() + 25.0
            while time.monotonic() < deadline:
                proc = getattr(runner, "_last_process", None)
                if proc is not None and proc.stdout is not None:
                    try:
                        line = proc.stdout.readline()
                    except Exception:
                        line = ""
                    if line:
                        line = line.strip()
                        try:
                            ev = json.loads(line)
                        except Exception:
                            ev = {}
                        if ev.get("type") == "tool_start" and ev.get("name") == "bash":
                            seen_tool_start[0] = True
                            token.cancel()
                            cancel_at[0] = time.monotonic()
                            return
                time.sleep(0.05)

        result_holder: dict = {}

        def _invoke():
            try:
                from mini_deerflow.project_store import InMemoryProjectStore
                from mini_deerflow.thread_store import InMemoryThreadStore
                from mini_deerflow.runtime import _get_project_lock
                from mini_deerflow.project_tools import create_project_execute_tool

                unique = uuid.uuid4().hex[:8]
                pid = f"p-{unique}"
                tid = f"t-{unique}"
                store = InMemoryThreadStore()
                project_store = InMemoryProjectStore()
                project_store.create(
                    project_id=pid, name="c1", project_root=repo,
                    workspace_path=repo, legacy_repo_path=None,
                )
                store.create(tid, project_id=pid)
                tool = create_project_execute_tool(
                    thread_store=store, project_store=project_store,
                    runner=runner, get_lock=_get_project_lock,
                )
                ctx = type("Ctx", (), {
                    "thread_id": tid, "run_id": f"r-{unique}",
                    "project_id": pid, "cancellation_token": token,
                })()
                result_holder["raw"] = tool.handler(
                    {"task": "bash sleep", "context": ""}, ctx,
                )
                result_holder["pid"] = pid
            except Exception as exc:
                result_holder["error"] = exc
            finally:
                cancel_signal.set()

        invoke_t = threading.Thread(target=_invoke, daemon=True)
        cancel_t = threading.Thread(target=_canceller, daemon=True)
        invoke_t.start()
        cancel_t.start()

        cancel_signal.wait(timeout=15.0)
        assert "error" not in result_holder, f"tool 异常: {result_holder.get('error')}"
        assert seen_tool_start[0], (
            "未从 harness stdout 读到 bash 的 tool_start 事件——"
            "R4 ready 信号缺失，测试无法验证取消前的进程已存活"
        )
        assert cancel_at[0] is not None, "canceller 未等到 tool_start"

        result = json.loads(result_holder["raw"])
        pid = result_holder["pid"]

        assert result.get("ok") is False, f"取消后应 ok=false: {result}"
        assert result.get("status") == "stopped", (
            f"终态应为 stopped（cancel 触发），实际: {result}"
        )

        elapsed = time.monotonic() - cancel_at[0]
        assert elapsed < 15.0, f"cancel → 终态应在 <15s，实际 {elapsed:.2f}s"

        from mini_deerflow.runtime import _get_project_lock
        lock = _get_project_lock(pid)
        assert lock.acquire(blocking=False), "取消后项目锁未释放"
        lock.release()

        # 有界行为验证：等待超过 sleep 5，sentinel 永不出现
        deadline = time.monotonic() + 7.0
        while time.monotonic() < deadline:
            if (repo / sentinel).exists():
                pytest.fail(
                    f"子进程树未被回收：sleep 5 后 bash 写出了 sentinel，"
                    f"说明 taskkill /T 漏了 bash 子进程"
                )
            time.sleep(0.1)

        verification = result.get("verification", [])
        bash_done = [v for v in verification if v.get("name") == "bash"]
        assert not bash_done, (
            f"bash handler 不该跑完（被取消），但 verification 含: {bash_done}"
        )

        proc = runner._last_process
        assert proc is not None, "HarnessRunner 未记录子进程句柄"
        assert proc.poll() is not None, (
            f"取消后 harness 根进程未被收割: pid={proc.pid}"
        )
    finally:
        _cleanup_runner(runner)


# ---------------------------------------------------------------------------
# C2：权限拒绝跨进程透传
# ---------------------------------------------------------------------------

def test_权限拒绝_跨进程透传到_verification(tmp_path):
    """harness 的三道闸拒绝结果必须回到 project_execute.verification。"""
    scenario = {
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
    }
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = _mk_runner(scenario, timeout=60.0)

    try:
        result, _pid = _run_project_execute(runner, repo_path=repo)

        if result.get("status") == "launch_failed":
            pytest.fail(f"harness 启动失败: {result.get('error')}")

        mock_calls = runner._test_server.get_calls()
        assert mock_calls, (
            f"mock server 未收到任何请求——harness 可能走了真实 API。"
            f" status={result.get('status')}"
            f" files_written={result.get('files_written')}"
        )
        assert len(mock_calls) >= 2, f"mock 调用数不足（期望≥2）: {len(mock_calls)}"

        # 1) 危险命令的副作用没有发生：拒绝在 handler 之前生效
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

        record = denied[0]
        assert "rm -rf /" in str(record.get("command", "")), (
            f"拒绝记录应含被拒命令: {record}"
        )
        assert result.get("status") == "completed", (
            f"任务整体应正常完成（拒绝是单次工具级事件）: {result}"
        )
    finally:
        _cleanup_runner(runner)


# ---------------------------------------------------------------------------
# C3：本轮 diff 保留既有修改（内容级）
# ---------------------------------------------------------------------------

def test_本轮diff_既有修改内容不变_新文件出现在changes(tmp_path):
    """执行前后既有文件哈希必须一致；新文件必须出现在 changes 里。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    existing = repo / "existing.py"
    existing_content = "# existing content that must not change\n"
    existing.write_text(existing_content, encoding="utf-8")
    before_hash = _sha256(existing)

    scenario = {
        "tools": [{
            "name": "write_file",
            "arguments": {"path": "new_mod.py", "content": "print('new')\n"},
        }],
    }
    runner = _mk_runner(scenario, timeout=60.0)

    try:
        result, _pid = _run_project_execute(runner, repo_path=repo)

        if result.get("status") == "launch_failed":
            pytest.fail(f"harness 启动失败: {result.get('error')}")

        mock_calls = runner._test_server.get_calls()
        assert mock_calls, (
            f"mock server 未收到任何请求——harness 可能走了真实 API。"
            f" status={result.get('status')}"
        )

        # 1) 既有文件内容未变（哈希级）
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
    finally:
        _cleanup_runner(runner)
