"""上下文与任务交接：真实任务端到端验收。

验收对象（对应阶段目标）：
1. 主 Agent 能把**用户确认的目标和边界**交给执行 Agent —— 断言执行 Agent
   实际收到的 prompt 里带着 task / context / scope / acceptance。
2. 能把**本轮改动和测试证据**带回同一会话 —— 断言主 Agent 这一轮的工具结果里
   同时含 changes（新增文件的 diff）与 verification（命令 + 退出码），
   并且最终回答引用的是这些证据。

两条链路共用同一套断言，差别只在「执行侧跑在哪」：

- `test_真实任务_...`：跑 **真 harness 子进程**（`D:/项目解读/harnessagent`），
  harness 侧用本地 mock LLM 服务驱动它真写文件、真跑 pytest。
- `test_任务交接_仓内执行器替身_...`：执行侧换成 `_InProcRunner`（仓内替身），
  **不执行任何外部仓库代码**；写文件与 pytest 仍是真实执行。
  它验证的是宿主侧全链路：prompt 组装 → 快照/diff → verification 解析 →
  handover 盘点 → 同一会话拿回证据。

不调用付费模型：主 Agent 用脚本化替身，harness 侧用本地 mock LLM 服务。
"""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

HARNESS_ROOT = "D:/项目解读/harnessagent"

CALC_SOURCE = 'def add(a, b):\n    return a + b\n'
CALC_TEST = (
    "from calc import add\n"
    "\n"
    "\n"
    "def test_add():\n"
    "    assert add(2, 3) == 5\n"
)

# 用户确认后的目标与边界（主 Agent 要原样交出去的东西）
CONFIRMED_TASK = "在工作区新增 calc.py（add 函数）与其测试 test_calc.py，并跑一次 pytest"
CONFIRMED_CONTEXT = "用户已确认：这是一个独立练习目录，不要引入其它文件"
CONFIRMED_SCOPE = "只新增 calc.py 与 test_calc.py 两个文件；不修改其它文件"
CONFIRMED_ACCEPTANCE = "运行 pytest 全部通过（退出码 0）"

# 开发机上真实存在的凭据类环境变量（**只列名字，不含值**）。
# HarnessRunner.build_env() 只在测试注入 .env 时才清 MINIMAX/MIMO 一族，
# 生产路径（env_file=None）是直接继承父进程环境的 —— 所以这里必须自己剥。
REAL_CREDENTIAL_NAMES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "DASHSCOPE_API_KEY",
    "DEEPSEEK_API_KEY",
    "HARNESS_MIMO_API_KEY",
    "HARNESS_MIMO_BASE_URL",
    "MIMO_API_KEY",
    "MIMO_BASE_URL",
    "MIMO_MODEL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "TOKENAPI_KEY",
)

# 真实子进程自报环境用的探针脚本（由 harness 的 write_file 写进工作区再执行）。
# 报告写到 workspace 的**上级目录**，避免污染本轮改动的 diff 证据。
PROBE_ENV_SOURCE = '''\
import json
import os
import socket
import threading
from pathlib import Path


def _try_connect(host, port=80):
    try:
        with socket.create_connection((host, port), timeout=3):
            return "CONNECTED"
    except RuntimeError as exc:
        return "BLOCKED: %s" % exc
    except OSError as exc:
        return "OSERROR: %s" % type(exc).__name__


report = {
    "env_names": sorted(os.environ.keys()),
    "base_url": os.environ.get("MINIMAX_OPENAI_BASE_URL", ""),
    "model": os.environ.get("MINIMAX_MODEL", ""),
    "non_loopback": _try_connect("203.0.113.1"),
    "loopback": "not-tested",
}

srv = socket.socket()
srv.bind(("127.0.0.1", 0))
srv.listen(1)
threading.Thread(target=lambda: srv.accept(), daemon=True).start()
report["loopback"] = _try_connect("127.0.0.1", srv.getsockname()[1])
srv.close()

Path.cwd().parent.joinpath("handover_child_env.json").write_text(
    json.dumps(report, ensure_ascii=False), encoding="utf-8"
)
print("probe done")
'''

