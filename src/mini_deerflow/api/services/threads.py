"""ThreadService（19B 阶段）。

职责：
- 通过 ThreadStore Protocol 执行业务操作，不直接依赖具体实现。
- 将 Store 的异常转换为领域异常，供路由层统一映射到 HTTP。
- 负责 ID 生成；不接触 FastAPI / HTTP 类型，保持纯净可测。
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path

from mini_deerflow.project_store import ProjectNotFoundError, ProjectStore
from mini_deerflow.thread_state import ThreadState
from mini_deerflow.thread_store import (
    ThreadAlreadyExistsError as StoreAlreadyExistsError,
    ThreadNotFoundError as StoreNotFoundError,
    ThreadStore,
)

_LOGGER = logging.getLogger(__name__)


# --- 领域异常（与 HTTP 解耦） ---


class ThreadServiceError(Exception):
    """ThreadService 领域异常基类。"""


class ThreadNotFoundError(ThreadServiceError):
    """请求的 thread_id 不存在。"""

    def __init__(self, thread_id: str) -> None:
        super().__init__(f"未知线程: {thread_id}")
        self.thread_id = thread_id


class ThreadAlreadyExistsError(ThreadServiceError):
    """创建线程时 ID 已被占用。"""

    def __init__(self, thread_id: str) -> None:
        super().__init__(f"线程已存在: {thread_id}")
        self.thread_id = thread_id


class UnknownProjectError(ThreadServiceError):
    """指定的项目不存在。"""

    def __init__(self, project_id: str) -> None:
        super().__init__(f"未知项目: {project_id}")
        self.project_id = project_id


# --- Service ---


class ThreadService:
    """线程领域服务；接受任意 ThreadStore 实现。"""

    def __init__(
        self,
        store: ThreadStore,
        context_assembler: object | None = None,
        workspaces_root: Path | None = None,
        project_store: ProjectStore | None = None,
    ) -> None:
        self._store = store
        # 24：可选。线程删除时同步回收 ContextState 与向量索引两份投影，
        # 否则会留下指向已消失线程的孤儿数据。
        self._context_assembler = context_assembler
        # WM-3C：用于 Thread 创建时分配 sessions 目录。
        self._workspaces_root = workspaces_root
        self._project_store = project_store

    @staticmethod
    def generate_id() -> str:
        """生成新线程 ID；使用 uuid4 hex 前 12 位，足够避免日常冲突。"""
        return uuid.uuid4().hex[:12]

    def create(
        self, thread_id: str | None = None, project_id: str | None = None
    ) -> ThreadState:
        """创建新线程；client_id 为 None 时服务端生成。

        重复 ID 抛出 ThreadAlreadyExistsError。
        如果传入了 project_id 且 workspaces_root / project_store 可用，
        则在 {project_root}/sessions/{thread_id}/ 下创建会话目录。
        """
        new_id = thread_id if thread_id else self.generate_id()
        try:
            state = self._store.create(new_id, project_id)
        except StoreAlreadyExistsError as exc:
            raise ThreadAlreadyExistsError(new_id) from exc

        # 分配 sessions 目录（失败不阻塞 Thread 创建成功）
        if project_id and self._workspaces_root and self._project_store:
            try:
                project = self._project_store.get(project_id)
                if project.project_root:
                    sessions_dir = project.project_root / "sessions" / new_id
                    sessions_dir.mkdir(parents=True, exist_ok=True)
            except (OSError, ProjectNotFoundError) as exc:
                # OSError（目录已存在、无权限等）或 ProjectNotFoundError
                # 都不影响 Thread 创建成功——sessions 目录可在需要时延迟创建。
                _LOGGER.warning(
                    "Failed to create session directory for project=%s, thread=%s: %s",
                    project_id,
                    new_id,
                    exc,
                )

        return state

    def list(self) -> tuple[ThreadState, ...]:
        """列出全部线程；按 Store 实现约定的稳定顺序返回。"""
        return self._store.list_threads()

    def get(self, thread_id: str) -> ThreadState:
        """按 ID 读取线程；不存在抛出 ThreadNotFoundError。"""
        try:
            return self._store.get(thread_id)
        except StoreNotFoundError as exc:
            raise ThreadNotFoundError(thread_id) from exc

    def delete(self, thread_id: str) -> None:
        """按 ID 删除线程；不存在抛出 ThreadNotFoundError。"""
        try:
            self._store.delete(thread_id)
        except StoreNotFoundError as exc:
            raise ThreadNotFoundError(thread_id) from exc
        # 24：事实源删成功后再回收投影（ContextState + 向量索引）。
        # 反过来会在线程仍然存在时丢掉摘要。
        # 回收失败**不能只记日志就返回成功**——那等于告诉调用方「全清干净了」，
        # 而共享 store 里其实还留着指向已消失线程的孤儿数据。因此：
        #   · 日志里留完整细节（异常类型、栈、线程号）
        #   · 对外抛错，让响应成为明确的失败
        #   · 文案不带内部路径与异常类名（E2E 断言了响应不泄漏内部信息）
        drop = getattr(self._context_assembler, "drop_thread_projections", None)
        if callable(drop):
            try:
                drop(thread_id)
            except Exception:
                _LOGGER.error(
                    "线程 %s 的事实源已删除，但上下文投影清理失败",
                    thread_id,
                    exc_info=True,
                )
                raise ThreadServiceError(
                    f"线程 {thread_id} 已删除，但其上下文数据清理未完成"
                ) from None