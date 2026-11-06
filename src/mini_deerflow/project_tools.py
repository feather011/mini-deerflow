"""WM-3B：项目只读检索工具（discussion 分支）。

三个工具：`project_read_file` / `project_glob` / `project_grep`。
主 Agent 用它们在绑定仓库内**只读**理解代码；写入与命令执行属执行器
（后续 WM-3C），本模块刻意不提供任何写能力。

四个设计约束：

1. **工作区根目录不进参数 schema。** thread_id → project_id → repo_path
   全部从落库事实反查（`ExecutionContext.thread_id` 是唯一入口），
   模型只能给仓库内相对路径——否则一句 `{"root": "/"}` 就能读全盘。
2. **路径边界与 `api.services.projects.PathGate` 同规则**（NUL / 空 /
   `..` / 绝对路径 / UNC 拒绝 → `resolve()` 消解符号链接 → normcase
   判回仓库内）。不跨层 import api 包，规则在此同构实现；符号链接
   指向仓库外时 `resolve()` 已把它带到真实位置，边界检查自然拦下。
3. **所有输出有上限**：读取字节、行数、glob 条数、grep 命中数与
   单文件扫描字节都封顶，超限只截断并如实标注 truncated。
4. **纯 Python 检索，无 shell、无子进程**：grep 用 `re` + `os.walk`，
   参数永远不进命令行拼接。

工具结果统一为 JSON（复用 context_shared 的 ok/err 约定）：
参数类错误 `kind="usage_error"`（模型可改参重试），
会话/项目状态类错误 `kind="error"`。
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path

from mini_deerflow.context_shared import context_attr, err_response, ok_response
from mini_deerflow.middleware import CancellationToken
from mini_deerflow.project_store import (
    ProjectNotFoundError as StoreProjectNotFoundError,
    ProjectStore,
)
from mini_deerflow.thread_store import ThreadNotFoundError, ThreadStore
from mini_deerflow.tools import Tool
from mini_deerflow.verification_guard import (
    evaluate_verification,
    execution_failed_verdict,
)

__all__ = [
    "PROJECT_READ_FILE_TOOL_NAME",
    "PROJECT_GLOB_TOOL_NAME",
    "PROJECT_GREP_TOOL_NAME",
    "PROJECT_EXECUTE_TOOL_NAME",
    "PROJECT_TOOL_CODES",
    "create_project_read_tools",
    "create_project_execute_tool",
]

PROJECT_EXECUTE_TOOL_NAME = "project_execute"

PROJECT_READ_FILE_TOOL_NAME = "project_read_file"
PROJECT_GLOB_TOOL_NAME = "project_glob"
PROJECT_GREP_TOOL_NAME = "project_grep"


# --- 项目工具的稳定错误码 ---
#
# 这些码进入**工具结果**（ok:false JSON 的 `code` 字段），也就是模型
# 真正读到的那份错误。模型可以按码判断「改参重试」还是「如实转告用户」，
# 不必解析文案。
#
# 为什么码在结果里而不在 SSE 事件里：项目工具的失败被 `_as_tool_result`
# 转成 ok:false JSON，**异常不逃出 handler** —— 业务失败是**可恢复**的
# （模型改参即可重试），不该让整轮 Run 失败，因此也**不会**产生
# `tool.failed` 事件。稳定码的消费者是模型，不是事件流。
PROJECT_TOOL_CODES: dict[str, str] = {
    "usage": "PROJECT_TOOL_USAGE_ERROR",
    "state": "PROJECT_TOOL_STATE_ERROR",
    "file_unreadable": "PROJECT_TOOL_FILE_UNREADABLE",
    "internal": "PROJECT_TOOL_INTERNAL_ERROR",
    "launch_failed": "PROJECT_TOOL_LAUNCH_FAILED",
    "protocol_error": "PROJECT_TOOL_PROTOCOL_ERROR",
    "execute_stopped": "PROJECT_TOOL_STOPPED",
    "execute_timeout": "PROJECT_TOOL_TIMEOUT",
    "execute_failed": "PROJECT_TOOL_EXECUTE_FAILED",
}

# 错误文案的对外长度上限：文案会回灌给模型，超长既费 token 又撑爆展示
_ERROR_TEXT_MAX = 300

# --- 输出上限（超限截断并标注，不静默丢弃） ---

READ_MAX_BYTES = 256 * 1024
READ_DEFAULT_LINES = 200
READ_MAX_LINES = 1000

GLOB_MAX_RESULTS = 200

GREP_DEFAULT_MAX_HITS = 50
GREP_MAX_HITS = 100
GREP_MAX_FILE_BYTES = 1024 * 1024
GREP_MAX_FILES_SCANNED = 2000
GREP_LINE_PREVIEW_CHARS = 200
GREP_LINE_MATCH_CHARS = 4096
"""单行参与正则匹配的最大字符数（超长行截断后匹配）。"""

# 这些目录默认跳过：不是「安全边界」，是「别把 node_modules 灌进上下文」。
# .git 必须跳——检索工作区不该把内部对象文件当源码扫出来。
_SKIP_DIR_NAMES = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
})


class ProjectToolError(Exception):
    """项目工具领域异常；handler 统一转成模型可读的 JSON 错误。"""


class ProjectToolUsageError(ProjectToolError):
    """参数不合法（路径越界、正则非法、类型错）：模型可改参重试。"""


class ProjectToolStateError(ProjectToolError):
    """会话 / 项目状态问题（非项目会话、项目被删）：模型改参数也无解，如实告知。"""


def _norm(path: str | Path) -> str:
    """Windows 下大小写不敏感的比较键（与 PathGate 的 _within 同规则）。"""
    return os.path.normcase(os.path.normpath(str(path)))


def _within(candidate: Path, root: Path) -> bool:
    """candidate 是否等于 root 或位于 root 之下（resolve 之后才可判）。"""
    c = _norm(candidate)
    r = _norm(root)
    return c == r or c.startswith(r.rstrip("\\/") + os.sep)


def _looks_absolute(text: str) -> bool:
    """拒绝仓库外绝对路径：Unix 根、Windows 盘符、UNC。"""
    return (
        text.startswith("/")
        or text.startswith("\\")
        or bool(re.match(r"^[A-Za-z]:", text))
    )


class CancellationToStopEvent:
    """将 CancellationToken 适配为 threading.Event 接口（供 HarnessRunner 消费）。

    CancellationToken 的 is_cancelled() 为 True 时，stop_event.is_set() 为 True，
    从而触发 HarnessRunner._supervise() 中的 _terminate_process_tree()。
    """

    __slots__ = ("_token",)

    def __init__(self, token: CancellationToken) -> None:
        self._token = token

    def is_set(self) -> bool:
        """与 threading.Event.is_set() 接口一致（供 HarnessRunner._supervise 调用）。"""
        return self._token.is_cancelled()

    def wait(self, timeout: float | None = None) -> bool:
        # threading.Event.wait() 返回 True（超时前被设置）或 False（超时）
        # CancellationToken 没有 wait()；这里轮询直到取消或超时
        deadline = time.monotonic() + timeout if timeout is not None else None
        while not self._token.is_cancelled():
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.05)  # 50ms 轮询间隔
        return True


# ---------------------------------------------------------------------------
# 工具工厂
# ---------------------------------------------------------------------------


def _as_tool_result(action: str, fn):
    """把领域异常转成模型可读的 JSON（不抛出，避免一整轮 Run 直接失败）。

    - usage_error：参数不合法，模型改参即可重试
    - error：会话 / 项目状态问题，重试无用，模型如实转告用户

    **返回的 JSON 会作为 tool_result 回灌给模型**（也会出现在 `run.result`
    事件里），因此文案必须受控：只给必要的相对路径，**不给服务端绝对路径、
    不给原始异常文本**。原始异常一律留在服务端日志。

    领域异常（`ProjectToolUsageError` / `ProjectToolStateError`）的文案由
    本模块本地构造、逐条受控，可以直接对外；其它异常一律走兜底分支。
    """

    def wrapper(arguments: dict[str, object], context: object) -> str:
        try:
            return fn(arguments, context)
        except ProjectToolUsageError as exc:
            return err_response(
                action,
                _bounded_text(str(exc)),
                kind="usage_error",
                code=PROJECT_TOOL_CODES["usage"],
            )
        except ProjectToolStateError as exc:
            return err_response(
                action,
                _bounded_text(str(exc)),
                kind="error",
                code=PROJECT_TOOL_CODES["state"],
            )
        except Exception as exc:  # noqa: BLE001 - runner.run() 等底层抛任意异常都兜住
            import logging
            logging.getLogger("mini_deerflow.project_tools").error(
                "project tool 异常（原文只进日志，对外只给受控说明）: %s", exc, exc_info=True
            )
            # 原始异常的 message 可能带绝对路径、内部栈或凭据片段 → 不外泄。
            return err_response(
                action,
                "工具内部错误，请重试或联系管理员",
                kind="error",
                code=PROJECT_TOOL_CODES["internal"],
            )

    return wrapper


def _bounded_text(text: str) -> str:
    """把对外文案限制在 `_ERROR_TEXT_MAX` 以内。"""
    text = text.strip()
    if len(text) > _ERROR_TEXT_MAX:
        return text[:_ERROR_TEXT_MAX] + "…"
    return text


def _executor_error(raw: str, fallback: str) -> str:
    """执行器（harness）返回的原文只进服务端日志，对外给受控说明。

    harness 的 error 可能带 harness 根目录、python 解释器路径等**服务端
    绝对路径**；而这段文案会随工具结果回灌给模型 → 等于把服务端目录结构
    发给外部 Provider。因此原文只进日志，对外只给 `fallback` 这句受控说明。

    执行失败的具体原因由 `answer`（harness 的最终回答）承载 —— 那是模型
    本来就该读的产物，不属于「原始异常」。
    """
    import logging
    if raw:
        logging.getLogger("mini_deerflow.project_tools").warning(
            "执行器返回的原文（只进日志，对外只给受控说明）: %s", raw
        )
    return fallback


def _bound_repo(
    thread_store: ThreadStore,
    project_store: ProjectStore,
    thread_id: str,
) -> tuple[str, Path]:
    """thread_id → 落库项目 → (project_id, 已解析的绑定仓库根)。

    只读工具与 project_execute 共用的反查入口；每次调用都从 store
    现查（不缓存）：项目改绑、删除后下一次调用立刻反映。
    """
    try:
        thread = thread_store.get(thread_id)
    except ThreadNotFoundError as exc:
        raise ProjectToolStateError(f"会话不存在: {thread_id}") from exc
    if not thread.project_id:
        raise ProjectToolStateError("当前会话不是项目会话，没有绑定仓库")
    try:
        project = project_store.get(thread.project_id)
    except StoreProjectNotFoundError as exc:
        raise ProjectToolStateError(
            f"会话绑定的项目不存在或已被删除: {thread.project_id}"
        ) from exc
    # 优先使用 workspace_path（新项目），fallback 到 legacy_repo_path（旧项目）
    if project.workspace_path is not None:
        repo_path = project.workspace_path
    elif project.legacy_repo_path is not None:
        repo_path = project.legacy_repo_path
    else:
        raise ProjectToolStateError(
            f"项目 {project.project_id} 既没有 workspace_path 也没有 legacy_repo_path"
        )
    return thread.project_id, Path(repo_path).resolve()


def create_project_read_tools(
    thread_store: ThreadStore,
    project_store: ProjectStore,
) -> list[Tool]:
    """创建三个只读工具；stores 以闭包注入，与 context 工具同模式。

    每次调用都从 store 反查绑定目录（不缓存）：项目改绑、删除后
    下一次调用立刻反映，不需要失效逻辑。
    """

    def _repo_root(thread_id: str) -> Path:
        return _bound_repo(thread_store, project_store, thread_id)[1]

    def _resolve_in_repo(repo: Path, raw: object, *, what: str) -> Path:
        """把模型给的相对路径解析为仓库内真实路径。"""
        if not isinstance(raw, str) or not raw.strip():
            raise ProjectToolUsageError(f"{what}不能为空")
        if "\x00" in raw:
            raise ProjectToolUsageError(f"{what}不能包含 NUL 字符")
        text = raw.strip()
        if _looks_absolute(text):
            raise ProjectToolUsageError(
                f"{what}必须是仓库内相对路径（不接受绝对路径）"
            )
        parts = [p for p in text.replace("\\", "/").split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise ProjectToolUsageError(f"{what}不能包含 '..' 组件")
        try:
            # resolve() 顺带消解符号链接；指向仓库外的链接会落到真实位置，
            # 随后的边界检查自然拦下。
            candidate = repo.joinpath(*parts).resolve() if parts else repo
        except OSError as exc:
            # 原始 OSError 的 message 可能带绝对路径（POSIX 上形如
            # "[Errno 13] Permission denied: '/home/user/secret'"），
            # 而本异常会经 safe_failure_detail 直接进客户端 SSE 事件。
            # 因此**客户端只拿受控说明**，原始异常与绝对路径留在服务端日志。
            import logging
            logging.getLogger("mini_deerflow.project_tools").warning(
                "路径无法解析（%s）: %s", what, exc, exc_info=True
            )
            raise ProjectToolUsageError(f"{what}无法解析（路径不可访问）") from None
        if not _within(candidate, repo):
            raise ProjectToolUsageError(f"{what}越出仓库绑定目录（符号链接同样不允许）")
        return candidate

    def _display_path(repo: Path, resolved: Path) -> str:
        """仓库内相对展示路径（POSIX 分隔符，便于模型引用）。"""
        try:
            return resolved.relative_to(repo).as_posix() or "."
        except ValueError:  # pragma: no cover - _within 已保证不发生
            return resolved.as_posix()

    # --- project_read_file ---

    def _read_file(arguments: dict[str, object], context: object) -> str:
        repo = _repo_root(_require_thread_id(context))
        target = _resolve_in_repo(repo, arguments.get("path"), what="path")
        if not target.exists():
            return err_response(
                "read_file", f"文件不存在: {arguments.get('path')}",
                kind="usage_error",
                code=PROJECT_TOOL_CODES["usage"],
            )
        if not target.is_file():
            return err_response(
                "read_file", f"不是文件: {arguments.get('path')}",
                kind="usage_error",
                code=PROJECT_TOOL_CODES["usage"],
            )

        offset = _int_arg(arguments, "offset", default=0, minimum=0)
        limit = _int_arg(
            arguments, "limit", default=READ_DEFAULT_LINES, minimum=1
        )
        limit = min(limit, READ_MAX_LINES)

        try:
            size = target.stat().st_size
            with target.open("rb") as fh:
                chunk = fh.read(READ_MAX_BYTES)
        except OSError as exc:
            # OSError 的 message 会带**绝对路径**（POSIX 上形如
            # "[Errno 13] Permission denied: '/home/user/secret'"，Windows 上
            # 带盘符全路径）。本错误结果会回灌给模型 → 等于把服务端目录结构
            # 发给外部 Provider。因此：**原始异常只进服务端日志**，对外只给
            # 受控说明 + 仓库**相对**路径（模型改参要用）。
            import logging
            logging.getLogger("mini_deerflow.project_tools").warning(
                "read_file 读取失败（%s）: %s",
                _display_path(repo, target),
                exc,
                exc_info=True,
            )
            return err_response(
                "read_file",
                f"文件无法读取（权限或 IO 错误）：{_display_path(repo, target)}",
                kind="error",
                code=PROJECT_TOOL_CODES["file_unreadable"],
            )
        text = chunk.decode("utf-8", errors="replace")
        lines = text.splitlines()
        byte_truncated = size > len(chunk)

        selected = lines[offset : offset + limit]
        rendered = "\n".join(
            f"{offset + i + 1}| {line}" for i, line in enumerate(selected)
        )
        return ok_response(
            action="read_file",
            path=_display_path(repo, target),
            offset=offset,
            returned=len(selected),
            total_lines=len(lines) if not byte_truncated else None,
            byte_truncated=byte_truncated,
            lines_truncated=offset + len(selected) < len(lines),
            content=rendered,
        )

    # --- project_glob ---

    def _glob(arguments: dict[str, object], context: object) -> str:
        repo = _repo_root(_require_thread_id(context))
        raw_pattern = arguments.get("pattern")
        if not isinstance(raw_pattern, str) or not raw_pattern.strip():
            return err_response(
                "glob", "pattern 不能为空", kind="usage_error",
                code=PROJECT_TOOL_CODES["usage"],
            )
        pattern = raw_pattern.strip().replace("\\", "/")
        if "\x00" in pattern:
            return err_response(
                "glob", "pattern 不能包含 NUL 字符", kind="usage_error",
                code=PROJECT_TOOL_CODES["usage"],
            )
        if _looks_absolute(pattern) or ".." in pattern.split("/"):
            return err_response(
                "glob", "pattern 必须是仓库内相对路径，且不含 '..'",
                kind="usage_error",
                code=PROJECT_TOOL_CODES["usage"],
            )

        try:
            matches = sorted(repo.glob(pattern))
        except (OSError, ValueError) as exc:
            # ValueError：pathlib 拒绝的模式（如 "**" 之外的畸形写法）
            # OSError：可能带**绝对路径** → 原文只进日志，对外只给受控说明
            import logging
            logging.getLogger("mini_deerflow.project_tools").warning(
                "glob pattern 非法: %s", exc, exc_info=True
            )
            return err_response(
                "glob", "pattern 非法，无法展开", kind="usage_error",
                code=PROJECT_TOOL_CODES["usage"],
            )

        hits: list[str] = []
        truncated = False
        for match in matches:
            # 通配展开出的每一项都要过边界：**/ 能穿过指向仓库外的符号链接
            try:
                resolved = match.resolve()
            except OSError:
                continue
            if not _within(resolved, repo):
                continue
            rel_parts = resolved.relative_to(repo).parts if resolved != repo else ()
            if any(part in _SKIP_DIR_NAMES for part in rel_parts):
                continue
            if len(hits) >= GLOB_MAX_RESULTS:
                truncated = True
                break
            hits.append(_display_path(repo, resolved))

        return ok_response(
            action="glob",
            pattern=pattern,
            returned=len(hits),
            truncated=truncated,
            paths=hits,
        )

    # --- project_grep ---

    # 字面检索上线前使用正则作为过渡。
    # 正则参数名为 pattern（与 glob/grep 习惯一致）；后续切字面时只需改这里。
    _USE_LITERAL = True
    _LITERAL_DEADLINE_SECONDS = 10.0  # 整个扫描的墙钟上限（秒）

    def _grep(arguments: dict[str, object], context: object) -> str:
        repo = _repo_root(_require_thread_id(context))
        raw_pattern = arguments.get("pattern")
        if not isinstance(raw_pattern, str) or not raw_pattern.strip():
            return err_response(
                "grep", "pattern 不能为空", kind="usage_error",
                code=PROJECT_TOOL_CODES["usage"],
            )

        base = _resolve_in_repo(
            repo, arguments.get("path") or ".", what="path"
        )
        if not base.exists():
            return err_response(
                "grep", f"路径不存在: {arguments.get('path')}",
                kind="usage_error",
                code=PROJECT_TOOL_CODES["usage"],
            )

        max_hits = min(
            _int_arg(arguments, "max_hits", default=GREP_DEFAULT_MAX_HITS, minimum=1),
            GREP_MAX_HITS,
        )

        if base.is_file():
            files = [base]
        else:
            files = []
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES]
                files.extend(Path(dirpath) / name for name in filenames)

        if _USE_LITERAL:
            needle = raw_pattern.strip().lower() if arguments.get("ignore_case") else raw_pattern.strip()
            if not needle:
                return err_response(
                    "grep", "pattern 不能为空", kind="usage_error",
                    code=PROJECT_TOOL_CODES["usage"],
                )
            deadline = time.monotonic() + _LITERAL_DEADLINE_SECONDS
            hits: list[dict] = []
            deadline_exceeded = False
            hits_truncated = False
            for file_path in files:
                if time.monotonic() >= deadline:
                    deadline_exceeded = True
                    break
                try:
                    resolved = file_path.resolve()
                except OSError:
                    continue
                if not _within(resolved, repo):
                    continue
                if any(p in _SKIP_DIR_NAMES for p in resolved.relative_to(repo).parts):
                    continue
                try:
                    if resolved.stat().st_size > GREP_MAX_FILE_BYTES:
                        continue
                    with resolved.open("rb") as fh:
                        raw = fh.read()
                except OSError:
                    continue
                if b"\x00" in raw[:8192]:
                    continue
                text = raw.decode("utf-8", errors="replace")
                for line_no, line in enumerate(text.splitlines(), start=1):
                    haystack = line.lower() if arguments.get("ignore_case") else line
                    if needle not in haystack:
                        continue
                    hits.append({
                        "path": _display_path(repo, resolved),
                        "line": line_no,
                        "text": line[:GREP_LINE_PREVIEW_CHARS],
                    })
                    if len(hits) >= max_hits:
                        hits_truncated = True
                        break  # 本文件内命中已满：不再找更多文件
                if hits_truncated:
                    break  # 已满：跳出文件循环
            return ok_response(
                action="grep", pattern=raw_pattern, returned=len(hits),
                deadline_exceeded=deadline_exceeded, hits_truncated=hits_truncated, hits=hits,
            )

        # 正则过渡路径（上线前保留，可单测）
        try:
            regex = re.compile(
                raw_pattern,
                re.IGNORECASE if arguments.get("ignore_case") else 0,
            )
        except re.error as exc:
            # re.error 的文案描述的是**模型给的 pattern**（如 "unterminated
            # character set at position 5"），不含服务端路径 —— 模型靠它改参，
            # 因此保留，但按上限截断（文案会回灌给模型）。
            return err_response(
                "grep", _bounded_text(f"正则表达式非法: {exc}"), kind="usage_error",
                code=PROJECT_TOOL_CODES["usage"],
            )

        deadline = time.monotonic() + _LITERAL_DEADLINE_SECONDS
        hits = []
        deadline_exceeded = False
        hits_truncated = False
        for file_path in files:
            if time.monotonic() >= deadline:
                deadline_exceeded = True
                break
            try:
                resolved = file_path.resolve()
            except OSError:
                continue
            if not _within(resolved, repo):
                continue
            if any(p in _SKIP_DIR_NAMES for p in resolved.relative_to(repo).parts):
                continue
            try:
                if resolved.stat().st_size > GREP_MAX_FILE_BYTES:
                    continue
                with resolved.open("rb") as fh:
                    raw = fh.read()
            except OSError:
                continue
            if b"\x00" in raw[:8192]:
                continue
            text = raw.decode("utf-8", errors="replace")
            for line_no, line in enumerate(text.splitlines(), start=1):
                clipped = line[:GREP_LINE_MATCH_CHARS]
                if not regex.search(clipped):
                    continue
                if len(hits) >= max_hits:
                    hits_truncated = True
                    break
                hits.append({
                    "path": _display_path(repo, resolved),
                    "line": line_no,
                    "text": line[:GREP_LINE_PREVIEW_CHARS],
                })
            if hits_truncated:
                break
        return ok_response(
            action="grep", pattern=raw_pattern, returned=len(hits),
            deadline_exceeded=deadline_exceeded, hits_truncated=hits_truncated, hits=hits,
        )

    return [
        Tool(
            name=PROJECT_READ_FILE_TOOL_NAME,
            description=(
                "读取绑定仓库内的一个文本文件（只读，不修改任何内容）。"
                "path 为仓库内相对路径（如 src/app.py），不接受绝对路径或 '..'。"
                "结果带行号；超长文件自动按字节截断并标注 byte_truncated。"
                "可用 offset/limit 控制行窗口。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "仓库内相对路径",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "起始行号（0 基），默认 0",
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            f"最多返回多少行，默认 {READ_DEFAULT_LINES}，"
                            f"上限 {READ_MAX_LINES}"
                        ),
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            handler=_as_tool_result("read_file", _read_file),
            needs_context=True,
        ),
        Tool(
            name=PROJECT_GLOB_TOOL_NAME,
            description=(
                "在绑定仓库内按通配符列出文件路径（只读）。"
                "pattern 为仓库内相对通配模式，如 src/**/*.py；"
                "默认跳过 .git、node_modules 等目录，结果条数有上限。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "仓库内相对通配模式",
                    },
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            handler=_as_tool_result("glob", _glob),
            needs_context=True,
        ),
        Tool(
            name=PROJECT_GREP_TOOL_NAME,
            description=(
                "在绑定仓库内检索文件内容（只读，不落盘）。"
                "当前为字面检索；path 默认为仓库根；"
                "结果含路径、行号与行文本预览，命中数有上限。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "要检索的字面文本",
                    },
                    "path": {
                        "type": "string",
                        "description": "仓库内相对路径（文件或目录），默认仓库根",
                    },
                    "ignore_case": {
                        "type": "boolean",
                        "description": "是否忽略大小写，默认 false",
                    },
                    "max_hits": {
                        "type": "integer",
                        "description": (
                            f"最多返回多少条命中，默认 {GREP_DEFAULT_MAX_HITS}，"
                            f"上限 {GREP_MAX_HITS}"
                        ),
                    },
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            handler=_as_tool_result("grep", _grep),
            needs_context=True,
        ),
    ]


# --- 快照与 diff ---

SNAPSHOT_FILE_LIMIT = 10 * 1024 * 1024  # 10MB，单文件大小限制
SNAPSHOT_DIR_LIMIT = 10000  # 目录深度限制
SNAPSHOT_TOTAL_LIMIT = 50 * 1024 * 1024  # 50MB，总采集字节限制
DIFF_MAX_LINES = 100  # diff 最多展示的行数（超出置 diff_truncated）
TEXT_EXTENSIONS = {".py", ".js", ".ts", ".jsx", ".tsx", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".sh", ".bat", ".ps1", ".html", ".css", ".sql", ".xml", ".svg", ".c", ".cpp", ".h", ".hpp", ".java", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala", ".r", ".m", ".lua", ".pl", ".pm", ".ex", ".exs", ".erl", ".hrl", ".clj", ".cljs", ".hs", ".fs", ".fsx", ".vb", ".cs", ".fs"}


def _snapshot_workspace(repo_path: "Path") -> dict:
    """采集工作区快照：{path: (is_binary, size, mtime, content|text_preview)}。

    采集失败时返回 complete=False；其余字段尽力返回。
    """
    result: dict = {
        "files": {},
        "complete": True,
        "error": None,
    }
    try:
        root = Path(repo_path).resolve()
        total_bytes = 0
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            # 大小限制
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size > SNAPSHOT_FILE_LIMIT:
                result["files"][str(p.relative_to(root))] = {
                    "type": "binary",
                    "size": size,
                    "note": "file too large",
                }
                continue
            total_bytes += size
            if total_bytes > SNAPSHOT_TOTAL_LIMIT:
                result["complete"] = False
                result["error"] = "snapshot size limit exceeded"
                break
            # 内容
            try:
                is_binary = _is_binary_file(p)
            except OSError:
                is_binary = True
            if is_binary:
                result["files"][str(p.relative_to(root))] = {
                    "type": "binary",
                    "size": size,
                }
            else:
                try:
                    content = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                result["files"][str(p.relative_to(root))] = {
                    "type": "text",
                    "size": size,
                    "mtime": int(p.stat().st_mtime),
                    "preview": content[:2000],
                    "content": content,  # 用于 diff
                }
    except Exception as exc:
        result["complete"] = False
        result["error"] = str(exc)
    return result


def _is_binary_file(path: Path) -> bool:
    """判断文件是否为二进制（包含不可打印字符）。"""
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
        return b"\x00" in chunk
    except OSError:
        return True


def _diff_snapshots(before: dict, after: dict, repo_path: "Path") -> dict:
    """对比两个快照，返回统一结构的文件变更列表。

    二进制文件只显示变化状态，不做 diff。
    """
    before_files = before.get("files", {})
    after_files = after.get("files", {})
    before_keys = set(before_files.keys())
    after_keys = set(after_files.keys())

    files: list[dict] = []

    # 新增文件
    for path in sorted(after_keys - before_keys):
        after_meta = after_files[path]
        entry: dict = {
            "path": path,
            "status": "added",
        }
        if after_meta.get("type") == "binary":
            entry["reason"] = "binary file"
            entry["diff"] = None
        else:
            # 文本文件：给出完整 diff（新增文件 before 为空，即 /dev/null → 新文件）
            diff_result = _compute_diff(
                "",
                after_meta.get("content", ""),
                fromfile="/dev/null",
                tofile=f"b/{path}",
            )
            entry["unified_diff"] = diff_result.get("unified", "")
            entry["diff_truncated"] = diff_result.get("truncated", False)
        files.append(entry)

    # 删除文件（仅在 after 快照完整时判定）
    # 如果 after 快照因大小限制不完整，缺失文件可能是扫描未覆盖，不应标记为删除
    if after.get("complete", True):
        for path in sorted(before_keys - after_keys):
            files.append({
                "path": path,
                "status": "deleted",
            })
    else:
        # after 快照不完整：标记为"未检测到"，不在 files 中记录为 deleted
        pass

    # 共同文件：检查是否修改
    common = before_keys & after_keys
    for path in sorted(common):
        before_meta = before_files[path]
        after_meta = after_files[path]

        # 类型变化
        if before_meta.get("type") != after_meta.get("type"):
            files.append({
                "path": path,
                "status": "modified",
                "change": "type_changed",
                "before_type": before_meta.get("type"),
                "after_type": after_meta.get("type"),
                "diff": None,
            })
            continue

        # 文本文件
        if before_meta.get("type") == "text" and after_meta.get("type") == "text":
            before_content = before_meta.get("content", "")
            after_content = after_meta.get("content", "")
            if before_content != after_content:
                diff_result = _compute_diff(
                    before_content,
                    after_content,
                    fromfile=f"a/{path}",
                    tofile=f"b/{path}",
                )
                files.append({
                    "path": path,
                    "status": "modified",
                    "change": "content_changed",
                    "unified_diff": diff_result.get("unified", ""),
                    "diff_truncated": diff_result.get("truncated", False),
                })
            # else: 内容相同，不记录
            continue

        # 二进制文件
        if before_meta.get("type") == "binary":
            if before_meta.get("size") != after_meta.get("size"):
                files.append({
                    "path": path,
                    "status": "modified",
                    "change": "binary_changed",
                    "before_size": before_meta.get("size"),
                    "after_size": after_meta.get("size"),
                    "diff": None,
                })
            # else: 大小相同，不记录

    # 记录 after 快照不完整时哪些文件未能检测到
    after_incomplete_deleted = []
    if not after.get("complete", True):
        after_incomplete_deleted = sorted(before_keys - after_keys)

    return {"files": files, "after_incomplete_deleted": after_incomplete_deleted}


def _compute_diff(
    before: str,
    after: str,
    *,
    fromfile: str = "",
    tofile: str = "",
) -> dict:
    """计算文本 diff，返回统一格式。

    实现 Unified Diff 格式：只显示前 DIFF_MAX_LINES 行 diff。

    换行必须显式处理：`difflib.unified_diff` 的控制行（--- / +++ / @@）在
    `lineterm=""` 时**不带**结尾换行，直接 `"".join` 会把文件头、hunk 头和
    首行内容粘成一行 —— 前端按行解析时整段 diff 都会丢失。因此这里用
    `"\n".join` 逐行拼接。
    """
    try:
        import difflib
        before_lines = before.splitlines()
        after_lines = after.splitlines()
        diff = list(difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=fromfile,
            tofile=tofile,
            lineterm="",
            n=3,
        ))
        truncated = len(diff) > DIFF_MAX_LINES
        unified = "\n".join(diff[:DIFF_MAX_LINES])
        if truncated:
            # 用 "\" 开头：diff 工具惯例的元信息行，前端解析时跳过
            unified += "\n\\ ... diff 已截断"
        return {"unified": unified, "truncated": truncated}
    except Exception:
        return {"unified": "[diff failed]", "truncated": False}


def create_project_execute_tool(
    thread_store: ThreadStore,
    project_store: ProjectStore,
    runner: "HarnessRunner | None",
    get_lock: "Callable[[str], threading.Lock] | None",
) -> Tool:
    """创建 project_execute 工具。

    所有状态（启动失败 / 仓库被占 / 进程管理 / 占用释放）都在
    handler 内部处理，底层 Runner/锁的引用以闭包注入。

    注意：handler 本身是同步的（返回 JSON 字符串），真正的进程管理
    在 HarnessRunner.run() 的子进程内进行。模型收到的是结构化结果，
    不是流式事件（WM-4 才接 SSE）。
    """

    def _execute(arguments: dict[str, object], context: object) -> str:
        tid = _require_thread_id(context)
        project_id, repo_path = _bound_repo(thread_store, project_store, tid)

        task_raw = arguments.get("task")
        if not isinstance(task_raw, str) or not task_raw.strip():
            return err_response(
                "execute", "task 不能为空", kind="usage_error",
                code=PROJECT_TOOL_CODES["usage"],
            )
        context_raw = arguments.get("context", "")
        if not isinstance(context_raw, str):
            return err_response(
                "execute", "context 必须是字符串", kind="usage_error",
                code=PROJECT_TOOL_CODES["usage"],
            )

        # 从 context 提取取消令牌，转换为 stop_event 传给 HarnessRunner
        cancellation_token: CancellationToken | None = getattr(
            context, "cancellation_token", None
        ) if context is not None else None
        stop_event: threading.Event | None = None
        if cancellation_token is not None:
            stop_event = CancellationToStopEvent(cancellation_token)

        # 事件观察回调（可选）：把 harness 的协议事件原样透传给上层。
        # 由调用方通过上下文提供（生产链路暂不提供 → None → 无额外行为）；
        # 验收测试用它被动观察执行过程，不去抢读 harness 的 stdout 管道。
        raw_on_event = getattr(context, "on_event", None) if context is not None else None
        on_event = raw_on_event if callable(raw_on_event) else None

        # 仓库占用检查（与 ProjectService.delete 共用同一锁实例）
        project_lock = get_lock(project_id) if get_lock else None
        if project_lock is not None and not project_lock.acquire(blocking=False):
            return err_response(
                "execute",
                "该项目正在执行另一个任务，请等待完成后重试",
                kind="error",
                code=PROJECT_TOOL_CODES["state"],
            )

        def _release():
            if project_lock is not None:
                try:
                    project_lock.release()
                except RuntimeError:
                    pass

        # 任务交接规则（固定注入，不依赖模型主动填写）
        _TASK_RULES = (
            "约束：\n"
            "- 按任务需要检索相关文件；只修改完成任务必需的内容。\n"
            "- 不扩展目标，关键信息不足时返回需要澄清。\n"
            "- cwd 由后端根据项目绑定仓库决定，工具调用中不要覆盖。\n"
        )

        # 组装 prompt：模型传入的 context / task 优先，规则固定补充
        # 不覆盖模型已有的上下文；不一律禁止目录检索或要求运行文件
        prompt_parts = []
        if context_raw:
            prompt_parts.append(context_raw.strip())
        prompt_parts.append(f"任务：{task_raw}")
        prompt_parts.append(_TASK_RULES)

        # 引导主 Agent 填写任务范围和验收条件（如有）
        scope = arguments.get("scope", "")
        scope = scope.strip() if isinstance(scope, str) else ""
        acceptance = arguments.get("acceptance", "")
        acceptance = acceptance.strip() if isinstance(acceptance, str) else ""
        if scope:
            prompt_parts.append(f"任务范围：{scope}")
        if acceptance:
            prompt_parts.append(f"验收条件：{acceptance}")

        prompt = "\n\n".join(prompt_parts)

        # 交接要素盘点：让"边界没交过去"这件事在会话内可见（此前是静默缺口）
        handover = _build_handover(context_raw, scope, acceptance)

        # 快照：持有项目锁后采集执行前基线（所有终态都需要）
        baseline = _snapshot_workspace(repo_path)
        baseline_complete = baseline.get("complete", False)

        # 检查 runner 是否可用
        if runner is None or not runner.is_configured:
            _release()
            return err_response(
                "execute",
                "执行器未配置，请联系管理员设置 harness 路径",
                kind="error",
                code=PROJECT_TOOL_CODES["internal"],
            )

        from mini_deerflow.harness_runner import (
            STATUS_COMPLETED,
            STATUS_FAILED,
            STATUS_LAUNCH_FAILED,
            STATUS_PROTOCOL_ERROR,
            STATUS_STOPPED,
            STATUS_TIMEOUT,
        )
        import uuid as _uuid

        try:
            run_id = _uuid.uuid4().hex[:8]
            result = runner.run(
                task_id=run_id,
                repo_path=str(repo_path),
                prompt=prompt,
                on_event=on_event,
                stop_event=stop_event,
            )

            # 快照：执行结束且进程回收后采集执行后状态
            after = _snapshot_workspace(repo_path)
            after_complete = after.get("complete", False)

            # 对比快照
            changes = _diff_snapshots(baseline, after, repo_path)
            changes["baseline_complete"] = baseline_complete
            changes["snapshot_complete"] = after_complete

            # 验证判定（Guard）：**status=completed 只说明执行器跑完了**，
            # 不代表用户的验收条件已经满足。结论一律由代码从证据算出 ——
            # 模型只能转述，不能自己判定"已完成"。
            verification = _build_verification(result)
            guard = evaluate_verification(
                verification,
                acceptance,
                has_changes=bool(changes.get("files")),
            )

            if result.status == STATUS_COMPLETED:
                body = {
                    "ok": True,
                    "status": "completed",
                    "task_id": run_id,
                    "answer": result.final_answer,
                    "files_written": result.files_written,
                    "verification": verification,
                    "guard": guard,
                    "acceptance": acceptance,
                    "handover": handover,
                    "changes": changes,
                    "tool_events": [
                        {
                            "name": e["name"],
                            "duration_s": e.get("duration_s"),
                        }
                        for e in result.tool_events
                    ],
                    "tool_calls": result.tool_calls,
                }
                return ok_response(**body)
            elif result.status == STATUS_STOPPED:
                body = {
                    "ok": False,
                    "status": "stopped",
                    "code": PROJECT_TOOL_CODES["execute_stopped"],
                    "task_id": run_id,
                    "answer": result.final_answer,
                    "files_written": result.files_written,
                    "verification": verification,
                    "guard": guard,
                    "acceptance": acceptance,
                    "handover": handover,
                    "changes": changes,
                    "error": _executor_error(result.error, "任务被停止"),
                    "tool_events": [
                        {"name": e["name"], "duration_s": e.get("duration_s")}
                        for e in result.tool_events
                    ],
                    "tool_calls": result.tool_calls,
                }
                return ok_response(**body)
            elif result.status == STATUS_TIMEOUT:
                body = {
                    "ok": False,
                    "status": "timeout",
                    "code": PROJECT_TOOL_CODES["execute_timeout"],
                    "task_id": run_id,
                    "answer": result.final_answer,
                    "files_written": result.files_written,
                    "verification": verification,
                    "guard": guard,
                    "acceptance": acceptance,
                    "handover": handover,
                    "changes": changes,
                    "error": f"执行超时（{runner._timeout_seconds}s），任务已被终止",
                    "tool_events": [
                        {"name": e["name"], "duration_s": e.get("duration_s")}
                        for e in result.tool_events
                    ],
                    "tool_calls": result.tool_calls,
                }
                return ok_response(**body)
            elif result.status == STATUS_LAUNCH_FAILED:
                return _err_with_guard(
                    "execute",
                    _executor_error(result.error, "执行器启动失败，请检查执行器配置"),
                    PROJECT_TOOL_CODES["launch_failed"],
                    execution_failed_verdict(
                        "执行器启动失败，本轮没有任何验证证据"
                    ),
                )
            elif result.status == STATUS_PROTOCOL_ERROR:
                return _err_with_guard(
                    "execute",
                    _executor_error(
                        result.error,
                        f"执行器协议错误（{result.unmapped_lines} 行非法输出）",
                    ),
                    PROJECT_TOOL_CODES["protocol_error"],
                    execution_failed_verdict(
                        "执行器协议错误，本轮没有任何可采信的验证证据"
                    ),
                )
            else:  # STATUS_FAILED / unknown
                body = {
                    "ok": False,
                    "status": result.status,
                    "code": PROJECT_TOOL_CODES["execute_failed"],
                    "task_id": run_id,
                    "answer": result.final_answer,
                    "files_written": result.files_written,
                    "verification": verification,
                    "guard": guard,
                    "acceptance": acceptance,
                    "handover": handover,
                    "changes": changes,
                    "error": _executor_error(
                        result.error, f"执行失败（{result.status}）"
                    ),
                    "tool_events": [
                        {"name": e["name"], "duration_s": e.get("duration_s")}
                        for e in result.tool_events
                    ],
                    "tool_calls": result.tool_calls,
                }
                return ok_response(**body)
        finally:
            _release()

    return Tool(
        name=PROJECT_EXECUTE_TOOL_NAME,
        description=(
            "在绑定仓库内执行编码任务（调用 harness 执行器）。"
            "当任务涉及创建文件、修改代码、运行程序或验证输出时，应使用本工具。"
            "四个参数各有分工：task 写要达成的目标；"
            "context 写用户已确认的背景与约束（无则写'无'，不要编造）；"
            "scope 写范围边界（改哪些文件、明确不做什么）；"
            "acceptance 写验收条件（跑什么命令、期望什么结果）。"
            "返回包含 files_written、verification（含退出码）、changes diff、handover"
            "（本次交接带了哪些要素）与 guard（系统按证据算出的权威验证结论："
            "verified 验证通过 / unverified 有改动但未验证 / failed 验证失败）。"
            "**status=completed 只说明执行器跑完了，不代表验收条件已满足** —— "
            "汇报结论一律以 guard 为准，不要自己复述未验证的结果。"
            "已有改动会被保留；不自动提交或推送。只能由项目会话调用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "要执行的编码任务描述。"
                        "当涉及写文件、修改代码、运行程序或验证输出时应填写本参数。"
                    ),
                },
                "context": {
                    "type": "string",
                    "description": "补充背景（如相关文件路径、之前的讨论摘要等）",
                },
                "scope": {
                    "type": "string",
                    "description": "任务范围约束（如'只写 loop.py，不遍历目录'）",
                },
                "acceptance": {
                    "type": "string",
                    "description": "验收条件（如'运行后输出 0-4'），用于判断任务是否完成",
                },
            },
            "required": ["task"],
            "additionalProperties": False,
        },
        handler=_as_tool_result("execute", _execute),
        needs_context=True,
    )


def _err_with_guard(action: str, error: str, code: str, guard: dict) -> str:
    """带验证判定的错误响应。

    ``launch_failed`` / ``protocol_error`` 这两条路径拿不到任何验证记录，
    但它们同样不能让主 Agent 自由写"已完成" —— 所以错误响应里也带
    ``guard``，让结论闸门有依据可依。

    形状与 ``context_shared.err_response`` 一致（ok/action/error/kind/code），
    只多一个 ``guard`` 键。
    """
    import json

    return json.dumps(
        {
            "ok": False,
            "action": action,
            "error": error,
            "kind": "error",
            "code": code,
            "guard": guard,
        },
        ensure_ascii=False,
    )


def _build_handover(context_raw: str, scope: str, acceptance: str) -> dict:
    """盘点本次交接实际带了哪些要素，让缺口在会话内可见。

    背景：交接缺口此前是**静默**的 —— 主 Agent 只填 task 时，执行 Agent 仍会
    收到固定约束规则（`_TASK_RULES`），但"用户确认的边界没有交过去"这件事，
    主 Agent 和用户都看不到，只能等执行结果不对才发现。

    这里把三要素的到位情况显式放进工具结果：主 Agent 在同一会话里就能看到
    `missing`，补齐后再派发；用户也能从结果里知道这一轮交接带了什么。
    """
    provided = {
        "context": bool(context_raw.strip()),
        "scope": bool(scope),
        "acceptance": bool(acceptance),
    }
    missing = [name for name, ok in provided.items() if not ok]
    return {"provided": provided, "missing": missing, "complete": not missing}


def _build_verification(result) -> list[dict]:
    """从 harness 的 tool_events 抽出 bash 命令记录。

    R2 修复：所有终态（completed / stopped / timeout / failed）共用
    同一段事件转换，未完成的命令不再被伪报为空。模型/UI 可以看到
    "已经跑过什么/还没跑过什么"——这是 cancelled mid-task 的关键反馈。
    """
    verification: list[dict] = []
    for e in result.tool_events:
        if e.get("name") != "bash":
            continue
        output = e.get("output", "")
        command_preview = (
            e.get("command_preview") or e.get("args_preview", "")
        )
        # 解析 exit_code：bash 输出格式 "[exit N] output"
        exit_code: int | None = None
        if output.startswith("[exit "):
            end = output.find("]", 7)
            if end > 0:
                try:
                    exit_code = int(output[6:end])
                except ValueError:
                    pass
        verification.append({
            "command": command_preview,
            "command_truncated": e.get("command_truncated", False),
            "output": output,
            "output_truncated": e.get("output_truncated", False),
            "exit_code": exit_code,
            "duration_s": e.get("duration_s"),
            "has_output": bool(output),
        })
    return verification


def _require_thread_id(context: object) -> str:
    """从执行上下文取 thread_id；取不到即拒绝执行。

    与 task_note 同约束：thread_id 只认上下文，模型传什么都没用。
    """
    thread_id = context_attr(context, "thread_id")
    if not thread_id:
        raise ProjectToolStateError("缺少执行上下文（thread_id），无法定位绑定仓库")
    return thread_id


def _int_arg(
    arguments: dict[str, object],
    name: str,
    *,
    default: int,
    minimum: int,
) -> int:
    """取整数参数；非法值抛 usage 类错误（模型可改参重试）。"""
    raw = arguments.get(name)
    if raw is None:
        return default
    # bool 是 int 的子类，显式拒绝
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ProjectToolUsageError(f"{name} 必须是整数，收到: {type(raw).__name__}")
    if raw < minimum:
        raise ProjectToolUsageError(f"{name} 不能小于 {minimum}")
    return raw