# 本用例自带的最小离线守卫（**不依赖 TEST-01 的 tests/_offline_guard**）。
# 写到临时目录后注入子进程的 PYTHONPATH：子进程启动时自动 import sitecustomize，
# 于是"真实 harness 子进程够不到真实端点"由本用例自己证明，不依赖别的分支。
GUARD_SOURCE = '''\
import ipaddress
import os
import socket


def _check_host(host):
    if host is None:
        return
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="replace")
    if host == "localhost":
        return
    try:
        if ipaddress.ip_address(str(host).split("%", 1)[0]).is_loopback:
            return
    except ValueError:
        pass
    raise RuntimeError("TEST_NETWORK_BLOCKED: only loopback network is allowed")


def _check_address(address):
    if isinstance(address, tuple):
        _check_host(address[0])


def install():
    _orig_connect = socket.socket.connect
    _orig_getaddrinfo = socket.getaddrinfo

    def connect(sock, address):
        _check_address(address)
        return _orig_connect(sock, address)

    def getaddrinfo(host, *args, **kwargs):
        _check_host(host)
        return _orig_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = connect
    socket.getaddrinfo = getaddrinfo


if os.environ.get("HANDOVER_TEST_OFFLINE") == "1":
    install()
'''




def _pytest_command() -> str:
    """用当前解释器跑 pytest：路径显式化，避免 PATH 差异导致结果不可复现。

    关掉 pytest 缓存插件 —— 缓存目录会落进工作区，被快照成"新增文件"，
    污染本轮改动的证据。
    """
    exe = sys.executable.replace("\\", "/")
    return f'"{exe}" -m pytest test_calc.py -q -p no:cacheprovider'


# ---------------------------------------------------------------------------
# 脚本化 LLM 服务（harness 子进程侧）：真写文件、真跑命令
# ---------------------------------------------------------------------------


class _ScriptedProvider:
    """本地 mock LLM：按脚本依次返回工具调用，并记录收到的请求体。"""

    def __init__(self, steps: list[dict]) -> None:
        self._steps = list(steps)
        self._requests: list[dict] = []
        self._lock = threading.Lock()
        self._server = None
        self._thread = None
        self.base_url = ""

    # -- 生命周期 ---------------------------------------------------------
    def start(self) -> str:
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, fmt, *args):  # 抑制日志
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                try:
                    req = json.loads(raw.decode("utf-8"))
                except Exception:  # pragma: no cover - 只在 mock 异常时触发
                    req = {"_raw": raw.decode("utf-8", "replace")}
                with outer._lock:
                    outer._requests.append(req)
                    n_tool = len(
                        [m for m in req.get("messages", []) if m.get("role") == "tool"]
                    )
                idx = min(n_tool, len(outer._steps) - 1)
                step = outer._steps[idx]
                if step.get("tool"):
                    message = {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"call_{idx}",
                                "type": "function",
                                "function": {
                                    "name": step["tool"]["name"],
                                    "arguments": json.dumps(
                                        step["tool"]["args"], ensure_ascii=False
                                    ),
                                },
                            }
                        ],
                    }
                    finish_reason = "tool_calls"
                else:
                    message = {"role": "assistant", "content": step.get("content", "")}
                    finish_reason = "stop"
                response = {
                    "id": f"chatcmpl-{idx}",
                    "object": "chat.completion",
                    "created": 1700000000,
                    "model": req.get("model", "mock-model"),
                    "choices": [
                        {"index": 0, "message": message, "finish_reason": finish_reason}
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 10,
                        "total_tokens": 20,
                    },
                }
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(response).encode("utf-8"))

        self._server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        host, port = self._server.server_address
        self.base_url = f"http://{host}:{port}/v1"
        return self.base_url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # -- 观测 -------------------------------------------------------------
    @property
    def requests(self) -> list[dict]:
        with self._lock:
            return list(self._requests)

    def prompts_seen(self) -> str:
        """把 mock 收到的所有 user/system 文本拼起来（= 执行 Agent 看到的上下文）。"""
        chunks: list[str] = []
        for req in self.requests:
            for m in req.get("messages", []):
                content = m.get("content")
                if isinstance(content, str):
                    chunks.append(content)
        return "\n".join(chunks)


