"""HarnessRunner：以子进程驱动 harness 非交互入口（WM-3C）。

进程模型（设计文档第 3 节的既定选择）：

- **子进程 + 参数数组**（`shell=False`），`cwd` = 项目绑定仓库，
  `PYTHONPATH` 前置 harness 根目录，Python 解释器显式可配。
  prompt 经临时 UTF-8 文件传递（`--prompt-file`），不进命令行。
- stdout 是 JSONL 协议流（每行一个事件）；stderr 是执行器日志，
  只保留尾部用于诊断。
- **只有拿到 `done` 事件且退出码为 0 才算 completed**：缺终态、
  协议污染（stdout 出现非 JSON 行）、异常退出一律不显示成功。

终态分类：completed / failed / stopped / timeout / launch_failed /
protocol_error——所有终态都在 `run()` 返回前完成进程树回收。

占用（ProjectRunLock）：仓库级互斥，防止同一项目并发写；
持有者身份校验，完成 / 失败 / 取消 / 超时 / 启动失败都释放。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# --- 协议事件 → 既有 RunEvent 类型的映射（尽量复用，不新增类型） ---

RUN_EVENT_TYPE_MAP: dict[str, str] = {
    "started": "run.started",
    "tool_start": "tool.started",
    "tool_done": "tool.completed",
    "assistant": "message.completed",
    "done": "run.completed",
    "error": "run.failed",
}
"""harness JSONL 类型 → Mini DeerFlow RunEvent 类型。

前端事件面板按既有类型穷举渲染；此处只做映射不做新增，
新增类型要改 8 处（见设计文档第 4 节）——本轮不需要。
"""

# stderr 诊断尾部与工具事件在结果里的保留上限
STDERR_TAIL_BYTES = 16 * 1024
TOOL_EVENT_OUTPUT_CHARS = 2000
TOOL_EVENTS_IN_RESULT = 30

# 进程树回收参数
# 宽限期：先发终止信号，等这么久还没退出就强制恢复
TERMINATE_GRACE_SECONDS = 3.0
# 强制恢复后的等待上限
TERMINATE_KILL_SECONDS = 3.0

STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_STOPPED = "stopped"
STATUS_TIMEOUT = "timeout"
STATUS_LAUNCH_FAILED = "launch_failed"
STATUS_PROTOCOL_ERROR = "protocol_error"

_TERMINAL_EVENT_TYPES = frozenset({"done", "error"})


class HarnessProtocolError(Exception):
    """JSONL 协议行无法解析或字段非法。"""


@dataclass
class HarnessRunResult:
    """一次执行的收口结果（无论成败都完整返回）。"""

    status: str
    task_id: str
    final_answer: str = ""
    tool_events: list[dict] = field(default_factory=list)
    tool_calls: int = 0
    exit_code: int | None = None
    timed_out: bool = False
    error: str = ""
    stderr_tail: str = ""
    unmapped_lines: int = 0
    files_written: list[str] = field(default_factory=list)
    """工具报告的写入路径（write_file / edit_file），非完整变更证据。"""


def _parse_protocol_line(line: str) -> dict:
    """校验一行协议：必须是 JSON 对象且带 type 字段。"""
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise HarnessProtocolError(f"非 JSON 行: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
        raise HarnessProtocolError("协议行缺少字符串 type 字段")
    return payload


def _posix_group_alive(pgid: int) -> bool:
    """进程组是否还有成员（含已忽略 SIGTERM 的后代）。

    组内成员全部退出后进程组才被回收；组长的僵尸态仍算成员，
    因此调用方需要先 reap 组长再判断。
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _posix_pgid(process: subprocess.Popen) -> int | None:
    """取子进程所在进程组 id；不可安全定位时返回 None。

    `start_new_session=True` → 子进程是会话/进程组组长，pgid == pid。
    根进程已退出时 `getpgid()` 会失败，此时仍按 pgid == pid 处理：
    进程组在成员全部退出前不会被回收，pgid 也不会被复用。

    返回 None 表示"不能安全地整组操作"（子进程与宿主同组），
    此时绝不能 killpg——否则会把宿主进程一起杀掉。
    """
    try:
        pgid = os.getpgid(process.pid)
    except (ProcessLookupError, OSError):
        return process.pid
    try:
        if pgid == os.getpgid(0):
            return None
    except OSError:
        return None
    return pgid


