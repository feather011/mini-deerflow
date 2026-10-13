"""项目仓库接口与内存实现、SQLite 实现。

结构照 `thread_store.py`，理由相同：SQLite 单连接被 FastAPI 同步路由的
线程池共用，必须同时做 `check_same_thread=False` 与串行化。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Protocol, runtime_checkable

from mini_deerflow.project_state import Project


# --- 异常体系 ---


class ProjectStoreError(Exception):
    """项目仓库错误的基类。"""


class ProjectAlreadyExistsError(ProjectStoreError):
    """创建已存在的项目 ID 时抛出。"""


class ProjectRepoAlreadyBoundError(ProjectStoreError):
    """同一仓库路径已被其他项目绑定（已废弃，保留供兼容性）。"""


class ProjectNotFoundError(ProjectStoreError):
    """查找/保存/删除未知项目时抛出。"""


class ProjectStoreClosedError(ProjectStoreError):
    """关闭后继续操作 Store 时抛出。"""


class ProjectDataCorruptedError(ProjectStoreError, ValueError):
    """数据库中的项目数据无法解析时抛出；错误信息包含对应 project_id。"""


# --- 接口 ---


@runtime_checkable
class ProjectStore(Protocol):
    """项目仓库：管理 Project 的创建、读取、更新、删除与列表。"""

    def create(
        self,
        project_id: str,
        name: str,
        project_root: Path | None = None,
        workspace_path: Path | None = None,
        legacy_repo_path: str | None = None,
    ) -> Project: ...
    def save(self, project: Project) -> None: ...
    def delete(self, project_id: str) -> None: ...
    def exists(self, project_id: str) -> bool: ...
    def list_projects(self) -> tuple[Project, ...]: ...
    def find_by_repo_path(self, legacy_repo_path: str) -> Project | None: ...


def _norm(path: str) -> str:
    """仓库路径的比较键：Windows 下大小写不敏感，且统一分隔符与尾随斜杠。"""
    return str(path).replace("/", "\\").rstrip("\\").lower()


# --- 内存实现 ---


class InMemoryProjectStore:
    """项目仓库的内存实现，私有字典持有所有 Project。"""

    def __init__(self) -> None:
        self._projects: dict[str, Project] = {}

    def create(
        self,
        project_id: str,
        name: str,
        project_root: Path | None = None,
        workspace_path: Path | None = None,
        legacy_repo_path: str | None = None,
    ) -> Project:
        """创建并保存新 Project；重复 ID 抛出。"""
        if project_id in self._projects:
            raise ProjectAlreadyExistsError(f"项目已存在: {project_id}")
        project = Project(
            project_id=project_id,
            name=name,
            project_root=project_root,
            workspace_path=workspace_path,
            legacy_repo_path=legacy_repo_path,
            legacy=legacy_repo_path is not None,
        )
        self._projects[project_id] = project
        return project

    def get(self, project_id: str) -> Project:
        """获取已有 Project；不存在时抛出 ProjectNotFoundError。"""
        if project_id not in self._projects:
            raise ProjectNotFoundError(f"未知项目: {project_id}")
        return self._projects[project_id]

    def save(self, project: Project) -> None:
        """更新已有 Project；非 Project 抛 TypeError，未知 ID 抛 ProjectNotFoundError。"""
        if not isinstance(project, Project):
            raise TypeError(f"只接受 Project，收到: {type(project).__name__}")
        if project.project_id not in self._projects:
            raise ProjectNotFoundError(f"未知项目: {project.project_id}")
        self._projects[project.project_id] = project

    def delete(self, project_id: str) -> None:
        """删除已有 Project；不存在时抛出 ProjectNotFoundError。"""
        if project_id not in self._projects:
            raise ProjectNotFoundError(f"未知项目: {project_id}")
        del self._projects[project_id]

    def exists(self, project_id: str) -> bool:
        """检查 Project 是否存在；从不抛出 ProjectNotFoundError。"""
        return project_id in self._projects

    def list_projects(self) -> tuple[Project, ...]:
        """按 project_id 排序返回所有 Project 的不可变快照。"""
        return tuple(
            sorted(self._projects.values(), key=lambda p: p.project_id)
        )

    def find_by_repo_path(self, legacy_repo_path: str) -> Project | None:
        """按旧仓库路径查项目；未绑定返回 None。"""
        if not legacy_repo_path:
            return None
        target = _norm(legacy_repo_path)
        for project in self._projects.values():
            if project.legacy_repo_path and _norm(project.legacy_repo_path) == target:
                return project
        return None


# --- SQLite 实现 ---


class SQLiteProjectStore:
    """基于 SQLite 的 ProjectStore 实现：项目以 JSON 形式持久化到数据库。"""

    def __init__(self, database_path: str | Path) -> None:
        self._path = Path(database_path)
        self._closed = False
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS projects ("
                "  project_id TEXT PRIMARY KEY,"
                "  state_json TEXT NOT NULL"
                ")"
            )
            self._conn.commit()

    def __enter__(self) -> "SQLiteProjectStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        """关闭数据库连接；重复调用安全无副作用。"""
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def _ensure_open(self) -> None:
        """操作前检查连接；已关闭时抛出清晰错误。"""
        if self._closed:
            raise ProjectStoreClosedError(
                f"Store 已关闭，无法操作: {self._path}"
            )

    def _serialize(self, project: Project) -> str:
        return json.dumps(project.to_dict(), ensure_ascii=False)

    def _deserialize(self, raw: str, project_id: str) -> Project:
        """解析 state_json；任何非法内容都包装为 ProjectDataCorruptedError。"""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ProjectDataCorruptedError(
                f"项目 {project_id} 的数据损坏: JSON 非法 ({exc})"
            ) from exc

        if not isinstance(data, dict):
            raise ProjectDataCorruptedError(
                f"项目 {project_id} 的数据损坏: JSON 顶层应为字典，"
                f"实际为 {type(data).__name__}"
            )

        try:
            restored = Project.from_dict(data)
        except ValueError as exc:
            raise ProjectDataCorruptedError(
                f"项目 {project_id} 的数据损坏: {exc}"
            ) from exc

        if restored.project_id != project_id:
            raise ProjectDataCorruptedError(
                f"项目 {project_id} 的数据损坏: 内部 project_id="
                f"{restored.project_id!r} 与数据库主键不一致"
            )
        return restored

    def create(
        self,
        project_id: str,
        name: str,
        project_root: Path | None = None,
        workspace_path: Path | None = None,
        legacy_repo_path: str | None = None,
    ) -> Project:
        """创建新 Project；重复 ID 抛出。"""
        project = Project(
            project_id=project_id,
            name=name,
            project_root=project_root,
            workspace_path=workspace_path,
            legacy_repo_path=legacy_repo_path,
            legacy=legacy_repo_path is not None,
        )
        with self._lock:
            self._ensure_open()
            try:
                with self._conn:
                    self._conn.execute(
                        "INSERT INTO projects (project_id, state_json)"
                        " VALUES (?, ?)",
                        (project_id, self._serialize(project)),
                    )
            except sqlite3.IntegrityError:
                raise ProjectAlreadyExistsError(
                    f"项目已存在: {project_id}"
                ) from None
        return project

    def get(self, project_id: str) -> Project:
        """获取已有 Project；不存在时抛出 ProjectNotFoundError。"""
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT state_json FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            raise ProjectNotFoundError(f"未知项目: {project_id}")
        return self._deserialize(row[0], project_id)

    def save(self, project: Project) -> None:
        """更新已有 Project；整个写入在事务中执行。"""
        if not isinstance(project, Project):
            raise TypeError(f"只接受 Project，收到: {type(project).__name__}")
        with self._lock:
            self._ensure_open()
            with self._conn:
                cursor = self._conn.execute(
                    "UPDATE projects SET state_json = ? WHERE project_id = ?",
                    (self._serialize(project), project.project_id),
                )
                if cursor.rowcount == 0:
                    raise ProjectNotFoundError(f"未知项目: {project.project_id}")

    def delete(self, project_id: str) -> None:
        """删除已有 Project；不存在时抛出 ProjectNotFoundError。"""
        with self._lock:
            self._ensure_open()
            with self._conn:
                cursor = self._conn.execute(
                    "DELETE FROM projects WHERE project_id = ?", (project_id,)
                )
                if cursor.rowcount == 0:
                    raise ProjectNotFoundError(f"未知项目: {project_id}")

    def exists(self, project_id: str) -> bool:
        """检查 Project 是否存在；从不抛出 ProjectNotFoundError。"""
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT 1 FROM projects WHERE project_id = ? LIMIT 1", (project_id,)
            ).fetchone()
        return row is not None

    def list_projects(self) -> tuple[Project, ...]:
        """按 project_id 排序返回所有 Project；任一记录损坏则抛 ProjectDataCorruptedError。"""
        with self._lock:
            self._ensure_open()
            rows = self._conn.execute(
                "SELECT project_id, state_json FROM projects ORDER BY project_id"
            ).fetchall()
        return tuple(self._deserialize(raw, pid) for pid, raw in rows)

    def find_by_repo_path(self, legacy_repo_path: str) -> Project | None:
        """按旧仓库路径查项目；未绑定返回 None。"""
        if not legacy_repo_path:
            return None
        target = _norm(legacy_repo_path)
        for project in self.list_projects():
            if project.legacy_repo_path and _norm(project.legacy_repo_path) == target:
                return project
        return None