# ---------------------------------------------------------------------------
# 仓内执行器替身：只替代 harness 子进程这一层
# ---------------------------------------------------------------------------


class _InProcRunner:
    """仓内执行器替身 —— **不执行任何外部仓库代码**。

    它顶替的只有「跑 harness 子进程」这一步：harness 自己的子进程、协议解析、
    LLM 客户端都不参与。替身做的事都是**真实执行**：

    - 收到的 prompt 原样存下来（这就是执行 Agent 会看到的东西）
    - 真把 calc.py / test_calc.py 写进工作区
    - 真在工作区跑一次 pytest，拿到真退出码
    - 按 harness 协议事件的形状产出 tool_events / files_written

    宿主侧（prompt 组装、快照 diff、verification 解析、handover 盘点）全部是
    生产代码，未被替身绕过。
    """

    is_configured = True

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.tool_events: list[dict] = []
        self.files_written: list[str] = []

    def run(
        self,
        task_id: str,
        repo_path: str,
        prompt: str,
        on_event=None,
        stop_event=None,
        extra_env=None,
    ):
        from mini_deerflow.harness_runner import (
            STATUS_COMPLETED,
            HarnessRunResult,
        )

        self.prompts.append(prompt)
        root = Path(repo_path)

        for seq, (name, content) in enumerate(
            (("calc.py", CALC_SOURCE), ("test_calc.py", CALC_TEST))
        ):
            (root / name).write_text(content, encoding="utf-8")
            self.files_written.append(name)
            self.tool_events.append({
                "seq": seq,
                "name": "write_file",
                "duration_s": 0.0,
                "output": f"Wrote {len(content.encode('utf-8'))} bytes to {name}",
                "output_truncated": False,
            })

        command = _pytest_command()
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "test_calc.py", "-q",
             "-p", "no:cacheprovider"],
            cwd=str(root),
            capture_output=True,
            text=True,
            errors="replace",
        )
        out = (proc.stdout + proc.stderr).strip() or "(no output)"
        self.tool_events.append({
            "seq": len(self.tool_events),
            "name": "bash",
            "duration_s": 0.0,
            "output": f"[exit {proc.returncode}] {out}",
            "output_truncated": False,
            "command_preview": command,
            "command_truncated": False,
        })

        return HarnessRunResult(
            status=STATUS_COMPLETED,
            task_id=task_id,
            final_answer="已创建 calc.py 与 test_calc.py，pytest 通过。",
            tool_events=list(self.tool_events),
            tool_calls=len(self.tool_events),
            exit_code=0,
            files_written=list(self.files_written),
        )


# ---------------------------------------------------------------------------
# 脚本化主 Agent：按交接契约填四个参数，并在汇总时引用工具结果里的证据
# ---------------------------------------------------------------------------


