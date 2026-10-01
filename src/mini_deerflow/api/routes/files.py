"""File / Artifact API 路由（19E 阶段，19F 统一 OpenAPI）。

端点：
- POST   /api/files               上传到 uploads/（可写、可删）
- GET    /api/files               列出 uploads/
- GET    /api/files/{file_path}  下载 uploads/ 文件
- DELETE /api/files/{file_path}  删除 uploads/ 文件
- GET    /api/artifacts           列出 artifacts/（只读）
- GET    /api/artifacts/{path}    下载 artifacts/ 产物（只读，无删除路由）
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from mini_deerflow.api.schemas.files import (
    FileKind,
    FileListResponse,
    FileMetadata,
    UploadResponse,
)
from mini_deerflow.api.schemas.errors import ErrorResponse
from mini_deerflow.api.services.workspace_file import (
    DirectoryTooLargeError,
    FileAlreadyExistsError,
    FileNotFoundError,
    NotAFileError,
    NotADirectoryError,
    PathViolationError,
    UploadFileTooLargeError,
    WorkspaceFileService,
    WorkspaceServiceError,
)
from mini_deerflow.sandbox_path import SandboxPathViolationError


_LOGGER = logging.getLogger(__name__)

router = APIRouter(tags=["files", "artifacts"])


def _get_file_service(request: Request) -> WorkspaceFileService:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="File 服务未初始化",
        )
    svc = getattr(runtime, "file_service", None)
    if svc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="File 服务未初始化",
        )
    return svc


# --------------------------------------------------------------------------- POST /files


@router.post(
    "/files",
    status_code=status.HTTP_201_CREATED,
    response_model=UploadResponse,
    summary="上传文件到 uploads/",
    description=(
        "将文件上传到 workspace/uploads/ 目录。\n\n"
        "**行为**：\n"
        "- multipart/form-data，`file` 字段携带文件\n"
        "- 可选 `dest` 参数指定相对路径（如 subdir/my.txt）\n"
        "- 默认不覆盖同名文件，冲突返回 409\n"
        "- 超出 max_upload_bytes 限制返回 413\n"
        "- 上传失败不留下目标文件或临时文件\n"
        "- 流式处理，每次读取 64 KiB，不在内存中累积完整文件"
    ),
    responses={
        201: {"description": "文件上传成功"},
        400: {"model": ErrorResponse, "description": "路径非法或目标父目录无效"},
        409: {"model": ErrorResponse, "description": "同名文件已存在，禁止覆盖"},
        413: {"model": ErrorResponse, "description": "文件大小超出限制"},
        503: {"model": ErrorResponse, "description": "File 服务未初始化"},
    },
)
async def upload_file(
    file: UploadFile,
    dest: str | None = None,
    svc: WorkspaceFileService = Depends(_get_file_service),
) -> UploadResponse:
    safe_name = (file.filename or "upload").strip()
    if not safe_name or safe_name in (".", ".."):
        safe_name = "upload"

    try:
        rel_path, file_size, media_type = await run_in_threadpool(
            svc.upload_streaming,
            source_filename=safe_name,
            file_obj=file.file,
            path=dest,
        )
    except FileAlreadyExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except NotADirectoryError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except UploadFileTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=str(exc),
        ) from exc

    return UploadResponse(
        path=f"uploads/{rel_path.as_posix()}",
        name=Path(str(rel_path)).name,
        size=file_size,
        media_type=media_type,
        kind=FileKind.FILE,
    )


# --------------------------------------------------------------------------- GET /files


@router.get(
    "/files",
    response_model=FileListResponse,
    summary="列出 uploads/ 文件",
    description=(
        "列出 workspace/uploads/ 目录中的文件。\n\n"
        "**限制**：\n"
        "- 最多返回 max_list_entries 条（默认 1000）\n"
        "- 超出时返回 500 DirectoryTooLargeError\n"
        "- 返回相对路径，不暴露宿主机绝对路径\n"
        "- 按文件名稳定排序"
    ),
    responses={
        200: {"description": "文件列表（可能为空）"},
        500: {"model": ErrorResponse, "description": "目录条目数超限"},
        503: {"model": ErrorResponse, "description": "File 服务未初始化"},
    },
)
def list_files(
    svc: WorkspaceFileService = Depends(_get_file_service),
) -> FileListResponse:
    try:
        entries = svc.list_uploads()
    except DirectoryTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
    except WorkspaceServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

    return FileListResponse(
        entries=[
            FileMetadata(
                path=e.relative_path.as_posix(),
                name=e.name,
                size=e.size_bytes or 0,
                media_type=None,
                kind=FileKind.FILE if e.kind == "file" else FileKind.DIRECTORY,
            )
            for e in entries
        ],
        total=len(entries),
    )


# --------------------------------------------------------------------------- GET /files/{file_path}


@router.get(
    "/files/{file_path:path}",
    summary="下载 uploads/ 文件",
    description=(
        "下载 workspace/uploads/ 中的文件。\n\n"
        "**行为**：\n"
        "- Content-Type 由服务端根据扩展名推断\n"
        "- Content-Disposition 使用安全文件名（不含路径）\n"
        "- 文件不存在返回 404\n"
        "- 路径非法（../ 穿越等）返回 400"
    ),
    responses={
        200: {
            "content": {"application/octet-stream": {}},
            "description": "文件内容",
        },
        400: {"model": ErrorResponse, "description": "路径非法或目标不是文件"},
        404: {"model": ErrorResponse, "description": "文件不存在"},
        503: {"model": ErrorResponse, "description": "File 服务未初始化"},
    },
)
async def download_file(
    file_path: str,
    svc: WorkspaceFileService = Depends(_get_file_service),
) -> StreamingResponse:
    try:
        abs_path, size, media_type = svc.resolve_upload(file_path)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"文件不存在: {file_path}",
        ) from exc
    except NotAFileError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="目标不是普通文件",
        )
    except (PathViolationError, SandboxPathViolationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    safe_name = abs_path.name
    headers = {
        "Content-Disposition": f'attachment; filename="{safe_name}"',
    }

    return StreamingResponse(
        _stream_file(abs_path),
        media_type=media_type,
        headers=headers,
        status_code=status.HTTP_200_OK,
    )


async def _stream_file(path: Path, chunk_size: int = 65536):
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            yield chunk


# --------------------------------------------------------------------------- DELETE /files/{file_path}


@router.delete(
    "/files/{file_path:path}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除 uploads/ 文件",
    description=(
        "删除 workspace/uploads/ 中的文件。\n\n"
        "**行为**：\n"
        "- 成功返回 204 无响应体\n"
        "- 文件不存在返回 404\n"
        "- 不能删除目录（目录返回 400）"
    ),
    responses={
        204: {"description": "删除成功"},
        400: {"model": ErrorResponse, "description": "目标不是文件（目录）"},
        404: {"model": ErrorResponse, "description": "文件不存在"},
        503: {"model": ErrorResponse, "description": "File 服务未初始化"},
    },
)
def delete_file(
    file_path: str,
    svc: WorkspaceFileService = Depends(_get_file_service),
) -> None:
    try:
        svc.delete_upload(file_path)
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"文件不存在: {file_path}",
        )
    except NotAFileError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不能删除目录",
        )
    except (PathViolationError, SandboxPathViolationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


# --------------------------------------------------------------------------- GET /artifacts


@router.get(
    "/artifacts",
    response_model=FileListResponse,
    summary="列出 artifacts/ 产物（只读）",
    description=(
        "列出 workspace/artifacts/ 目录中的 Agent 产物。\n\n"
        "**只读**：此接口仅供读取，不提供删除路由。\n"
        "产物由 Agent 执行过程中写入，不通过 API 上传。"
    ),
    responses={
        200: {"description": "产物列表（可能为空）"},
        500: {"model": ErrorResponse, "description": "目录条目数超限"},
        503: {"model": ErrorResponse, "description": "File 服务未初始化"},
    },
)
def list_artifacts(
    svc: WorkspaceFileService = Depends(_get_file_service),
) -> FileListResponse:
    try:
        entries = svc.list_artifacts()
    except DirectoryTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
    except WorkspaceServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

    return FileListResponse(
        entries=[
            FileMetadata(
                path=e.relative_path.as_posix(),
                name=e.name,
                size=e.size_bytes or 0,
                media_type=None,
                kind=FileKind.FILE if e.kind == "file" else FileKind.DIRECTORY,
            )
            for e in entries
        ],
        total=len(entries),
    )


# --------------------------------------------------------------------------- GET /artifacts/{path}


@router.get(
    "/artifacts/{artifact_path:path}",
    summary="下载 Artifact（只读）",
    description=(
        "下载 workspace/artifacts/ 中的 Agent 产物。\n\n"
        "**只读**：此接口仅供读取，不提供删除路由。"
    ),
    responses={
        200: {
            "content": {"application/octet-stream": {}},
            "description": "Artifact 内容",
        },
        400: {"model": ErrorResponse, "description": "路径非法或目标不是文件"},
        404: {"model": ErrorResponse, "description": "Artifact 不存在"},
        503: {"model": ErrorResponse, "description": "File 服务未初始化"},
    },
)
async def download_artifact(
    artifact_path: str,
    svc: WorkspaceFileService = Depends(_get_file_service),
) -> StreamingResponse:
    try:
        abs_path, size, media_type = svc.resolve_artifact(artifact_path)
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Artifact 不存在: {artifact_path}",
        )
    except NotAFileError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="目标不是普通文件",
        )
    except (PathViolationError, SandboxPathViolationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    safe_name = abs_path.name
    headers = {
        "Content-Disposition": f'attachment; filename="{safe_name}"',
    }

    return StreamingResponse(
        _stream_file(abs_path),
        media_type=media_type,
        headers=headers,
        status_code=status.HTTP_200_OK,
    )
