"""Project 数据模型 / Store / Service / API 测试（Phase 1 改动适配）。

Phase 1 改动：
- Project 新字段：project_root, workspace_path, legacy, legacy_repo_path
- Project.repo_path 字段已删除
- PathGate 类已删除
- browse() 改为 browse_workspace()
- ProjectRepoAlreadyBoundError 已废弃（store 层保留但不使用）
- create_runtime() 新增 workspaces_root 参数
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from mini_deerflow.api.app import create_app
from mini_deerflow.api.config import ApiSettings
from mini_deerflow.api.services.projects import (
    ProjectBusyError,
    ProjectNotFoundError,
    ProjectPathError,
    ProjectService,
)
from mini_deerflow.api.services.threads import ThreadService, UnknownProjectError
from mini_deerflow.project_state import Project
from mini_deerflow.project_store import (
    InMemoryProjectStore,
    ProjectAlreadyExistsError,
    SQLiteProjectStore,
)
from mini_deerflow.runtime import RuntimeConfig, create_runtime
from mini_deerflow.thread_state import ThreadState
from mini_deerflow.thread_store import InMemoryThreadStore, SQLiteThreadStore
from mini_deerflow.tools import ToolRegistry


# =============================================================================
# 工具
# =============================================================================


def _store_candidates(tmp_path):
    """把同一组断言套在内存与 SQLite 两种实现上。"""
    return [
        InMemoryProjectStore(),
        SQLiteProjectStore(str(tmp_path / "p.db")),
    ]


# =============================================================================
# Project 数据模型
# =============================================================================


class TestProjectState:
    def test_test_round_trip(self):
        """新项目：project_id + name，不带 repo_path。"""
        project = Project(project_id="p1", name="demo")
        restored = Project.from_dict(project.to_dict())
        assert restored == project
        assert restored.project_root is None
        assert restored.workspace_path is None
        assert restored.legacy is False
        assert restored.legacy_repo_path is None

    def test_test_round_trip_full(self):
        """带完整字段的新项目。"""
        project = Project(
            project_id="p1",
            name="demo",
            project_root=Path("/tmp/p1"),
            workspace_path=Path("/tmp/p1/workspace"),
            legacy=False,
            legacy_repo_path=None,
        )
        restored = Project.from_dict(project.to_dict())
        assert restored == project

    def test_test_created_at(self):
        project = Project(project_id="p1", name="demo")
        assert project.created_at

    def test_test_validation(self):
        with pytest.raises(ValueError):
            Project(project_id="", name="demo")
        with pytest.raises(ValueError):
            Project(project_id="p1", name="")
        with pytest.raises(ValueError):
            Project(project_id="p1", name="  ")

    def test_test_schema_version(self):
        """schema_version 必须是 2。"""
        project = Project(project_id="p1", name="demo")
        data = project.to_dict()
        data["schema_version"] = 999
        with pytest.raises(ValueError, match="未知 schema_version"):
            Project.from_dict(data)

    def test_test_bool_version(self):
        project = Project(project_id="p1", name="demo")
        data = project.to_dict()
        data["schema_version"] = True
        with pytest.raises(ValueError, match="必须是整数"):
            Project.from_dict(data)

    def test_test_legacy_from_v1(self):
        """schema_version=1 只有 repo_path，迁移后 legacy=True。"""
        legacy_data = {
            "schema_version": 1,
            "project_id": "old1",
            "name": "old",
            "repo_path": "/tmp/old-repo",
        }
        p = Project.from_dict(legacy_data)
        assert p.legacy is True
        assert p.legacy_repo_path == "/tmp/old-repo"
        assert p.project_root is None
        assert p.workspace_path is None

    def test_test_to_dict_omits_none_fields(self):
        """None 字段不应出现在 to_dict() 中。"""
        project = Project(project_id="p1", name="demo")
        d = project.to_dict()
        assert "project_root" not in d
        assert "workspace_path" not in d
        assert "legacy_repo_path" not in d
        assert d["legacy"] is False


# =============================================================================
# ProjectStore（两种实现跑同一组断言）
# =============================================================================


class TestProjectStore:
    def test_test_create_get_delete(self, tmp_path):
        for store in _store_candidates(tmp_path):
            created = store.create("p1", "demo")
            assert created.project_id == "p1"
            assert created.name == "demo"
            assert created.workspace_path is None  # store 层不设 workspace_path
            assert store.exists("p1")
            assert store.get("p1").name == "demo"
            store.delete("p1")
            assert not store.exists("p1")

    def test_test_id_AlreadyExists(self, tmp_path):
        for store in _store_candidates(tmp_path):
            store.create("p1", "demo")
            with pytest.raises(ProjectAlreadyExistsError):
                store.create("p1", "other")

    def test_test_find_by_repo_path(self, tmp_path):
        """legacy_repo_path 查询。"""
        for store in _store_candidates(tmp_path):
            store.create("p1", "demo", legacy_repo_path="/tmp/demo")
            found = store.find_by_repo_path("/tmp/demo")
            assert found is not None
            assert found.project_id == "p1"
            assert store.find_by_repo_path("/tmp/other") is None

    def test_test_find_by_repo_path_empty(self, tmp_path):
        """空路径返回 None。"""
        store = InMemoryProjectStore()
        assert store.find_by_repo_path("") is None
        assert store.find_by_repo_path(None) is None  # type: ignore

    def test_test_id_NotFound(self, tmp_path):
        from mini_deerflow.project_store import ProjectNotFoundError as StoreNotFound

        for store in _store_candidates(tmp_path):
            with pytest.raises(StoreNotFound):
                store.get("nope")
            with pytest.raises(StoreNotFound):
                store.delete("nope")

    def test_test_list_id(self, tmp_path):
        for store in _store_candidates(tmp_path):
            store.create("p2", "b")
            store.create("p1", "a")
            assert [p.project_id for p in store.list_projects()] == ["p1", "p2"]

    def test_test_save(self, tmp_path):
        for store in _store_candidates(tmp_path):
            project = store.create("p1", "demo")
            project.name = "renamed"
            store.save(project)
            assert store.get("p1").name == "renamed"

    def test_test_sqlite(self, tmp_path):
        path = str(tmp_path / "restart.db")
        with SQLiteProjectStore(path) as store:
            store.create("p1", "demo", legacy_repo_path="/tmp/demo")
        with SQLiteProjectStore(path) as reopened:
            p = reopened.get("p1")
            assert p.name == "demo"
            assert p.legacy_repo_path == "/tmp/demo"


# =============================================================================
# ProjectService
# =============================================================================


def _service(tmp_path, project_store=None, thread_store=None):
    return ProjectService(
        project_store or InMemoryProjectStore(),
        str(Path(tmp_path) / "workspaces"),
        thread_store or InMemoryThreadStore(),
    )


class TestProjectService:
    def test_create_name(self, tmp_path):
        """创建项目：name 来自参数。"""
        service = _service(tmp_path)
        project = service.create("我的项目")
        assert project.name == "我的项目"
        assert project.project_id
        assert project.project_root is not None
        assert project.workspace_path is not None
        assert project.project_root.exists()
        assert project.workspace_path.exists()

    def test_create_project_root_created(self, tmp_path):
        """创建项目后 project_root 和 workspace_path 目录存在。"""
        service = _service(tmp_path)
        project = service.create("test")
        assert project.project_root.exists()
        assert project.workspace_path.exists()

    def test_create_git_init(self, tmp_path):
        """workspace 目录已初始化 Git 仓库。"""
        service = _service(tmp_path)
        project = service.create("git-test")
        assert (project.workspace_path / ".git").is_dir()

    def test_create_duplicate_id(self, tmp_path):
        """不同 project_id 不冲突。"""
        service = _service(tmp_path)
        p1 = service.create("p1")
        p2 = service.create("p2")
        assert p2.project_id != p1.project_id

    def test_create_in_workspaces_root(self, tmp_path):
        """项目目录在 workspaces_root 下。"""
        service = _service(tmp_path)
        project = service.create("ws-test")
        ws_root = Path(tmp_path) / "workspaces"
        assert project.project_root.parent == ws_root
        assert project.project_root.name == project.project_id

    def test_delete_project_deletes_root(self, tmp_path):
        """删除项目后 project_root 目录被删除。"""
        service = _service(tmp_path)
        project = service.create("del-test")
        root = project.project_root
        assert root.exists()
        service.delete(project.project_id)
        assert not root.exists()

    def test_delete_project_not_found(self, tmp_path):
        """删除不存在的项目抛出 ProjectNotFoundError。"""
        with pytest.raises(ProjectNotFoundError):
            _service(tmp_path).delete("nope")

    def test_delete_project_busy(self, tmp_path):
        """项目被锁时无法删除。"""
        service = _service(tmp_path)
        project = service.create("busy-test")
        service.acquire_project_lock(project.project_id)
        with pytest.raises(ProjectBusyError):
            service.delete(project.project_id)
        service.release_project_lock(project.project_id)

    def test_workspace_boundary_check_absolute(self, tmp_path):
        """绝对路径拒绝。"""
        service = _service(tmp_path)
        project = service.create("boundary-test")
        ws = project.workspace_path
        with pytest.raises(ProjectPathError, match="不接受绝对路径"):
            service.workspace_boundary_check("C:/Windows/win.ini", ws)
        with pytest.raises(ProjectPathError, match="不接受绝对路径"):
            service.workspace_boundary_check("/etc/passwd", ws)

    def test_workspace_boundary_check_normal(self, tmp_path):
        """正常相对路径通过。"""
        service = _service(tmp_path)
        project = service.create("boundary-test2")
        ws = project.workspace_path
        (ws / "src").mkdir()
        (ws / "src" / "a.py").write_text("x", encoding="utf-8")
        result = service.workspace_boundary_check("src/a.py", ws)
        assert result.name == "a.py"

    def test_browse_workspace(self, tmp_path):
        """浏览工作区根目录。"""
        service = _service(tmp_path)
        project = service.create("browse-test")
        (project.workspace_path / "a.py").write_text("x", encoding="utf-8")
        (project.workspace_path / "sub").mkdir()
        result = service.browse_workspace(project.project_id)
        assert result["is_root"] is True
        names = [e["name"] for e in result["entries"]]
        assert "a.py" in names
        assert "sub" in names

    def test_browse_workspace_subdir(self, tmp_path):
        """浏览子目录。"""
        service = _service(tmp_path)
        project = service.create("browse-subdir")
        sub = project.workspace_path / "sub"
        sub.mkdir()
        (sub / "b.py").write_text("y", encoding="utf-8")
        result = service.browse_workspace(project.project_id, "sub")
        assert result["is_root"] is False
        names = [e["name"] for e in result["entries"]]
        assert "b.py" in names

    def test_browse_workspace_not_found(self, tmp_path):
        """项目不存在时抛出。"""
        with pytest.raises(ProjectNotFoundError):
            _service(tmp_path).browse_workspace("nope")

    def test_browse_workspace_entries_sorted(self, tmp_path):
        """条目按目录优先、名字排序。"""
        service = _service(tmp_path)
        project = service.create("browse-sort")
        (project.workspace_path / "z_dir").mkdir()
        (project.workspace_path / "a.py").write_text("x", encoding="utf-8")
        (project.workspace_path / "b_dir").mkdir()
        result = service.browse_workspace(project.project_id)
        names = [e["name"] for e in result["entries"]]
        # .git 目录来自 git init；目录优先于文件，名字升序
        assert names == [".git", "b_dir", "z_dir", "a.py"]

    def test_get_project(self, tmp_path):
        """通过 service.get 读取项目。"""
        service = _service(tmp_path)
        created = service.create("get-test")
        found = service.get(created.project_id)
        assert found.project_id == created.project_id

    def test_get_project_not_found(self, tmp_path):
        with pytest.raises(ProjectNotFoundError):
            _service(tmp_path).get("nope")

    def test_list_projects(self, tmp_path):
        """列出所有项目。"""
        service = _service(tmp_path)
        p1 = service.create("list-1")
        p2 = service.create("list-2")
        ids = {p.project_id for p in service.list()}
        assert p1.project_id in ids
        assert p2.project_id in ids


# =============================================================================
# HTTP API（通过依赖注入覆盖 get_project_service）
# =============================================================================


class _FakeSettings:
    """模拟 ApiSettings：只暴露 ProjectService 路由需要的字段。"""

    def __init__(self, workspaces_root: str) -> None:
        self.workspaces_root = workspaces_root


class _FakeRuntime:
    """模拟 RuntimeContext：提供 ProjectService 路由所需的属性。"""

    def __init__(
        self,
        project_store,
        workspaces_root: str,
        thread_store,
    ) -> None:
        self.project_store = project_store
        self._workspaces_root = workspaces_root
        self.store = thread_store
        # get_project_service 访问 runtime.settings
        self.settings = _FakeSettings(workspaces_root)

    def shutdown(self) -> None:
        """空实现，TestClient teardown 时调用。"""
        pass


@pytest.fixture
def api_client(tmp_path):
    """TestClient + 共享 store；workspaces_root = tmp_path/workspaces。"""
    ws_root = tmp_path / "workspaces"
    ws_root.mkdir()
    thread_store = InMemoryThreadStore()
    project_store = InMemoryProjectStore()
    runtime = _FakeRuntime(project_store, str(ws_root), thread_store)
    app = create_app(
        ApiSettings(api_prefix="/api"),
        runtime_factory=lambda: runtime,
    )
    with TestClient(app) as client:
        client.store = thread_store  # type: ignore[attr-defined]
        client.projects = project_store  # type: ignore[attr-defined]
        client.ws_root = ws_root  # type: ignore[attr-defined]
        yield client


class TestProjectsApi:
    def test_create_201(self, api_client):
        """创建项目返回 201。"""
        res = api_client.post("/api/projects", json={"name": "demo"})
        assert res.status_code == 201
        body = res.json()
        assert body["name"] == "demo"
        assert "project_id" in body
        assert body["workspace_path"] is not None

    def test_create_400_empty_name(self, api_client):
        """空名字返回 422。"""
        res = api_client.post("/api/projects", json={"name": ""})
        assert res.status_code == 422

    def test_create_400_whitespace_name(self, api_client):
        """空白名字由 schema 层拒绝。"""
        res = api_client.post("/api/projects", json={"name": "   "})
        assert res.status_code == 422

    def test_create_sets_legacy_false(self, api_client):
        """新项目 legacy=False。"""
        res = api_client.post("/api/projects", json={"name": "legacy-test"})
        assert res.json()["legacy"] is False

    def test_get_project(self, api_client):
        """读取项目详情。"""
        res = api_client.post("/api/projects", json={"name": "test"})
        pid = res.json()["project_id"]
        body = api_client.get(f"/api/projects/{pid}").json()
        assert body["project_id"] == pid
        assert body["name"] == "test"
        assert body["workspace_path"] is not None

    def test_get_project_404(self, api_client):
        """项目不存在返回 404。"""
        assert api_client.get("/api/projects/nope").status_code == 404

    def test_list_projects(self, api_client):
        """列出项目列表。"""
        api_client.post("/api/projects", json={"name": "a"})
        api_client.post("/api/projects", json={"name": "b"})
        body = api_client.get("/api/projects").json()
        assert len(body["projects"]) == 2

    def test_list_projects_empty(self, api_client):
        """空列表返回空数组。"""
        body = api_client.get("/api/projects").json()
        assert body["projects"] == []

    def test_delete_project_204(self, api_client):
        """删除项目返回 204。"""
        res = api_client.post("/api/projects", json={"name": "del"})
        pid = res.json()["project_id"]
        assert api_client.delete(f"/api/projects/{pid}").status_code == 204
        assert api_client.get(f"/api/projects/{pid}").status_code == 404

    def test_delete_project_404(self, api_client):
        assert api_client.delete("/api/projects/nope").status_code == 404

    def test_browse_workspace(self, api_client):
        """浏览工作区文件树。"""
        res = api_client.post("/api/projects", json={"name": "ws"})
        pid = res.json()["project_id"]
        project = api_client.projects.get(pid)
        (project.workspace_path / "x.py").write_text("y", encoding="utf-8")
        (project.workspace_path / "sub").mkdir()
        browse = api_client.get(f"/api/projects/{pid}/workspace/browse")
        assert browse.status_code == 200
        result = browse.json()
        assert result["is_root"] is True
        names = [e["name"] for e in result["entries"]]
        assert "x.py" in names
        assert "sub" in names

    def test_browse_workspace_subpath(self, api_client):
        """浏览子目录。"""
        res = api_client.post("/api/projects", json={"name": "ws2"})
        pid = res.json()["project_id"]
        project = api_client.projects.get(pid)
        (project.workspace_path / "sub" / "y.py").parent.mkdir()
        (project.workspace_path / "sub" / "y.py").write_text("z", encoding="utf-8")
        browse = api_client.get(
            f"/api/projects/{pid}/workspace/browse", params={"path": "sub"}
        )
        assert browse.status_code == 200
        names = [e["name"] for e in browse.json()["entries"]]
        assert "y.py" in names

    def test_browse_workspace_404(self, api_client):
        """项目不存在时返回 404。"""
        assert (
            api_client.get("/api/projects/nope/workspace/browse").status_code == 404
        )

    def test_browse_workspace_400_invalid_path(self, api_client):
        """绝对路径返回 400。"""
        res = api_client.post("/api/projects", json={"name": "ws3"})
        pid = res.json()["project_id"]
        browse = api_client.get(
            f"/api/projects/{pid}/workspace/browse",
            params={"path": "C:/Windows/win.ini"},
        )
        assert browse.status_code == 400

    def test_delete_project_deletes_root_directory(self, api_client):
        """删除项目时工作区目录被物理删除。"""
        res = api_client.post("/api/projects", json={"name": "物理删除"})
        pid = res.json()["project_id"]
        project = api_client.projects.get(pid)
        root = project.project_root
        assert root.exists()
        api_client.delete(f"/api/projects/{pid}")
        assert not root.exists()