class _ScriptedMainAgent:
    """主 Agent 替身：第一次派发，第二次按契约汇总。"""

    def __init__(self) -> None:
        self.calls = 0
        self.tool_result_raw = ""
        self.tool_result: dict = {}
        self.final_answer = ""

    def decide(self, messages, tools=None):
        from mini_deerflow.models import FinalAnswer, ToolCall

        self.calls += 1
        tool_msgs = [m for m in messages if m.role == "tool"]
        if not tool_msgs:
            return ToolCall(
                name="project_execute",
                arguments={
                    "task": CONFIRMED_TASK,
                    "context": CONFIRMED_CONTEXT,
                    "scope": CONFIRMED_SCOPE,
                    "acceptance": CONFIRMED_ACCEPTANCE,
                },
            )
        # 第二轮：证据必须在**这一轮的上下文里**，汇总只能引用它
        self.tool_result_raw = tool_msgs[-1].content
        self.tool_result = json.loads(self.tool_result_raw)
        files = [
            f["path"]
            for f in self.tool_result.get("changes", {}).get("files", [])
        ]
        exits = [v.get("exit_code") for v in self.tool_result.get("verification", [])]
        self.final_answer = (
            f"本轮改动：{', '.join(files)}；验证退出码：{exits}"
        )
        return FinalAnswer(content=self.final_answer)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def provider():
    p = _ScriptedProvider(
        steps=[
            {"tool": {"name": "write_file",
                      "args": {"path": "calc.py", "content": CALC_SOURCE}}},
            {"tool": {"name": "write_file",
                      "args": {"path": "test_calc.py", "content": CALC_TEST}}},
            {"tool": {"name": "bash",
                      "args": {"command": _pytest_command()}}},
            {"tool": {"name": "write_file",
                      "args": {"path": "probe_env.py", "content": PROBE_ENV_SOURCE}}},
            {"tool": {"name": "bash",
                      "args": {"command": _probe_env_command()}}},
            {"content": "已创建 calc.py 与 test_calc.py，pytest 通过。"},
        ]
    )
    p.start()
    try:
        yield p
    finally:
        p.stop()


def _probe_env_command() -> str:
    """让真实子进程跑探针并自报环境（证据来自子进程本身，不是父进程的推断）。"""
    exe = sys.executable.replace("\\", "/")
    return f'"{exe}" probe_env.py'


@pytest.fixture
def isolated_workspaces_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "workspaces"
    root.mkdir()
    monkeypatch.setenv("MINI_DEERFLOW_WORKSPACES_ROOT", str(root))
    return root


@pytest.fixture
def harness_env(provider, tmp_path, monkeypatch) -> None:
    """把 harness 子进程指向本地 mock LLM；不设置 HARNESS_USE_FAKE_LLM。

    同时做两件隔离的事：

    1. **剥掉开发机的真实凭据** —— HarnessRunner.build_env() 在生产路径
       （env_file=None）下直接继承父进程环境；实测子进程会带进
       DASHSCOPE_API_KEY / DEEPSEEK_API_KEY / TOKENAPI_KEY / MIMO_API_KEY /
       CLAUDE_CODE_MESSAGING_TOKEN。这里先在父进程删掉，子进程就继承不到。
    2. **给子进程装上离线守卫** —— 守卫写到临时目录后注入 PYTHONPATH，
       子进程启动时自动 import sitecustomize，于是"子进程够不到真实端点"
       由本用例自己证明，不依赖别的分支。
    """
    for name in REAL_CREDENTIAL_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MINI_DEERFLOW_HARNESS_ROOT", HARNESS_ROOT)
    monkeypatch.delenv("HARNESS_USE_FAKE_LLM", raising=False)
    monkeypatch.setenv("MINIMAX_API_KEY", "fake-key-for-test")
    monkeypatch.setenv("MINIMAX_OPENAI_BASE_URL", provider.base_url)
    monkeypatch.setenv("MINIMAX_MODEL", "mock-model")

    guard_dir = tmp_path / "offline_guard"
    guard_dir.mkdir()
    (guard_dir / "sitecustomize.py").write_text(GUARD_SOURCE, encoding="utf-8")
    monkeypatch.setenv("HANDOVER_TEST_OFFLINE", "1")
    inherited = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        str(guard_dir) + (os.pathsep + inherited if inherited else ""),
    )


def _make_project(store, project_store, workspaces_root, name):
    """建项目 + 线程，返回 (project, thread)。"""
    from mini_deerflow.api.services.projects import ProjectService

    project_service = ProjectService(
        store=project_store,
        workspaces_root=str(workspaces_root),
        thread_store=store,
    )
    project = project_service.create(name)
    thread = store.create(f"t-{name}", project_id=project.project_id)
    return project, thread


