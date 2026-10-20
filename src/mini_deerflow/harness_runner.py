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


def _terminate_process_tree(process: subprocess.Popen) -> None:
    """杀整棵进程树（镜像 sandbox_command 的方案：Windows taskkill /T /F）。"""
    if process.poll() is not None:
        return
    pid = process.pid
    try:
        if sys.platform == "win32":
            system_root = os.environ.get("SystemRoot")
            taskkill = Path(system_root) / "System32" / "taskkill.exe" if system_root else None
            if taskkill and taskkill.exists():
                subprocess.run(
                    [str(taskkill), "/PID", str(pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    timeout=5,
                )
                return
            process.kill()
        else:
            import signal

            os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:  # noqa: BLE001 - 尽力而为，失败走最终 fallback
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=3)
        except Exception:  # noqa: BLE001
            pass


class HarnessRunner:
    """驱动一次 harness 非交互执行；同一实例可顺序复用。"""

    def __init__(
        self,
        harness_root: str | None,
        python_exe: str | None = None,
        timeout_seconds: float = 600.0,
        max_tool_calls: int = 50,
    ) -> None:
        self._harness_root = Path(harness_root).resolve() if harness_root else None
        self._python_exe = python_exe or sys.executable
        self._timeout_seconds = timeout_seconds
        self._max_tool_calls = max_tool_calls
        # 最近一次 run() 启动的子进程句柄；终态后 poll() 应非 None，
        # 供调用方（及验收测试）核查进程树确实被回收。
        self._last_process: subprocess.Popen | None = None

    @property
    def is_configured(self) -> bool:
        """harness 根目录未配置时为 False，派发工具据此如实拒绝。"""
        return (
            self._harness_root is not None
            and (self._harness_root / "harness" / "noninteractive.py").exists()
        )

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

        env = dict(os.environ)
        # 清除 MCP 代理配置：harness 必须直连真实 API，不能走 127.0.0.1:15721
        env.pop("ANTHROPIC_BASE_URL", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        env["PYTHONPATH"] = str(self._harness_root) + os.pathsep + env.get("PYTHONPATH", "")
        env["HARNESS_NONINTERACTIVE"] = "1"
        # 让 harness 加载 mini-deerflow 的 .env（包含 MINI_MAX 配置）
        env["HARNESS_ENV_FILE"] = str(Path(__file__).parent.parent.parent / ".env")
        if extra_env:
            env.update(extra_env)

        prompt_file: str | None = None
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
                process = subprocess.Popen(
                    argv,
                    cwd=str(repo_path),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
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

            return self._supervise(
                process, task_id, on_event=on_event, stop_event=stop_event
            )
        finally:
            if prompt_file:
                try:
                    os.unlink(prompt_file)
                except OSError:
                    pass

    def _supervise(
        self,
        process: subprocess.Popen,
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
                _terminate_process_tree(process)
                break
            try:
                exit_code = process.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    timed_out = True
                    _terminate_process_tree(process)
                    break

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
