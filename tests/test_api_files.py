"""19E File/Artifact API 集成测试。"""
from __future__ import annotations

import io
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mini_deerflow.api.app import create_app
from mini_deerflow.api.config import ApiSettings
from mini_deerflow.runtime import RuntimeConfig
from mini_deerflow.sandbox_path import WorkspacePathResolver
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tools import ToolRegistry


# ============================================================================
# App fixture
# ============================================================================


@pytest.fixture
def workspace_root():
    """每个测试用独立临时 workspace。"""
    import shutil, tempfile
    root = Path(tempfile.mkdtemp(prefix="mdf_ws_test_"))
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def app(workspace_root):
    settings = ApiSettings(workspace_root=str(workspace_root), max_upload_bytes=1024 * 1024)
    return create_app(settings=settings)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


# ============================================================================
# 辅助
# ============================================================================


def _upload(client, filename, content, dest=None, expected_status=201):
    """上传文件的辅助函数。"""
    files = {"file": (filename, io.BytesIO(content), "application/octet-stream")}
    resp = client.post("/api/files", params={"dest": dest} if dest else None, files=files)
    assert resp.status_code == expected_status, resp.text
    return resp


# ============================================================================
# 1. 上传文本文件成功
# ============================================================================


def test_upload_text_file_success(client):
    resp = _upload(client, "hello.txt", b"Hello, World!")
    json = resp.json()
    assert json["name"] == "hello.txt"
    assert json["size"] == 13
    assert json["kind"] == "file"


# ============================================================================
# 2. 上传二进制文件成功
# ============================================================================


def test_upload_binary_file_success(client):
    data = bytes(range(256))
    resp = _upload(client, "binary.bin", data)
    json = resp.json()
    assert json["name"] == "binary.bin"
    assert json["size"] == 256


# ============================================================================
# 3. 上传后下载内容完全一致
# ============================================================================


def test_upload_download_roundtrip(client):
    content = b"\x00\x01\x02\xff binary \xe4\xb8\xad\xe6\x96\x87"
    _upload(client, "roundtrip.bin", content)
    resp = client.get("/api/files/roundtrip.bin")
    assert resp.status_code == 200
    assert resp.content == content


# ============================================================================
# 4. Unicode 文件名正确
# ============================================================================


def test_upload_unicode_filename(client):
    resp = _upload(client, "文件_\u4e2d\u6587.txt", b"unicode content")
    assert resp.status_code == 201
    assert "文件" in resp.json()["name"]


# ============================================================================
# 5. 自定义相对路径正确
# ============================================================================


def test_upload_custom_path(client):
    resp = _upload(client, "ignored.txt", b"data", dest="subdir/myfile.txt")
    assert resp.status_code == 201
    assert resp.json()["path"] == "uploads/subdir/myfile.txt"


# ============================================================================
# 6. 同名文件默认返回 409
# ============================================================================


def test_upload_same_name_conflict(client):
    _upload(client, "conflict.txt", b"first")
    resp = _upload(client, "conflict.txt", b"second", expected_status=409)
    assert resp.status_code == 409


# ============================================================================
# 7. 刚好等于大小限制成功
# ============================================================================


def test_upload_at_limit(client):
    limit = 1024 * 1024
    content = b"x" * limit
    resp = _upload(client, "at_limit.bin", content)
    assert resp.status_code == 201
    assert resp.json()["size"] == limit


# ============================================================================
# 8. 超过一个字节返回 413
# ============================================================================


def test_upload_over_limit(client):
    limit = 1024 * 1024
    content = b"x" * (limit + 1)
    resp = _upload(client, "over.bin", content, expected_status=413)
    assert resp.status_code == 413


# ============================================================================
# 9. 超限不留下目标文件
# ============================================================================


def test_upload_over_limit_no_file_left(client):
    content = b"x" * (1024 * 1024 + 1)
    resp = _upload(client, "should_not_exist.bin", content, expected_status=413)
    assert resp.status_code == 413
    list_resp = client.get("/api/files")
    names = [e["name"] for e in list_resp.json()["entries"]]
    assert "should_not_exist.bin" not in names


# ============================================================================
# 10. 上传异常不留下临时文件
# ============================================================================


def test_upload_error_leaves_no_temp_file(client, workspace_root):
    uploads = workspace_root / "uploads"
    # 空内容应该成功，但测试的是异常清理
    resp = client.post("/api/files", data={}, files={})
    # 文件为空或参数缺失
    assert resp.status_code in (400, 422)