def _assert_handover_roundtrip(model, workspace, prompt_seen: str) -> None:
    """交接验收的公共断言：四要素交到位 + 改动与证据回到同一会话。"""
    # ── 1. 执行 Agent 收到了用户确认的目标与边界 ──────────────────────
    assert CONFIRMED_TASK in prompt_seen, "执行 Agent 没收到目标"
    assert CONFIRMED_CONTEXT in prompt_seen, "执行 Agent 没收到已确认的背景"
    assert CONFIRMED_SCOPE in prompt_seen, "执行 Agent 没收到范围边界"
    assert CONFIRMED_ACCEPTANCE in prompt_seen, "执行 Agent 没收到验收条件"

    # ── 2. 本轮改动回到了同一会话 ─────────────────────────────────────
    result = model.tool_result
    assert result, "主 Agent 第二轮没有拿到工具结果"
    files = {f["path"]: f for f in result.get("changes", {}).get("files", [])}
    assert "calc.py" in files, f"改动里没有 calc.py：{files}"
    assert "test_calc.py" in files, f"改动里没有 test_calc.py：{files}"
    assert files["calc.py"]["status"] == "added"
    assert "def add" in files["calc.py"].get("unified_diff", "")

    # ── 3. 测试证据回到了同一会话（命令 + 退出码）─────────────────────
    verification = result.get("verification", [])
    pytest_runs = [v for v in verification if "pytest" in (v.get("command") or "")]
    assert pytest_runs, f"没有 pytest 的验证记录：{verification}"
    assert pytest_runs[0]["exit_code"] == 0, (
        f"pytest 未通过：{pytest_runs[0].get('output')}"
    )

    # ── 4. 交接要素可见：本次四要素齐全 ──────────────────────────────
    handover = result.get("handover", {})
    assert handover.get("complete") is True, f"交接要素不齐：{handover}"
    assert handover.get("missing") == []

    # ── 4b. 验证判定由代码给出（Guard）──────────────────────────────
    guard = result.get("guard", {})
    assert guard.get("verdict") == "verified", f"验证判定不对：{guard}"
    assert guard.get("label") == "验证通过", guard

    # ── 5. 真实副作用：工作区里确实有这两个文件 ──────────────────────
    assert (workspace / "calc.py").read_text(encoding="utf-8") == CALC_SOURCE
    assert (workspace / "test_calc.py").exists()

    # ── 6. 主 Agent 的汇总引用的是工具结果里的证据 ───────────────────
    assert "calc.py" in model.final_answer
    assert "0" in model.final_answer
    assert any(t.get("action") == "tool_call" for t in model.trace)


# ---------------------------------------------------------------------------
# 验收
# ---------------------------------------------------------------------------


def test_真实任务_目标与边界交给执行Agent_改动与测试证据回到同一会话(
    isolated_workspaces_root, harness_env, provider
):
    """完整链路：真 harness 子进程执行真 harness 代码。"""
    import uuid

    from mini_deerflow.project_store import InMemoryProjectStore
    from mini_deerflow.runtime import RuntimeConfig, create_runtime
    from mini_deerflow.thread_store import InMemoryThreadStore
    from mini_deerflow.tools import create_default_registry

    unique = uuid.uuid4().hex[:8]
    project_store = InMemoryProjectStore()
    store = InMemoryThreadStore()
    registry = create_default_registry()

    runtime = create_runtime(
        store=store,
        project_store=project_store,
        registry=registry,
        config=RuntimeConfig(harness_root=HARNESS_ROOT),
    )
    model = _ScriptedMainAgent()
    runtime.integrated_agent._agent._model = model

    project, thread = _make_project(
        store, project_store, isolated_workspaces_root, f"handover-{unique}"
    )
    workspace = project.workspace_path
    assert workspace is not None and workspace.exists()

    answer, trace = runtime.integrated_agent._agent.run(
        thread.thread_id, f"{CONFIRMED_TASK}\n\n{CONFIRMED_CONTEXT}"
    )
    model.trace = trace

    _assert_handover_roundtrip(model, workspace, provider.prompts_seen())
    assert model.final_answer in answer, "最终回答里没有保留主 Agent 的原始汇总"
    assert "验证结论：验证通过" in answer, "最终回答没有带上代码判定的权威结论"

    # ── 7. 真实子进程自报环境：走本地 mock、不带真实凭据、够不到真实端点 ──
    report_path = workspace.parent / "handover_child_env.json"
    assert report_path.exists(), f"子进程没有产出环境报告：{report_path}"
    report = json.loads(report_path.read_text(encoding="utf-8"))

    # 7a. LLM 端点确实是本地 mock
    assert report["base_url"].startswith("http://127.0.0.1:"), (
        f"子进程指向的不是本地 mock：{report['base_url']}"
    )
    assert report["model"] == "mock-model", report["model"]

    # 7b. 子进程没有继承任何真实凭据变量
    leaked = sorted(set(report["env_names"]) & set(REAL_CREDENTIAL_NAMES))
    assert not leaked, f"子进程继承了真实凭据变量：{leaked}"

    # 7c. 子进程够不到真实端点（离线守卫在子进程里也生效）
    assert report["non_loopback"].startswith("BLOCKED"), (
        f"子进程能连到非回环地址：{report['non_loopback']}"
    )
    assert report["loopback"] == "CONNECTED", report["loopback"]