def _terminate_posix_group(process: subprocess.Popen) -> None:
    """POSIX：整组终止 → 宽限等待 → 强制恢复。

    根进程退出 **不等于** 进程组退出：后代可能忽略 SIGTERM 而继续运行。
    因此宽限期结束后必须复查整组是否真的消失，必要时用 SIGKILL 强制恢复。
    """
    import signal

    pgid = _posix_pgid(process)
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            pass
    # 宽限期：等根进程退出（同时把它 reap 掉，否则僵尸仍算组成员）
    try:
        process.wait(timeout=TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    if pgid is None:
        return
    # 整组复查：仍存活的成员（含忽略 SIGTERM 的后代）强制回收
    if _posix_group_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
        try:
            process.wait(timeout=TERMINATE_KILL_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        deadline = time.monotonic() + TERMINATE_KILL_SECONDS
        while time.monotonic() < deadline and _posix_group_alive(pgid):
            time.sleep(0.05)


def _taskkill_tree(pid: int) -> None:
    """Windows 兜底：taskkill /T /F 递归终止进程树。"""
    system_root = os.environ.get("SystemRoot")
    taskkill = Path(system_root) / "System32" / "taskkill.exe" if system_root else None
    if not (taskkill and taskkill.exists()):
        return
    try:
        subprocess.run(
            [str(taskkill), "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            timeout=15,
        )
    except Exception:  # noqa: BLE001 - 尽力而为
        pass


def _create_kill_on_close_job(process: subprocess.Popen) -> int | None:
    """Windows：把子进程放进 Job Object（进程组的等价物）。

    Job 内的后代自动继承成员身份；关闭 Job 句柄（KILL_ON_JOB_CLOSE）
    即可一次性杀掉整棵树——包括根进程已退出、后代被孤儿化的情况
    （taskkill /T 依赖 PPID 链，根进程一死就找不到后代了）。

    创建/绑定失败返回 None，调用方退回 taskkill 路径。
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        class _BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IOCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class _ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimit),
                ("IoInfo", _IOCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _ExtendedLimit()
        # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        info.BasicLimitInformation.LimitFlags = 0x2000
        # JobObjectExtendedLimitInformation
        if not kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(info), ctypes.sizeof(info)
        ):
            kernel32.CloseHandle(job)
            return None
        if not kernel32.AssignProcessToJobObject(
            job, wintypes.HANDLE(int(process._handle))
        ):
            kernel32.CloseHandle(job)
            return None
        return int(job)
    except Exception:  # noqa: BLE001 - 拿不到 Job 就退回 taskkill
        return None


def _close_kill_job(job: int | None) -> None:
    """关闭 Job 句柄：KILL_ON_JOB_CLOSE 会杀掉 Job 内所有进程。"""
    if job is None:
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle(wintypes.HANDLE(int(job)))
    except Exception:  # noqa: BLE001
        pass


def _terminate_process_tree(
    process: subprocess.Popen, job: int | None = None
) -> None:
    """回收整棵进程树：宽限终止 → 有界等待 → 强制恢复。

    关键点：**根进程退出不等于整组退出**。后代可能忽略 SIGTERM，
    根进程先死时 taskkill / killpg 也定位不到它们。因此：
      1) 先对整组发终止信号；
      2) 有界宽限等待；
      3) 整组仍存活 → 强制恢复（Windows 关闭 Job / POSIX SIGKILL）；
      4) 根进程一定要被 reap。
    """
    if job is not None:
        # Windows Job：一次关闭即杀掉组内所有进程（含已孤儿化的后代）
        _close_kill_job(job)
    elif sys.platform == "win32":
        _taskkill_tree(process.pid)
    else:
        _terminate_posix_group(process)
    # 兜底：根进程必须被回收（否则留下僵尸，且调用方拿不到退出码）
    try:
        process.wait(timeout=TERMINATE_KILL_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=TERMINATE_KILL_SECONDS)
        except Exception:  # noqa: BLE001
            pass


class _RunState:
    """**单次 run** 的执行上下文：进程句柄 / Job 句柄 / 回收标记。

    为什么不能放在 Runner 实例上：生产装配只建**一个** HarnessRunner
    （`runtime.py`），而项目锁只互斥**同一个 project_id** —— 不同项目
    的任务会并发跑在同一个 Runner 上。实例级状态会让 A 的收口把
    "已回收"标记和 Job 句柄留给 B：B 的取消要么被跳过（进程树残留、
    继续写工作区、继续占锁），要么取到 A 的句柄（跨运行误杀）。

    因此这三样东西必须按**每一次 run** 归属。
    """

    __slots__ = ("process", "job", "terminated")

    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self.job: int | None = None
        self.terminated = False


class HarnessRunner:
    """驱动一次 harness 非交互执行；同一实例可顺序复用。

    **并发**：同一实例可被多个项目并发使用（生产装配只建一个 Runner），
    因此每次 run 的进程句柄 / Job 句柄 / 回收标记都放在 `_RunState` 里，
    不放在实例属性上。
    """

    def __init__(
        self,
        harness_root: str | None,
        python_exe: str | None = None,
        timeout_seconds: float = 600.0,
        max_tool_calls: int = 50,
        env_file: str | None = None,
    ) -> None:
        self._harness_root = Path(harness_root).resolve() if harness_root else None
        self._python_exe = python_exe or sys.executable
        self._timeout_seconds = timeout_seconds
        self._max_tool_calls = max_tool_calls
        # 测试隔离：优先用测试传入的 .env 文件路径
        self._env_file = env_file
        # 测试注入：直接注入环境变量到子进程（无需 .env 文件）
        self._extra_env: dict[str, str] = {}
        # 最近一次 run() 启动的子进程句柄；终态后 poll() 应非 None，
        # 供调用方（及验收测试）核查进程树确实被回收。
        # **仅供诊断**：并发时它可能指向另一轮 run，真正用于回收的句柄
        # 存在每次 run 自己的 `_RunState` 里。
        self._last_process: subprocess.Popen | None = None

    @property
    def is_configured(self) -> bool:
        """harness 根目录未配置时为 False，派发工具据此如实拒绝。"""
        return (
            self._harness_root is not None
            and (self._harness_root / "harness" / "noninteractive.py").exists()
        )

    def set_extra_env(self, env: dict[str, str]) -> None:
        """设置注入子进程的环境变量（覆盖同名变量）。

        测试隔离用：在调用 run() 前设置 mock LLM URL，
        之后每次 run() 都会注入这些变量，无需 .env 文件。
        """
        self._extra_env = dict(env)

    def build_env(self, extra_env: dict[str, str] | None = None) -> dict[str, str]:
        """构造子进程环境（run() 用同一份逻辑）。

        单独暴露是为了让调用方/验收能**核查隔离是否真的生效**：
        测试场景下父进程的真实 API 凭证必须不在子进程环境里。
        """
        env = dict(os.environ)
        # 清除 MCP 代理配置：harness 必须直连真实 API，不能走 127.0.0.1:15721
        env.pop("ANTHROPIC_BASE_URL", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        env["PYTHONPATH"] = str(self._harness_root) + os.pathsep + env.get("PYTHONPATH", "")
        env["HARNESS_NONINTERACTIVE"] = "1"
        # 测试隔离：仅当测试注入了 .env 文件时才设置 HARNESS_ENV_FILE。
        # 此时也要清除真实 API 凭证，防止父进程把 key 传给子进程。
        # 生产场景（self._env_file=None）不设置此变量，harness 走默认行为。
        if self._env_file:
            # 清除真实 API 凭证——测试子进程只能从测试 .env 文件读测试 key
            env.pop("MINIMAX_API_KEY", None)
            env.pop("MINIMAX_OPENAI_BASE_URL", None)
            env.pop("MINIMAX_MODEL", None)
            env.pop("MIMO_API_KEY", None)
            env.pop("HARNESS_MIMO_BASE_URL", None)
            env.pop("HARNESS_MIMO_API_KEY", None)
            env["HARNESS_ENV_FILE"] = self._env_file
        # 测试注入：set_extra_env() 设置的环境变量直接覆盖同名变量
        env.update(self._extra_env)
        if extra_env:
            env.update(extra_env)
        return env

    def run(
        self,
        task_id: str,
        repo_path: str,
        prompt: str,
        on_event=None,
        stop_event: threading.Event | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> HarnessRunResult:
        """在 `repo_path` 中执行一次任务，阻塞到终态。

        Args:
            on_event: 每解析出一条协议事件即回调（dict，已含映射前的
                原始 type；供上层转发到事件流，WM-4 接 SSE）。
            stop_event: 外部置位即杀进程树并以 stopped 收口。
            extra_env: 附加环境变量（测试注入假 Provider 地址等）。
        """
        if self._harness_root is None:
            return HarnessRunResult(
                status=STATUS_LAUNCH_FAILED,
                task_id=task_id,
                error="执行器未配置（缺少 harness 根目录）",
            )

        env = self.build_env(extra_env)

        prompt_file: str | None = None
        # 本次 run 独占的执行上下文（并发安全的关键）
        state = _RunState()
        try:
            fd, prompt_file = tempfile.mkstemp(suffix=".txt", prefix="harness-prompt-")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(prompt)

            argv = [
                self._python_exe,
                "-m",
                "harness.noninteractive",
                "--task-id", task_id,
                "--prompt-file", prompt_file,
                "--max-tool-calls", str(self._max_tool_calls),
            ]

            try:
                # R1：进程组隔离（让 taskkill /T /F / killpg 精确定向回收）
                #   - POSIX: start_new_session=True → 新会话组长
                #   - Windows: CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_NO_WINDOW
                # DETACHED_PROCESS 让子进程脱离父 console，CREATE_NO_WINDOW
                # 抑制启动时一闪而过的命令行窗口。
                creationflags = 0
                if sys.platform == "win32":
                    creationflags = (
                        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                        | getattr(subprocess, "DETACHED_PROCESS", 0)
                        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    )
                process = subprocess.Popen(
                    argv,
                    cwd=str(repo_path),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    start_new_session=True,
                    creationflags=creationflags or 0,
                    # bufsize=1 + 文本模式：按行读协议流
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
            except OSError as exc:
                return HarnessRunResult(
                    status=STATUS_LAUNCH_FAILED,
                    task_id=task_id,
                    error=f"启动失败: {exc}",
                )

            # 留下进程句柄供调用方核查回收结果：终态后 process.poll()
            # 必须非 None，证明子进程真的退出（而不是仅仅"报告了终态"）。
            self._last_process = process
            state.process = process

            # R1：Windows 上把子进程放进 Job Object —— 进程组的等价物。
            # 后代自动继承成员身份，关闭 Job 即整棵树消失（含根进程先死、
            # 后代被孤儿化的情况：taskkill /T 依赖 PPID 链，根进程一死
            # 就定位不到后代了）。创建失败时退回 taskkill /T /F 路径。
            # 句柄存在**本次 run 自己的 state** 里：并发时不与别的运行串。
            state.job = _create_kill_on_close_job(process)

            return self._supervise(
                process, state, task_id, on_event=on_event, stop_event=stop_event
            )
        finally:
            # 兜底：若 _supervise 提前抛出，本次 run 的 Job 里的后代也不能留下
            self._close_job(state)
            if prompt_file:
                try:
                    os.unlink(prompt_file)
                except OSError:
                    pass

    def _close_job(self, state: _RunState) -> None:
        """关闭**本次 run** 的 Job 句柄：残留后代（孤儿）随之被杀。"""
        job = state.job
        state.job = None
        if job is not None:
            _close_kill_job(job)

    def _terminate_tree(self, state: _RunState) -> None:
        """回收**本次 run** 的整棵进程树（幂等，仅对本次 run 生效）。

        根进程退出 **不代表** 整组退出：后代可能忽略 SIGTERM 而继续运行，
        根进程先死时也定位不到后代。因此必须在根进程退出后复查整组。

        回收标记与 Job 句柄都取自本次 run 的 `state`，**不使用实例状态**：
        否则并发时 A 的收口会把 B 的回收跳过（进程树残留）或让 B 取到
        A 的 Job 句柄（跨运行误杀）。
        """
        if state.terminated:
            return
        state.terminated = True
        job = state.job
        state.job = None
        process = state.process
        if process is not None:
            _terminate_process_tree(process, job=job)

    def _supervise(
        self,
        process: subprocess.Popen,
        run_state: _RunState,
        task_id: str,
        *,
        on_event=None,
        stop_event: threading.Event | None,
    ) -> HarnessRunResult:
        """读协议流、等终态、按需杀树并分类结果。"""
        state: dict[str, object] = {
            "done": False,
            "error_reason": "",
            "final_answer": "",
            "terminal_seen": False,
        }
        tool_events: list[dict] = []
        files_written: list[str] = []
        unmapped = {"count": 0}
        stderr_buf: list[str] = []
        stderr_bytes = {"n": 0}

        def read_stdout() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = _parse_protocol_line(line)
                except HarnessProtocolError:
                    unmapped["count"] += 1
                    continue
                event_type = payload["type"]
                if event_type in _TERMINAL_EVENT_TYPES:
                    state["terminal_seen"] = True
                if event_type == "done":
                    state["done"] = True
                    state["tool_calls"] = payload.get("tool_calls", 0)
                elif event_type == "error":
                    state["error_reason"] = str(payload.get("reason", ""))
                    state["tool_calls"] = payload.get("tool_calls", 0)
                elif event_type == "assistant":
                    state["final_answer"] = str(payload.get("content", ""))
                elif event_type in ("tool_start", "tool_done"):
                    if event_type == "tool_done":
                        output = str(payload.get("output", ""))
                        event = {
                            "seq": payload.get("seq"),
                            "name": payload.get("name"),
                            "duration_s": payload.get("duration_s"),
                            "output": output[:TOOL_EVENT_OUTPUT_CHARS],
                            "output_truncated": payload.get("output_truncated", False)
                            or len(output) > TOOL_EVENT_OUTPUT_CHARS,
                        }
                        # 对于 bash，捕获命令预览（500 字符截断）+ 截断标记
                        if payload.get("name") == "bash":
                            event["command_preview"] = payload.get("command_preview", "")
                            event["command_truncated"] = payload.get("command_truncated", False)
                        tool_events.append(event)
                        # 从 write_file / edit_file 输出中提取文件路径（工具报告，非完整变更证据）
                        tool_name = payload.get("name", "")
                        if tool_name in ("write_file", "edit_file") and not output.startswith("Error:"):
                            # write_file: "Wrote N bytes to {path}"
                            # edit_file: "Edited {path}: replaced first occurrence"
                            parts = output.split(" to ", 1)
                            if len(parts) < 2:
                                parts = output.split("Edited ", 1)
                                if len(parts) > 1:
                                    path_part = parts[1].split(":")[0].strip()
                                else:
                                    path_part = None
                            else:
                                path_part = parts[1].strip()
                            if path_part:
                                files_written.append(path_part)
                if on_event is not None:
                    try:
                        on_event(payload)
                    except Exception:  # noqa: BLE001 - 回调异常不能拖垮协议读取
                        pass

        def read_stderr() -> None:
            assert process.stderr is not None
            for line in process.stderr:
                stderr_bytes["n"] += len(line.encode("utf-8", errors="replace"))
                stderr_buf.append(line)
                # 只保留尾部
                while stderr_bytes["n"] > STDERR_TAIL_BYTES and stderr_buf:
                    removed = stderr_buf.pop(0)
                    stderr_bytes["n"] -= len(removed.encode("utf-8", errors="replace"))

        stdout_thread = threading.Thread(target=read_stdout, daemon=True)
        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        timed_out = False
        stopped = False
        deadline = time.monotonic() + self._timeout_seconds
        exit_code: int | None = None
        while True:
            if stop_event is not None and stop_event.is_set():
                stopped = True
                self._terminate_tree(run_state)
                break
            try:
                exit_code = process.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    timed_out = True
                    self._terminate_tree(run_state)
                    break

        # 根进程退出 ≠ 整组退出：可能仍有孤儿后代存活（还会占着 stdout 管道）。
        # 统一再回收一次整组，确保不残留子进程、管道也能正常关闭。
        self._terminate_tree(run_state)

        if exit_code is None:
            try:
                exit_code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                exit_code = None

        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)

        base = HarnessRunResult(
            status="",
            task_id=task_id,
            final_answer=str(state.get("final_answer", "")),
            tool_events=tool_events[-TOOL_EVENTS_IN_RESULT:],
            tool_calls=int(state.get("tool_calls", 0) or 0),
            exit_code=exit_code,
            timed_out=timed_out,
            error=str(state.get("error_reason", "")),
            stderr_tail="".join(stderr_buf),
            unmapped_lines=unmapped["count"],
            files_written=list(dict.fromkeys(files_written)),  # 去重，保持顺序
        )

        # 分类优先级：外部停止 > 超时 > 协议/终态判定
        if stopped:
            base.status = STATUS_STOPPED
            base.error = base.error or "收到停止请求，已终止执行进程"
            return base
        if timed_out:
            base.status = STATUS_TIMEOUT
            base.error = base.error or f"超过宿主超时（{self._timeout_seconds}s），已终止进程树"
            return base
        if unmapped["count"] > 0:
            # stdout 出现过非协议行：即使有 done 也不能显示成功
            base.status = STATUS_PROTOCOL_ERROR
            base.error = f"协议错误：{unmapped['count']} 行非 JSON 协议输出"
            return base
        if state["done"] and exit_code == 0:
            base.status = STATUS_COMPLETED
            return base
        if state["done"] and exit_code != 0:
            base.status = STATUS_FAILED
            base.error = base.error or f"收到 done 但进程退出码为 {exit_code}"
            return base
        if exit_code not in (0, None) and not state["terminal_seen"]:
            base.status = STATUS_FAILED
            base.error = base.error or f"进程异常退出（exit={exit_code}），无有效终态"
            return base
        # exit 0 但没有终态事件，或其它未覆盖组合：一律不显示成功
        base.status = STATUS_PROTOCOL_ERROR
        base.error = base.error or f"缺少有效终态事件（exit={exit_code}）"
        return base


class ProjectRunLock:
    """仓库（项目）级互斥：同一项目同时只允许一个执行任务。

    占用按 project_id 记录、按持有者身份释放：完成 / 失败 / 取消 /
    超时 / 启动失败都走调用方的 finally → release()。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._holders: dict[str, str] = {}

    @staticmethod
    def new_owner() -> str:
        return uuid.uuid4().hex

    def try_acquire(self, project_id: str, owner: str) -> bool:
        with self._lock:
            if project_id in self._holders:
                return False
            self._holders[project_id] = owner
            return True

    def release(self, project_id: str, owner: str) -> bool:
        with self._lock:
            if self._holders.get(project_id) == owner:
                del self._holders[project_id]
                return True
            return False

    def is_held(self, project_id: str) -> bool:
        with self._lock:
            return project_id in self._holders