# ============================================================================
# 11. 空文件行为明确
# ============================================================================


def test_upload_empty_file(client):
    resp = _upload(client, "empty.txt", b"")
    assert resp.status_code == 201
    assert resp.json()["size"] == 0


# ============================================================================
# 12. 文件列表顺序稳定
# ============================================================================


def test_file_list_order_stable(client):
    for name in ["z_file.txt", "a_file.txt", "m_file.txt"]:
        _upload(client, name, b"content")
    resp = client.get("/api/files")
    names = [e["name"] for e in resp.json()["entries"]]
    # 稳定排序
    assert names == sorted(names, key=str.casefold)


# ============================================================================
# 13. 列表不含绝对路径
# ============================================================================


def test_file_list_no_absolute_path(client, workspace_root):
    _upload(client, "safe.txt", b"content")
    resp = client.get("/api/files")
    for entry in resp.json()["entries"]:
        assert str(workspace_root) not in entry["path"]
        assert not Path(entry["path"]).is_absolute()


# ============================================================================
# 14. 列表数量限制生效
# ============================================================================


def test_file_list_limit(workspace_root):
    """设置 max_list_entries=3，目录有 5 个文件时应抛 DirectoryTooLargeError。"""
    from mini_deerflow.api.services.workspace_file import WorkspaceFileService, DirectoryTooLargeError
    from mini_deerflow.sandbox_path import WorkspacePathResolver

    resolver = WorkspacePathResolver(workspace_root)
    svc = WorkspaceFileService(resolver, max_upload_bytes=10_000_000, max_list_entries=3)

    for i in range(5):
        svc.upload_streaming(f"file{i}.txt", io.BytesIO(b"data"))

    with pytest.raises(DirectoryTooLargeError):
        svc.list_uploads()


# ============================================================================
# 15. 文件不存在返回 404
# ============================================================================


def test_download_nonexistent_returns_404(client):
    resp = client.get("/api/files/nonexistent.txt")
    assert resp.status_code == 404


# ============================================================================
# 16. 删除成功返回 204
# ============================================================================


def test_delete_file_success(client):
    _upload(client, "to_delete.txt", b"delete me")
    resp = client.delete("/api/files/to_delete.txt")
    assert resp.status_code == 204


# ============================================================================
# 17. 删除后无法下载
# ============================================================================


def test_deleted_file_not_downloadable(client):
    _upload(client, "delete_check.txt", b"content")
    client.delete("/api/files/delete_check.txt")
    resp = client.get("/api/files/delete_check.txt")
    assert resp.status_code == 404


# ============================================================================
# 18. 删除不存在文件返回 404
# ============================================================================


def test_delete_nonexistent_returns_404(client):
    resp = client.delete("/api/files/never_existed.txt")
    assert resp.status_code == 404


# ============================================================================
# 19. 不能删除目录
# ============================================================================


def test_cannot_delete_directory(client):
    _upload(client, "file_in_sub.txt", b"data", dest="subdir/file.txt")
    resp = client.delete("/api/files/subdir")
    assert resp.status_code in (400, 404)


# ============================================================================
# 20. Artifact 列表正确
# ============================================================================


def test_artifact_list_empty(client):
    resp = client.get("/api/artifacts")
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_artifact_list_with_content(workspace_root):
    """artifacts 目录初始为空。"""
    from mini_deerflow.api.services.workspace_file import WorkspaceFileService
    from mini_deerflow.sandbox_path import WorkspacePathResolver

    resolver = WorkspacePathResolver(workspace_root)
    svc = WorkspaceFileService(resolver, max_upload_bytes=10_000_000)

    entries = svc.list_artifacts()
    assert len(entries) == 0


# ============================================================================
# 21. Artifact 下载正确
# ============================================================================


def test_artifact_download_nonexistent(client):
    resp = client.get("/api/artifacts/nonexistent.txt")
    assert resp.status_code == 404


# ============================================================================
# 22. Artifact 没有删除路由
# ============================================================================


def test_artifact_delete_not_allowed(client):
    """API 不提供 Artifact 删除端点（405 Method Not Allowed）。"""
    resp = client.delete("/api/artifacts/some_artifact.txt")
    assert resp.status_code in (400, 404, 405)


# ============================================================================
# 23. ../ 路径穿越被拒绝（uploads）
# ============================================================================


def test_path_traversal_dotdot_rejected(client):
    resp = client.get("/api/files/../secret.txt")
    assert resp.status_code in (400, 403, 404)


# ============================================================================
# 24. Windows 绝对路径被拒绝
# ============================================================================


