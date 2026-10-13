"""Project REST 路由（托管工作区方案）。

端点：
- POST   /projects                      创建项目（201）
- GET    /projects                      列表（200）
- GET    /projects/{project_id}         详情（200，404）
- DELETE /projects/{project_id}         删除（204）
- GET    /projects/{project_id}/workspace/browse   文件树（200）
- GET    /projects/{project_id}/workspace/file     文件内容（200）
- GET    /projects/{project_id}/workspace/download 下载 ZIP（200）
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status

from mini_deerflow.api.dependencies import get_runtime
from mini_deerflow.api.schemas.projects import (
    BrowseResponse,
    CreateProjectRequest,
    FileContentResponse,
    ProjectListResponse,
    ProjectSummary,
    project_to_summary,
)
from mini_deerflow.api.schemas.threads import ThreadListResponse
from mini_deerflow.api.services.projects import (
    ProjectBusyError,
    ProjectNotFoundError,
    ProjectPathError,
    ProjectService,
    ProjectServiceError,
)
from mini_deerflow.runtime import RuntimeContext

router = APIRouter(tags=["projects"])


def get_project_service(runtime: RuntimeContext = Depends(get_runtime)) -> ProjectService:
    """构造 ProjectService，传入 runtime.project_store, workspaces_root, runtime.store。

    共享锁字典与 Agent 工具层（project_execute）共用同一实例，
    保证执行期间 API 删除返回 409，同项目并发执行被互斥。"""
    from mini_deerflow.runtime import _PROJECT_LOCKS, _PROJECT_LOCKS_LOCK
    ws_root = getattr(runtime, "_workspaces_root", None)
    return ProjectService(
        runtime.project_store,
        str(ws_root) if ws_root else str(Path.home() / ".mini-deerflow" / "workspaces"),
        runtime.store,
        getattr(runtime, "context_assembler", None),
        shared_locks=_PROJECT_LOCKS,
        shared_locks_lock=_PROJECT_LOCKS_LOCK,
    )


@router.post(
    "/projects",
    response_model=ProjectSummary,
    status_code=status.HTTP_201_CREATED,
    summary="创建项目",
    description="后端自动创建托管工作区目录，只接收项目名。",
    responses={
        201: {"description": "项目创建成功"},
        400: {"description": "创建失败"},
        409: {"description": "项目已存在"},
    },
)
def create_project(
    payload: CreateProjectRequest,
    service: ProjectService = Depends(get_project_service),
) -> ProjectSummary:
    try:
        project = service.create(payload.name)
    except ProjectServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except Exception as exc:
        # StoreAlreadyExistsError 等 store 层异常
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    return project_to_summary(
        project.project_id,
        project.name,
        str(project.workspace_path) if project.workspace_path else None,
        str(project.created_at),
        project.legacy,
    )


@router.get(
    "/projects",
    response_model=ProjectListResponse,
    summary="列出项目",
    description="返回全部项目的摘要列表。",
    responses={
        200: {"description": "项目列表（可能为空）"},
    },
)
def list_projects(
    service: ProjectService = Depends(get_project_service),
) -> ProjectListResponse:
    return ProjectListResponse(
        projects=[
            project_to_summary(
                p.project_id,
                p.name,
                str(p.workspace_path) if p.workspace_path else None,
                str(p.created_at),
                p.legacy,
            )
            for p in service.list()
        ]
    )


@router.get(
    "/projects/{project_id}",
    response_model=ProjectSummary,
    summary="读取项目详情",
    responses={
        200: {"description": "项目详情"},
        404: {"description": "项目不存在"},
    },
)
def get_project(
    project_id: str,
    service: ProjectService = Depends(get_project_service),
) -> ProjectSummary:
    try:
        project = service.get(project_id)
    except ProjectNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    return project_to_summary(
        project.project_id,
        project.name,
        str(project.workspace_path) if project.workspace_path else None,
        str(project.created_at),
        project.legacy,
    )


@router.delete(
    "/projects/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="删除项目",
    description="删除项目并级联删除工作区目录。成功返回 204 无响应体。",
    responses={
        204: {"description": "删除成功"},
        404: {"description": "项目不存在"},
        409: {"description": "项目正在运行，无法删除"},
    },
)
def delete_project(
    project_id: str,
    service: ProjectService = Depends(get_project_service),
) -> Response:
    try:
        service.delete(project_id)
    except ProjectNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except ProjectBusyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="项目正在运行，无法删除"
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/projects/{project_id}/workspace/browse",
    response_model=BrowseResponse,
    summary="浏览工作区文件树",
    responses={
        200: {"description": "目录内容"},
        400: {"description": "路径非法或项目尚未迁移"},
        404: {"description": "项目不存在"},
    },
)
def browse_workspace(
    project_id: str,
    path: str | None = Query(default=None, description="要浏览的子目录路径"),
    service: ProjectService = Depends(get_project_service),
) -> BrowseResponse:
    try:
        result = service.browse_workspace(project_id, path)
    except ProjectNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except ProjectPathError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except ProjectServiceError as exc:
        # "项目尚未迁移" 等场景
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return BrowseResponse(**result)


@router.get(
    "/projects/{project_id}/workspace/file",
    response_model=FileContentResponse,
    summary="读取文件内容",
    responses={
        200: {"description": "文件内容"},
        400: {"description": "路径非法或项目尚未迁移"},
        404: {"description": "项目不存在"},
    },
)
def get_workspace_file(
    project_id: str,
    path: str = Query(..., description="文件相对路径（必填）"),
    service: ProjectService = Depends(get_project_service),
) -> FileContentResponse:
    try:
        result = service.get_file_content(project_id, path)
    except ProjectNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except ProjectPathError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except ProjectServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return FileContentResponse(**result)


@router.get(
    "/projects/{project_id}/workspace/download",
    summary="下载工作区 ZIP",
    responses={
        200: {
            "description": "ZIP 文件",
            "content": {"application/zip": {}},
        },
        400: {"description": "项目尚未迁移"},
        404: {"description": "项目不存在"},
    },
)
def download_workspace_zip(
    project_id: str,
    service: ProjectService = Depends(get_project_service),
) -> Response:
    try:
        project = service.get(project_id)
    except ProjectNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    try:
        zip_bytes = service.download_workspace_zip(project_id)
    except ProjectServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={project.name}.zip"},
    )


@router.get(
    "/projects/{project_id}/threads",
    response_model=ThreadListResponse,
    summary="列出项目内线程",
    description="返回属于指定项目的所有线程。",
    responses={
        200: {"description": "线程列表（可能为空）"},
        404: {"description": "项目不存在"},
    },
)
def list_project_threads(
    project_id: str,
    service: ProjectService = Depends(get_project_service),
) -> ThreadListResponse:
    try:
        threads = service.list_project_threads(project_id)
    except ProjectNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    from mini_deerflow.api.schemas.threads import thread_to_summary

    return ThreadListResponse(
        threads=[thread_to_summary(t.thread_id) for t in threads],
    )
