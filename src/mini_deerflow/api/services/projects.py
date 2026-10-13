"""ProjectService（Phase 1: 后端托管工作区目录）。

职责：
- 通过 ProjectStore Protocol 执行业务操作，不直接依赖具体实现。
- 把 Store 异常转换为领域异常，供路由层统一映射到 HTTP。
- 管理项目工作区目录（创建、浏览、读取、下载）。
- 提供项目级别的线程锁。
- 不接触 FastAPI / HTTP 类型。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import uuid
import zipfile
from io import BytesIO
from pathlib import Path

from mini_deerflow.project_state import Project
from mini_deerflow.project_store import (
    ProjectStore,
    ProjectAlreadyExistsError as StoreAlreadyExistsError,
    ProjectNotFoundError as StoreNotFoundError,
)
from mini_deerflow.api.services.threads import ThreadService
from mini_deerflow.thread_store import ThreadStore

_LOGGER = logging.getLogger(__name__)

# 读取文件的最大字节数
READ_MAX_BYTES = 500_000


# --- 领域异常（与 HTTP 解耦） ---


class ProjectServiceError(Exception):
    """ProjectService 领域异常基类。"""


class ProjectNotFoundError(ProjectServiceError):
    """请求的 project_id 不存在。"""

    def __init__(self, project_id: str) -> None:
        super().__init__(f"未知项目: {project_id}")
        self.project_id = project_id


class ProjectAlreadyExistsError(ProjectServiceError):
    """创建项目时 ID 已被占用。"""

    def __init__(self, project_id: str) -> None:
        super().__init__(f"项目已存在: {project_id}")
        self.project_id = project_id


class ProjectPathError(ProjectServiceError):
    """路径越界或无效。"""


class ProjectBusyError(ProjectServiceError):
    """项目正忙。"""


# --- 路径工具 ---


def _norm(path: str | Path) -> str:
    """Windows 下大小写不敏感的比较键。"""
    return os.path.normcase(os.path.normpath(str(path)))


def _within(candidate: Path, root: Path) -> bool:
    """candidate 是否等于 root 或位于 root 之下。

    用 normcase + normpath 而不是 `Path.is_relative_to`，因为后者在
    Windows 上区分大小写，`c:\\users\\x` 会被判为不在 `C:\\Users\\X` 内。
    """
    c = _norm(candidate)
    r = _norm(root)
    return c == r or c.startswith(r.rstrip("\\/") + os.sep)


# --- Service ---


class ProjectService:
    """项目领域服务；接受任意 ProjectStore 实现。"""

    def __init__(
        self,
        store: ProjectStore,
        workspaces_root: str | None,
        thread_store: ThreadStore,
        context_assembler: object | None = None,
        shared_locks: "dict[str, threading.Lock] | None" = None,
        shared_locks_lock: "threading.Lock | None" = None,
    ) -> None:
        self._store = store
        self._workspaces_root = (
            Path(workspaces_root).expanduser().resolve() if workspaces_root else Path()
        )
        self._thread_service = ThreadService(
            thread_store,
            context_assembler,
            workspaces_root=self._workspaces_root,
            project_store=store,
        )
        # 若调用方传入了共享锁字典，则与 Agent 工具层共用同一实例；
        # 未传入时保持向后兼容（测试等独立场景各用各的）。
        if shared_locks is not None:
            self._locks = shared_locks
            self._locks_lock = shared_locks_lock or threading.Lock()
        else:
            self._locks = {}
            self._locks_lock = threading.Lock()

    @staticmethod
    def generate_id() -> str:
        """生成新项目 ID。"""
        return uuid.uuid4().hex[:12]

    def create(self, name: str) -> Project:
        """创建项目：生成 ID → 创建目录结构 → 初始化 Git → 落库。"""
        import sqlite3

        project_id = self.generate_id()
        project_root = self._workspaces_root / project_id
        workspace_path = project_root / "workspace"

        # 按创建顺序记录，以便失败时逆序删除
        created: list[Path] = []

        try:
            # 1. 创建项目根目录
            project_root.mkdir(parents=True, exist_ok=False)
            created.append(project_root)

            # 2. 创建 workspace 目录
            workspace_path.mkdir(parents=False, exist_ok=False)
            created.append(workspace_path)

            # 3. 创建 sessions 目录（用于未来会话数据）
            sessions_path = project_root / "sessions"
            sessions_path.mkdir(parents=False, exist_ok=False)
            created.append(sessions_path)

            # 4. Git init workspace
            try:
                subprocess.run(
                    ["git", "init"],
                    cwd=str(workspace_path),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=True,
                )
            except subprocess.CalledProcessError as exc:
                raise ProjectServiceError(f"Git 初始化失败: {exc.stderr}") from None
            except FileNotFoundError:
                raise ProjectServiceError("系统未安装 Git") from None

            # 5. 写 .project-meta.json
            meta_path = project_root / ".project-meta.json"
            import json

            meta_path.write_text(
                json.dumps(
                    {
                        "project_id": project_id,
                        "name": name,
                        "created_at": str(project_root.stat().st_ctime),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

        except Exception as exc:
            # 失败时逆序删除已创建的目录
            for path in reversed(created):
                try:
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                except OSError:
                    pass
            raise

        # 6. 落库（含 project_root / workspace_path，删除时需要）
        try:
            self._store.create(
                project_id,
                name,
                project_root=project_root,
                workspace_path=workspace_path,
            )
        except sqlite3.OperationalError:
            # DB 操作失败，尝试恢复
            if self._store.exists(project_id):
                # 项目已存在，目录保留（用户可能已手动处理）
                pass
            else:
                # 真正失败，清理目录
                try:
                    shutil.rmtree(project_root)
                except OSError:
                    pass
                raise ProjectServiceError(f"数据库写入失败: {exc}") from exc
        except StoreAlreadyExistsError:
            # ID 冲突，极少见
            try:
                shutil.rmtree(project_root)
            except OSError:
                pass
            raise ProjectAlreadyExistsError(project_id)

        return Project(
            project_id=project_id,
            name=name,
            project_root=project_root,
            workspace_path=workspace_path,
            created_at=project_root.stat().st_ctime,
            legacy=False,
        )

    def workspace_boundary_check(
        self, rel_path: str, workspace_path: Path
    ) -> Path:
        """校验相对路径不越界（包含符号链接检查）。"""
        # 拒绝绝对路径
        if rel_path.startswith("/") or rel_path.startswith("\\"):
            raise ProjectPathError("不接受绝对路径")
        if Path(rel_path).is_absolute():
            raise ProjectPathError("不接受绝对路径")

        # 相对路径 "." 特殊处理：直接用 workspace_path
        if rel_path == ".":
            candidate = workspace_path
        else:
            # 解析候选路径
            candidate = (workspace_path / rel_path).resolve()

        # 检查是否在 workspace 内
        if not _within(candidate, workspace_path):
            raise ProjectPathError("路径越界")

        # 检查父目录符号链接（仅对 "." 以外的路径）
        if rel_path != ".":
            for parent in candidate.parents:
                if _norm(parent) == _norm(workspace_path):
                    break
                resolved_parent = parent.resolve()
                if not _within(resolved_parent, workspace_path):
                    raise ProjectPathError("符号链接越界")

        return candidate

    def browse_workspace(
        self, project_id: str, sub_path: str | None = None
    ) -> dict:
        """浏览工作区目录内容。"""
        try:
            project = self._store.get(project_id)
        except StoreNotFoundError as exc:
            raise ProjectNotFoundError(project_id) from exc

        if project.workspace_path is None:
            raise ProjectServiceError("项目尚未迁移")

        # 边界检查
        self.workspace_boundary_check(sub_path or ".", project.workspace_path)

        base = project.workspace_path / (sub_path or ".")
        rel_path_str = str(Path(sub_path) if sub_path else Path("."))

        entries = []
        with os.scandir(base) as it:
            for entry in it:
                try:
                    stat = entry.stat()
                    entries.append({
                        "name": entry.name,
                        "is_dir": entry.is_dir(follow_symlinks=False),
                        "size": stat.st_size,
                        "modified": None,  # iso 时间戳可选
                    })
                except OSError:
                    # 跳过无法访问的条目
                    pass

        # 排序：目录优先，名字排序
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))

        return {
            "path": rel_path_str,
            "is_root": _norm(base) == _norm(project.workspace_path),
            "entries": entries,
            "truncated": False,
        }

    def get_file_content(self, project_id: str, rel_path: str) -> dict:
        """读取工作区文件内容。"""
        try:
            project = self._store.get(project_id)
        except StoreNotFoundError as exc:
            raise ProjectNotFoundError(project_id) from exc

        if project.workspace_path is None:
            raise ProjectServiceError("项目尚未迁移")

        self.workspace_boundary_check(rel_path, project.workspace_path)

        file_path = project.workspace_path / rel_path
        content_bytes = file_path.read_bytes()

        truncated = len(content_bytes) > READ_MAX_BYTES
        if truncated:
            content_bytes = content_bytes[:READ_MAX_BYTES]

        try:
            content = content_bytes.decode("utf-8", errors="replace")
        except Exception:
            content = "[二进制文件，无法显示]"

        # 简单计算行数
        total_lines = content.count("\n") + 1 if not truncated else None

        return {
            "path": rel_path,
            "content": content,
            "truncated": truncated,
            "total_lines": total_lines,
        }

    def download_workspace_zip(self, project_id: str) -> bytes:
        """打包工作区为 zip 文件（不含 .git）。"""
        try:
            project = self._store.get(project_id)
        except StoreNotFoundError as exc:
            raise ProjectNotFoundError(project_id) from exc

        if project.workspace_path is None:
            raise ProjectServiceError("项目尚未迁移")

        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(project.workspace_path):
                # 跳过 .git 目录
                dirs[:] = [d for d in dirs if d != ".git"]

                for file in files:
                    abs_file = Path(root) / file
                    rel_file = str(abs_file.relative_to(project.workspace_path))

                    # 符号链接越界检查
                    if os.path.islink(abs_file):
                        _LOGGER.warning("跳过符号链接文件: %s", rel_file)
                        resolved = abs_file.resolve()
                        if not _within(resolved, project.workspace_path):
                            raise ProjectPathError(
                                f"符号链接越界: {rel_file} -> {resolved}"
                            )
                        continue

                    # 验证无越界符号链接
                    self.workspace_boundary_check(rel_file, project.workspace_path)

                    zf.writestr(rel_file, abs_file.read_bytes())

        return buffer.getvalue()

    def acquire_project_lock(self, project_id: str) -> bool:
        """尝试获取项目锁。获取成功返回 True，失败抛 ProjectBusyError。"""
        with self._locks_lock:
            if project_id not in self._locks:
                self._locks[project_id] = threading.Lock()
            lock = self._locks[project_id]

        if not lock.acquire(blocking=False):
            raise ProjectBusyError(f"项目 {project_id} 正忙")
        return True

    def get_project_lock(self, project_id: str) -> threading.Lock:
        """返回项目锁（惰性创建）。用于与执行路径共享同一把锁。"""
        with self._locks_lock:
            if project_id not in self._locks:
                self._locks[project_id] = threading.Lock()
            return self._locks[project_id]

    def release_project_lock(self, project_id: str) -> None:
        """释放项目锁。"""
        with self._locks_lock:
            lock = self._locks.get(project_id)
        if lock:
            try:
                lock.release()
            except RuntimeError:
                # 已释放，忽略
                pass

    def delete(self, project_id: str) -> None:
        """删除项目（不级联删会话，由路由层处理）。"""
        with self._locks_lock:
            busy_lock = self._locks.get(project_id)
        if busy_lock and busy_lock.locked():
            raise ProjectBusyError(f"项目 {project_id} 正忙")

        try:
            project = self._store.get(project_id)
        except StoreNotFoundError as exc:
            raise ProjectNotFoundError(project_id) from exc

        try:
            self._store.delete(project_id)
        except StoreNotFoundError as exc:
            raise ProjectNotFoundError(project_id) from exc

        # 删除工作区目录（只删 project_root，不管 legacy_repo_path，保留原仓库）
        if project.project_root and project.project_root.exists():
            try:
                shutil.rmtree(project.project_root)
            except OSError as exc:
                _LOGGER.warning("删除项目目录失败: %s", exc)

    def list(self) -> tuple[Project, ...]:
        """列出全部项目。"""
        return self._store.list_projects()

    def get(self, project_id: str) -> Project:
        """按 ID 读取项目。"""
        try:
            return self._store.get(project_id)
        except StoreNotFoundError as exc:
            raise ProjectNotFoundError(project_id) from exc

    def list_project_threads(self, project_id: str) -> tuple:
        """列出属于指定项目的所有线程。"""
        # 先确认项目存在
        self.get(project_id)
        # 从 thread_store 过滤出属于该项目的线程
        all_threads = self._thread_service.list()
        return tuple(t for t in all_threads if t.project_id == project_id)
