"""Project REST API 的 Pydantic Schema（工作模式第 2 步）。

设计原则沿用 `threads.py`：只暴露稳定字段，不外泄 schema_version，
不依赖 `mini_deerflow.project_state`（避免领域类型被 API 子包直接耦合）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class ProjectSummary(BaseModel):
    """项目对外视图。"""

    project_id: str
    name: str
    workspace_path: str | None = None
    created_at: str
    legacy: bool = False


class ProjectListResponse(BaseModel):
    """GET /api/projects 的响应包装。"""

    projects: list[ProjectSummary] = Field(default_factory=list)


class CreateProjectRequest(BaseModel):
    """POST /api/projects 请求体。

    只接收项目名，后端自动创建托管工作区目录。
    """

    name: str = Field(min_length=1, max_length=200, description="项目显示名")

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("项目名不能为空白")
        return v.strip()


class BrowseEntry(BaseModel):
    """工作区文件树列表项。"""

    name: str
    is_dir: bool
    size: int = 0
    modified: str | None = None


class BrowseResponse(BaseModel):
    """GET /api/projects/{id}/workspace/browse 的响应。"""

    path: str
    is_root: bool
    entries: list[BrowseEntry] = Field(default_factory=list)
    truncated: bool = False


class FileContentResponse(BaseModel):
    """GET /api/projects/{id}/workspace/file 的响应。"""

    path: str
    content: str
    truncated: bool = False
    total_lines: int | None = None


def project_to_summary(
    project_id: str, name: str, workspace_path: str | None, created_at: str, legacy: bool = False
) -> ProjectSummary:
    """由字段构造响应；不读取 Store，避免在 Schema 层产生副作用。"""
    return ProjectSummary(
        project_id=project_id,
        name=name,
        workspace_path=workspace_path,
        created_at=created_at,
        legacy=legacy,
    )