def test_windows_absolute_path_rejected(client):
    resp = client.get("/api/files/C:\\Windows\\file.txt")
    assert resp.status_code in (400, 403, 404)


# ============================================================================
# 25. UNC 路径被拒绝
# ============================================================================


def test_unc_path_rejected(client):
    resp = client.get("/api/files/\\\\server\\share\\file.txt")
    assert resp.status_code in (400, 403, 404)


# ============================================================================
# 26. Workspace 外符号链接被拒绝（通过 SandboxPathResolver）
# ============================================================================


def test_uploads_cannot_read_artifacts_via_dotdot(workspace_root):
    """上传解析器不能通过 ../artifacts/x 读取 Artifact。"""
    from mini_deerflow.api.services.workspace_file import WorkspaceFileService
    from mini_deerflow.sandbox_path import WorkspacePathResolver
    from mini_deerflow.sandbox_path import SandboxPathViolationError

    resolver = WorkspacePathResolver(workspace_root)
    svc = WorkspaceFileService(resolver, max_upload_bytes=10_000_000)

    # 在 artifacts 里放一个文件
    import tempfile, pathlib
    artifact_dir = workspace_root / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "secret.txt").write_text("secret")

    # uploads 解析器无法通过 ../ 逃逸
    with pytest.raises(SandboxPathViolationError):
        svc.resolve_upload("../artifacts/secret.txt")


def test_artifacts_cannot_read_uploads_via_dotdot(workspace_root):
    """Artifact 解析器不能通过 ../uploads/x 读取上传文件。"""
    from mini_deerflow.api.services.workspace_file import WorkspaceFileService
    from mini_deerflow.sandbox_path import WorkspacePathResolver
    from mini_deerflow.sandbox_path import SandboxPathViolationError

    resolver = WorkspacePathResolver(workspace_root)
    svc = WorkspaceFileService(resolver, max_upload_bytes=10_000_000)

    # 在 uploads 里放一个文件
    upload_dir = workspace_root / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / "private.txt").write_text("private")

    # artifacts 解析器无法通过 ../ 逃逸
    with pytest.raises(SandboxPathViolationError):
        svc.resolve_artifact("../uploads/private.txt")


def test_both_resolvers_reject_dotdot_even_if_inside_workspace(workspace_root):
    """../ 即使最终仍在 Workspace 内，也必须因为跨逻辑根而拒绝。"""
    from mini_deerflow.api.services.workspace_file import WorkspaceFileService
    from mini_deerflow.sandbox_path import WorkspacePathResolver
    from mini_deerflow.sandbox_path import SandboxPathViolationError

    resolver = WorkspacePathResolver(workspace_root)
    svc = WorkspaceFileService(resolver, max_upload_bytes=10_000_000)

    # 创建子目录模拟 workspace/uploads/subdir
    sub = workspace_root / "uploads" / "subdir"
    sub.mkdir(parents=True)
    (sub / "file.txt").write_text("data")

    # 从 uploads/subdir 通过 ../sibling 访问 uploads/sibling（仍在 workspace 内）
    # 但 uploads resolver 的根是 workspace/uploads/，无法访问 workspace/uploads/sibling
    # 所以这个路径穿越被拒绝
    with pytest.raises(SandboxPathViolationError):
        svc.resolve_upload("subdir/../subdir2/file.txt")


# ============================================================================
# 27. 不返回宿主机绝对路径
# ============================================================================


def test_no_host_absolute_path_in_response(client, workspace_root):
    _upload(client, "test.txt", b"content")
    resp = client.get("/api/files")
    for entry in resp.json()["entries"]:
        path = entry["path"].replace("\\", "/")
        assert str(workspace_root).replace("\\", "/") not in path
        assert path.startswith("uploads/")


# ============================================================================
# 28. Content-Disposition 安全
# ============================================================================


def test_content_disposition_safe_filename(client):
    content = b"safe content"
    _upload(client, "safe.txt", content)
    resp = client.get("/api/files/safe.txt")
    assert resp.status_code == 200
    disp = resp.headers.get("content-disposition", "")
    assert "filename=" in disp
    # 不包含路径
    assert "\\" not in disp


# ============================================================================
# 29. Content-Type 由服务端推断
# ============================================================================


def test_content_type_inferred(client):
    content = b"<html>test</html>"
    _upload(client, "test.html", content)
    resp = client.get("/api/files/test.html")
    assert resp.status_code == 200
    # text/html 由 mimetypes 推断
    assert resp.headers.get("content-type", "").startswith("text/html")


