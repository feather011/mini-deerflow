"""File/Artifact API 数据模型（19E 阶段）。"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- FileKind


class FileKind(str, Enum):
    FILE = "file"
    DIRECTORY = "directory"


# --------------------------------------------------------------------------- FileMetadata


class FileMetadata(BaseModel):
    """单个文件的元数据。"""
    path: str = Field(..., description="相对于 workspace 的逻辑路径")
    name: str = Field(..., description="文件名（含扩展名）")
    size: int = Field(..., ge=0, description="文件大小（字节）")
    media_type: str | None = Field(None, description="推断的 MIME 类型")
    kind: FileKind = Field(..., description="条目类型")

    model_config = {"frozen": True}


# --------------------------------------------------------------------------- FileListResponse


class FileListResponse(BaseModel):
    """文件列表响应。"""
    entries: list[FileMetadata] = Field(default_factory=list)
    total: int = Field(..., description="返回条目总数")


# --------------------------------------------------------------------------- UploadResponse（复用 FileMetadata）


UploadResponse = FileMetadata