def test_任务交接_仓内执行器替身_四要素与证据回到同一会话(isolated_workspaces_root):
    """同一套验收断言，执行侧换成仓内替身 —— 不执行任何外部仓库代码。"""
    import uuid

    from mini_deerflow.project_store import InMemoryProjectStore
    from mini_deerflow.project_tools import create_project_execute_tool
    from mini_deerflow.runtime import RuntimeConfig, create_runtime, _get_project_lock
    from mini_deerflow.thread_store import InMemoryThreadStore
    from mini_deerflow.tools import create_default_registry

    unique = uuid.uuid4().hex[:8]
    project_store = InMemoryProjectStore()
    store = InMemoryThreadStore()
    registry = create_default_registry()

    runtime = create_runtime(
        store=store,
        project_store=project_store,
        registry=registry,
        config=RuntimeConfig(harness_root=None),
    )
    # 用仓内替身顶替 harness 执行器（注册的是同一个生产工具，只是换了 runner）
    runner = _InProcRunner()
    registry.register(create_project_execute_tool(
        thread_store=store,
        project_store=project_store,
        runner=runner,
        get_lock=_get_project_lock,
    ))

    model = _ScriptedMainAgent()
    runtime.integrated_agent._agent._model = model

    project, thread = _make_project(
        store, project_store, isolated_workspaces_root, f"handover-inproc-{unique}"
    )
    workspace = project.workspace_path
    assert workspace is not None and workspace.exists()

    answer, trace = runtime.integrated_agent._agent.run(
        thread.thread_id, f"{CONFIRMED_TASK}\n\n{CONFIRMED_CONTEXT}"
    )
    model.trace = trace

    assert len(runner.prompts) == 1, f"执行器被调用了 {len(runner.prompts)} 次"
    _assert_handover_roundtrip(model, workspace, runner.prompts[0])
    assert model.final_answer in answer, "最终回答里没有保留主 Agent 的原始汇总"
    assert "验证结论：验证通过" in answer, "最终回答没有带上代码判定的权威结论"


def test_交接缺要素时_缺口在会话内可见():
    """主 Agent 只填 task 时，缺口必须出现在工具结果里（不能静默）。"""
    from mini_deerflow.project_tools import _build_handover

    handover = _build_handover("", "", "")
    assert handover["complete"] is False
    assert set(handover["missing"]) == {"context", "scope", "acceptance"}
    assert handover["provided"] == {
        "context": False, "scope": False, "acceptance": False,
    }

    full = _build_handover("背景", "范围", "验收")
    assert full["complete"] is True
    assert full["missing"] == []


def test_项目会话提示词写明交接契约():
    """防回归：主 Agent 的行为契约不能被静默删掉。"""
    from mini_deerflow.agent import PROJECT_SESSION_PROMPT

    for marker in ("任务交接契约", "澄清", "task", "context", "scope",
                   "acceptance", "changes", "verification"):
        assert marker in PROJECT_SESSION_PROMPT, f"提示词缺少 {marker}"