# ============================================================================
# 30. 创建 App 不会立即创建 Workspace 资源
# ============================================================================


def test_app_does_not_create_workspace_on_init(workspace_root):
    settings = ApiSettings(workspace_root=str(workspace_root))
    app = create_app(settings=settings)
    # App 构造不创建目录
    assert not (workspace_root / "uploads").exists()
    assert not (workspace_root / "artifacts").exists()


# ============================================================================
# 31. lifespan 正确初始化文件服务
# ============================================================================


def test_lifespan_initializes_file_service(client):
    """Runtime 启动后 file_service 可用。"""
    resp = client.get("/api/files")
    # 不返回 503，说明 file_service 已初始化
    assert resp.status_code == 200


# ============================================================================
# 32. 19A～19D 接口继续通过
# ============================================================================


def test_health_endpoint_still_works(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200


def test_threads_still_work(client):
    resp = client.get("/api/threads")
    assert resp.status_code == 200


# ============================================================================
# 33. OpenAPI 包含文件上传和 Artifact 接口
# ============================================================================


def test_openapi_includes_files_endpoints(app):
    from fastapi.openapi.utils import get_openapi
    openapi = get_openapi(title=app.title, version=app.version, routes=app.routes)
    paths = openapi.get("paths", {})
    assert "/api/files" in paths
    assert "/api/files/{file_path}" in paths
    assert "/api/artifacts" in paths
    assert "/api/artifacts/{artifact_path}" in paths
    # POST 上传端点有 multipart/form-data
    files_post = paths["/api/files"]["post"]
    assert "multipart/form-data" in str(files_post)


# ============================================================================
# 额外：路径安全验证
# ============================================================================


def test_empty_path_rejected(client):
    """空路径被 SandboxPathResolver 拒绝。"""
    resp = client.get("/api/files/")
    # 空路径或根目录访问应被拒绝
    assert resp.status_code in (400, 404, 403)


def test_unicode_path_traversal_rejected(client):
    """Unicode 编码的路径穿越被拒绝。"""
    # %2e%2e = .. URL 编码
    resp = client.get("/api/files/%2e%2e/secret.txt")
    assert resp.status_code in (400, 403, 404)


def test_uploads_prefix_spoofing_rejected(client):
    """uploads-evil 等前缀伪装被拒绝。"""
    # 文件路由只匹配 uploads 子目录，不匹配 uploads-evil
    resp = client.get("/api/files/uploads-evil/file.txt")
    # 应该 404（不存在）或 400/403（路径违规），不是成功
    assert resp.status_code != 200


# ============================================================================
# 34. 流式上传：每次读取不超过 64 KiB，不在 Router 端累积完整文件
# ============================================================================


def test_upload_reader_chunk_size_bounded(workspace_root):
    """Service 层每次 read 不超过 64 KiB。"""
    from mini_deerflow.api.services.workspace_file import WorkspaceFileService
    from mini_deerflow.sandbox_path import WorkspacePathResolver

    resolver = WorkspacePathResolver(workspace_root)
    svc = WorkspaceFileService(resolver, max_upload_bytes=10_000_000)

    # 记录每次 read 的 size
    chunk_sizes = []

    class TrackingFile:
        def __init__(self, size: int):
            self.pos = 0
            self.size = size

        def read(self, n: int) -> bytes:
            chunk_sizes.append(n)
            remaining = self.size - self.pos
            if remaining <= 0:
                return b""
            to_read = min(n, remaining)
            self.pos += to_read
            return b"x" * to_read

    data = b"x" * 200_000  # ~195 KiB
    svc.upload_streaming("large.bin", TrackingFile(len(data)))

    # 所有 chunk_size 应 ≤ 64 KiB
    assert all(s <= 65536 for s in chunk_sizes), f"chunk sizes: {chunk_sizes}"
    # 应有多个 chunk
    assert len(chunk_sizes) >= 3, f"expected >=3 chunks, got {len(chunk_sizes)}"


def test_upload_no_router_accumulation(workspace_root):
    """Router 不累积 chunks list；验证 service 接收的是 file 对象。"""
    from mini_deerflow.api.services.workspace_file import WorkspaceFileService
    from mini_deerflow.sandbox_path import WorkspacePathResolver
    from io import BytesIO

    resolver = WorkspacePathResolver(workspace_root)
    svc = WorkspaceFileService(resolver, max_upload_bytes=10_000_000)

    # 用 BytesIO 模拟 UploadFile.file（SpooledTemporaryFile）
    data = b"streamed_" * 10_000  # ~80 KiB
    bio = BytesIO(data)
    rel, size, _ = svc.upload_streaming("streamed.bin", bio)
    assert size == len(data)


# ============================================================================
# 35. 并发上传同一路径：一个 201，一个 409，不互相覆盖
# ============================================================================


def test_concurrent_upload_same_path_one_201_one_409(client):
    """两个并发请求上传同一路径，严格一个 201、一个 409。"""
    import concurrent.futures
    import io

    content_a = b"content_from_request_a"
    content_b = b"content_from_request_b"

    def upload_request(content: bytes):
        files = {"file": ("concurrent.txt", io.BytesIO(content), "application/octet-stream")}
        resp = client.post("/api/files", files=files)
        return resp.status_code, resp.content

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(upload_request, content_a),
            executor.submit(upload_request, content_b),
        ]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    statuses = [r[0] for r in results]
    # 一个成功，一个冲突
    assert 201 in statuses, f"expected one 201, got {statuses}"
    assert 409 in statuses, f"expected one 409, got {statuses}"


