"""项目数据模型（工作模式 → 托管工作区方案）。

Project 是「一个由后端托管工作区的会话容器」，只存元信息，不持有消息。
旧数据（repo_path）迁移到 legacy_repo_path；新项目使用 project_root / workspace_path。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


PROJECT_SCHEMA_VERSION = 2


def utc_now_iso() -> str:
    """当前 UTC 时间的 ISO-8601 字符串（秒精度）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Project:
    """一个项目：唯一 ID + 显示名 + 后端托管工作区路径。

    旧项目（schema_version=1）只有 repo_path，迁移后：
    - project_root / workspace_path 由后端生成
    - legacy_repo_path 保留旧值供迁移用
    - legacy=True 标记待迁移状态
    """

    project_id: str
    name: str
    project_root: Path | None = None  # {workspaces_root}/{project_id}/
    workspace_path: Path | None = None  # project_root / "workspace"
    created_at: str = ""
    legacy: bool = False  # 旧项目标记（repo_path 绑定）
    legacy_repo_path: str | None = None  # 旧仓库路径，供迁移用

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, str) or not self.project_id.strip():
            raise ValueError("project_id 必须是非空字符串")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("name 不能为空")
        if not self.created_at:
            self.created_at = utc_now_iso()

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {
            "schema_version": PROJECT_SCHEMA_VERSION,
            "project_id": self.project_id,
            "name": self.name,
            "created_at": self.created_at,
            "legacy": self.legacy,
        }
        if self.project_root is not None:
            d["project_root"] = str(self.project_root)
        if self.workspace_path is not None:
            d["workspace_path"] = str(self.workspace_path)
        if self.legacy_repo_path is not None:
            d["legacy_repo_path"] = self.legacy_repo_path
        return d

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Project":
        """从字典恢复 Project；非法数据抛 ValueError，输入不被修改。"""
        if not isinstance(data, dict):
            raise ValueError(f"data 必须是字典，收到: {type(data).__name__}")

        if "schema_version" not in data:
            raise ValueError("Project 缺少字段: schema_version")
        version = data["schema_version"]
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValueError(
                f"schema_version 必须是整数，收到: {type(version).__name__}"
            )
        if version > PROJECT_SCHEMA_VERSION:
            raise ValueError(
                f"未知 schema_version: {version}（当前: {PROJECT_SCHEMA_VERSION}）"
            )

        for key in ("project_id", "name"):
            value = data.get(key)
            if not isinstance(value, str):
                raise ValueError(f"Project 缺少字段或类型错误: {key}")

        created_at = data.get("created_at", "")
        if not isinstance(created_at, str):
            raise ValueError("created_at 必须是字符串")

        # schema_version=1 兼容：旧数据只有 repo_path
        legacy = version == 1
        legacy_repo_path: str | None = None
        project_root: Path | None = None
        workspace_path: Path | None = None

        if legacy:
            legacy_repo_path = data.get("repo_path")
        else:
            legacy_repo_path = data.get("legacy_repo_path")
            pr = data.get("project_root")
            if pr is not None:
                project_root = Path(pr)
            wp = data.get("workspace_path")
            if wp is not None:
                workspace_path = Path(wp)

        try:
            return cls(
                project_id=str(data["project_id"]),
                name=str(data["name"]),
                project_root=project_root,
                workspace_path=workspace_path,
                created_at=created_at,
                legacy=legacy,
                legacy_repo_path=legacy_repo_path,
            )
        except ValueError as exc:
            raise ValueError(f"Project 字段非法: {exc}") from exc