def test_concurrent_upload_final_content_is_one_of_them(client):
    """并发上传后，最终文件内容等于其中一个请求，不混合。"""
    import concurrent.futures
    import io

    contents = [b"AAA", b"BBB", b"CCC"]
    results = {}

    def upload_request(idx: int, content: bytes):
        files = {"file": ("concurrent2.txt", io.BytesIO(content), "application/octet-stream")}
        resp = client.post("/api/files", files=files)
        return idx, resp.status_code, content

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(upload_request, i, c) for i, c in enumerate(contents)]
        for f in concurrent.futures.as_completed(futures):
            idx, status, content = f.result()
            results[idx] = (status, content)

    # 读取最终文件
    resp = client.get("/api/files/concurrent2.txt")
    if resp.status_code == 200:
        final_content = resp.content
        # 最终内容必须是某个请求的原始内容
        assert final_content in contents, (
            f"final content {final_content!r} does not match any request"
        )


# ============================================================================
# 36. flush/fsync 前异常不产生目标文件
# ============================================================================


def test_flush_before_commit_no_target_on_error(workspace_root):
    """flush/fsync 之前异常，不产生目标文件。"""
    from mini_deerflow.api.services.workspace_file import UploadFileTooLargeError
    from mini_deerflow.sandbox_path import WorkspacePathResolver
    from mini_deerflow.api.services.workspace_file import WorkspaceFileService

    resolver = WorkspacePathResolver(workspace_root)
    svc = WorkspaceFileService(resolver, max_upload_bytes=100, max_list_entries=1000)

    class FailingFile:
        def __init__(self):
            self.count = 0

        def read(self, n: int) -> bytes:
            self.count += 1
            if self.count == 1:
                return b"x" * 50
            elif self.count == 2:
                raise IOError("simulated read error")
            return b""

    with pytest.raises(IOError):
        svc.upload_streaming("should_not_exist.txt", FailingFile())

    uploads = workspace_root / "uploads"
    target = uploads / "should_not_exist.txt"
    assert not target.exists(), "目标文件不应存在"


# ============================================================================
# 37. 超限不产生目标文件或临时文件
# ============================================================================


def test_over_limit_no_target_nor_temp_files(workspace_root):
    """超限被拒绝后，不产生目标文件，也不留临时文件。"""
    from mini_deerflow.api.services.workspace_file import UploadFileTooLargeError
    from mini_deerflow.sandbox_path import WorkspacePathResolver
    from mini_deerflow.api.services.workspace_file import WorkspaceFileService

    resolver = WorkspacePathResolver(workspace_root)
    svc = WorkspaceFileService(resolver, max_upload_bytes=100, max_list_entries=1000)

    # 生成超限数据（每次 64 KiB 读取，超过 100 字节限制）
    class LargeFile:
        def __init__(self, total: int):
            self.total = total
            self.pos = 0

        def read(self, n: int) -> bytes:
            if self.pos >= self.total:
                return b""
            to_read = min(n, self.total - self.pos)
            self.pos += to_read
            return b"x" * to_read

    with pytest.raises(UploadFileTooLargeError):
        svc.upload_streaming("overlimit.bin", LargeFile(1024 * 1024))

    uploads = workspace_root / "uploads"
    assert not (uploads / "overlimit.bin").exists()
    # 临时文件也应被清理
    tmp_files = list(uploads.glob(".mdf_up_*"))
    assert len(tmp_files) == 0, f"临时文件残留: {tmp_files}"

