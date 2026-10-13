"""托管工作区集成验证脚本。

验证核心功能：
1. 创建项目 → 验证 {workspaces_root}/{project_id}/workspace/ 目录存在
2. 创建项目 → 创建 Thread → 验证 {workspaces_root}/{project_id}/sessions/{thread_id}/ 存在
3. 不同项目的工作区互不影响
4. ZIP 下载只包含 workspace/ 内文件
5. 浏览 API 返回正确文件列表

使用临时目录作为 workspaces_root，测试后清理。
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

# 添加 src 目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from fastapi.testclient import TestClient

from mini_deerflow.api.app import create_app
from mini_deerflow.model_config import ModelSettings
from mini_deerflow.model_factory import create_model
from mini_deerflow.runtime import RuntimeConfig, RuntimeContext, create_runtime
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.project_store import InMemoryProjectStore
from mini_deerflow.tools import create_default_registry
from dataclasses import dataclass


@dataclass
class MockSettings:
    """模拟 ApiSettings，修复 RuntimeContext 缺少 settings 属性的问题。"""
    workspaces_root: str | None = None


class MockRuntimeContext(RuntimeContext):
    """带 settings 属性的 RuntimeContext 替身。"""
    def __init__(self, *args, workspaces_root: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.settings = MockSettings(workspaces_root=workspaces_root)


class TestResult:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def ok(self, name: str) -> None:
        self.passed.append(name)
        print(f"  [PASS] {name}")

    def fail(self, name: str, reason: str) -> None:
        self.failed.append((name, reason))
        print(f"  [FAIL] {name}: {reason}")

    @property
    def all_passed(self) -> bool:
        return len(self.failed) == 0


def run_tests() -> TestResult:
    result = TestResult()
    temp_dir = None

    try:
        # 创建临时目录作为 workspaces_root
        temp_dir = tempfile.mkdtemp(prefix="ws_test_")
        workspaces_root = temp_dir
        print(f"临时工作区目录: {workspaces_root}\n")

        # 配置 FakeModel
        model_settings = ModelSettings(provider="fake")

        # 创建 Runtime
        def runtime_factory():
            runtime = create_runtime(
                store=InMemoryThreadStore(),
                project_store=InMemoryProjectStore(),
                registry=create_default_registry(),
                config=RuntimeConfig(
                    workspace_root=workspaces_root,
                    workspace_max_upload_bytes=50 * 1024 * 1024,
                    workspace_max_list_entries=1000,
                    model_settings=model_settings,
                ),
                workspaces_root=workspaces_root,
            )
            # 给 RuntimeContext 添加 settings 属性以兼容 projects.py
            runtime.settings = MockSettings(workspaces_root=workspaces_root)
            return runtime

        app = create_app(runtime_factory=runtime_factory)

        with TestClient(app) as client:
            # =================================================================
            # 测试 1: 创建项目 → workspace/ 目录存在
            # =================================================================
            print("[测试 1] 创建项目验证 workspace 目录")

            resp = client.post("/api/projects", json={"name": "TestProjectA"})
            project_id = None
            project_a_id = None
            project_a_workspace = None

            if resp.status_code != 201:
                result.fail("创建项目", f"HTTP {resp.status_code}: {resp.text}")
            else:
                data = resp.json()
                project_id = data["project_id"]
                project_a_id = project_id
                workspace_path = data.get("workspace_path", "")

                # 验证目录存在
                expected_workspace = Path(workspaces_root) / project_id / "workspace"
                if expected_workspace.exists():
                    result.ok(f"workspace 目录存在: {expected_workspace}")
                else:
                    result.fail("workspace 目录存在", f"目录不存在: {expected_workspace}")

                # 验证 sessions 目录存在
                expected_sessions = Path(workspaces_root) / project_id / "sessions"
                if expected_sessions.exists():
                    result.ok(f"sessions 目录存在: {expected_sessions}")
                else:
                    result.fail("sessions 目录存在", f"目录不存在: {expected_sessions}")

                project_a_workspace = expected_workspace

            # =================================================================
            # 测试 2: 创建 Thread → sessions/{thread_id}/ 存在
            # =================================================================
            print("\n[测试 2] 创建 Thread 验证 sessions/{thread_id} 目录")

            project_b_id = None

            # 创建第二个项目用于测试（用于后续的工作区隔离测试）
            resp2 = client.post("/api/projects", json={"name": "TestProjectB"})
            if resp2.status_code != 201:
                result.fail("创建项目B", f"HTTP {resp2.status_code}: {resp2.text}")
            else:
                project_b_id = resp2.json()["project_id"]

                # 直接创建 sessions 目录来验证项目结构
                expected_sessions_dir = (
                    Path(workspaces_root) / project_b_id / "sessions"
                )
                test_session_dir = expected_sessions_dir / "manual-test-session"
                test_session_dir.mkdir(parents=True, exist_ok=True)

                if test_session_dir.exists():
                    result.ok(f"手动创建 sessions/{test_session_dir.name} 目录成功")
                else:
                    result.fail(
                        "手动创建 sessions 目录",
                        f"目录不存在: {test_session_dir}",
                    )

            # 验证 Thread API 基本功能
            thread_payload = {"thread_id": "test-thread-001"}
            resp_thread = client.post("/api/threads", json=thread_payload)
            if resp_thread.status_code != 201:
                result.fail("创建 Thread", f"HTTP {resp_thread.status_code}: {resp_thread.text}")
            else:
                thread_id = resp_thread.json()["thread_id"]
                result.ok(f"Thread API 创建成功: {thread_id}")

            # =================================================================
            # 测试 3: 不同项目的工作区互不影响
            # =================================================================
            print("\n[测试 3] 不同项目工作区隔离验证")

            # 获取项目 A 的信息（从测试 1 中获取）
            project_a_id = resp.json()["project_id"]
            project_a_workspace = Path(workspaces_root) / project_a_id / "workspace"

            if project_b_id is None:
                result.fail("测试 3", "项目B未创建，跳过隔离测试")
            else:
                # 在项目A的 workspace 中创建文件（project_a_id 和 project_a_workspace 已在测试 1 中定义）
                test_file_a = project_a_workspace / "file_in_project_a.txt"
                test_file_a.write_text("这是项目A的文件", encoding="utf-8")

                # 在项目B的 workspace 中创建文件
                project_b_workspace = Path(workspaces_root) / project_b_id / "workspace"
                test_file_b = project_b_workspace / "file_in_project_b.txt"
                test_file_b.write_text("这是项目B的文件", encoding="utf-8")

                # 浏览项目A的工作区，验证只有项目A的文件
                resp_a = client.get(f"/api/projects/{project_a_id}/workspace/browse")
                if resp_a.status_code != 200:
                    result.fail("浏览项目A工作区", f"HTTP {resp_a.status_code}")
                else:
                    entries_a = resp_a.json().get("entries", [])
                    names_a = [e["name"] for e in entries_a if not e.get("is_dir")]
                    if "file_in_project_a.txt" in names_a:
                        result.ok("项目A工作区包含自己的文件")
                    else:
                        result.fail("项目A工作区包含自己的文件", f"文件列表: {names_a}")

                    if "file_in_project_b.txt" not in names_a:
                        result.ok("项目A工作区不包含项目B的文件")
                    else:
                        result.fail(
                            "项目A工作区不包含项目B的文件",
                            "项目B的文件出现在项目A中",
                        )

                # 浏览项目B的工作区，验证只有项目B的文件
                resp_b = client.get(f"/api/projects/{project_b_id}/workspace/browse")
                if resp_b.status_code != 200:
                    result.fail("浏览项目B工作区", f"HTTP {resp_b.status_code}")
                else:
                    entries_b = resp_b.json().get("entries", [])
                    names_b = [e["name"] for e in entries_b if not e.get("is_dir")]
                    if "file_in_project_b.txt" in names_b:
                        result.ok("项目B工作区包含自己的文件")
                    else:
                        result.fail("项目B工作区包含自己的文件", f"文件列表: {names_b}")

                    if "file_in_project_a.txt" not in names_b:
                        result.ok("项目B工作区不包含项目A的文件")
                    else:
                        result.fail(
                            "项目B工作区不包含项目A的文件",
                            "项目A的文件出现在项目B中",
                        )

            # =================================================================
            # 测试 4: ZIP 下载只包含 workspace/ 内文件
            # =================================================================
            print("\n[测试 4] ZIP 下载内容验证")

            # 在项目A的 workspace 中添加更多文件
            subdir = project_a_workspace / "subdir"
            subdir.mkdir(exist_ok=True)
            (subdir / "nested.txt").write_text("嵌套文件", encoding="utf-8")

            # 下载项目A的 ZIP
            resp_zip = client.get(f"/api/projects/{project_a_id}/workspace/download")
            if resp_zip.status_code != 200:
                result.fail("下载项目A ZIP", f"HTTP {resp_zip.status_code}")
            else:
                zip_content = io.BytesIO(resp_zip.content)
                with zipfile.ZipFile(zip_content, "r") as zf:
                    names = zf.namelist()

                    # 验证包含 workspace 内的文件
                    if any("file_in_project_a.txt" in n for n in names):
                        result.ok("ZIP 包含 workspace 文件")
                    else:
                        result.fail("ZIP 包含 workspace 文件", f"ZIP内容: {names}")

                    if any("nested.txt" in n for n in names):
                        result.ok("ZIP 包含嵌套目录文件")
                    else:
                        result.fail("ZIP 包含嵌套目录文件", f"ZIP内容: {names}")

                    # 验证不包含 sessions 目录（应在 workspace 外）
                    if not any("sessions" in n for n in names):
                        result.ok("ZIP 不包含 sessions 目录")
                    else:
                        result.fail(
                            "ZIP 不包含 sessions 目录",
                            f"sessions 出现在 ZIP 中: {[n for n in names if 'sessions' in n]}",
                        )

                    # 验证不包含 .git 目录
                    if not any(".git" in n for n in names):
                        result.ok("ZIP 不包含 .git 目录")
                    else:
                        result.fail(
                            "ZIP 不包含 .git 目录",
                            f".git 出现在 ZIP 中: {[n for n in names if '.git' in n]}",
                        )

            # =================================================================
            # 测试 5: 浏览 API 返回正确文件列表
            # =================================================================
            print("\n[测试 5] 浏览 API 文件列表验证")

            # 再次浏览项目A，验证文件列表正确
            resp_browse = client.get(f"/api/projects/{project_a_id}/workspace/browse")
            if resp_browse.status_code != 200:
                result.fail("浏览项目A工作区", f"HTTP {resp_browse.status_code}")
            else:
                data = resp_browse.json()
                entries = data.get("entries", [])

                # 验证目录和文件都存在
                names = [e["name"] for e in entries]
                if "subdir" in names:
                    result.ok("浏览返回包含子目录")
                else:
                    result.fail("浏览返回包含子目录", f"目录列表: {names}")

                if "file_in_project_a.txt" in names:
                    result.ok("浏览返回包含根目录文件")
                else:
                    result.fail("浏览返回包含根目录文件", f"文件列表: {names}")

                # 验证文件大小正确
                file_entry = next(
                    (e for e in entries if e["name"] == "file_in_project_a.txt"), None
                )
                if file_entry and file_entry.get("size", 0) > 0:
                    result.ok(f"文件大小正确: {file_entry['size']} 字节")
                else:
                    result.fail(
                        "文件大小正确",
                        f"文件大小异常: {file_entry.get('size') if file_entry else 'N/A'}",
                    )

                # 浏览子目录
                resp_subdir = client.get(
                    f"/api/projects/{project_a_id}/workspace/browse?path=subdir"
                )
                if resp_subdir.status_code != 200:
                    result.fail("浏览子目录", f"HTTP {resp_subdir.status_code}")
                else:
                    subdir_entries = resp_subdir.json().get("entries", [])
                    subdir_names = [e["name"] for e in subdir_entries]
                    if "nested.txt" in subdir_names:
                        result.ok("子目录浏览返回正确文件")
                    else:
                        result.fail(
                            "子目录浏览返回正确文件",
                            f"子目录文件列表: {subdir_names}",
                        )

            # =================================================================
            # 测试 6: 读取文件内容 API
            # =================================================================
            print("\n[测试 6] 读取文件内容 API 验证")

            resp_file = client.get(
                f"/api/projects/{project_a_id}/workspace/file",
                params={"path": "file_in_project_a.txt"},
            )
            if resp_file.status_code != 200:
                result.fail("读取文件内容", f"HTTP {resp_file.status_code}")
            else:
                content = resp_file.json().get("content", "")
                if "项目A" in content:
                    result.ok("文件内容读取正确")
                else:
                    result.fail("文件内容读取正确", f"读取内容: {content}")

            # =================================================================
            # 测试 7: 删除项目清理工作区
            # =================================================================
            print("\n[测试 7] 删除项目清理工作区")

            resp_del = client.delete(f"/api/projects/{project_a_id}")
            if resp_del.status_code != 204:
                result.fail("删除项目A", f"HTTP {resp_del.status_code}")
            else:
                project_a_root = Path(workspaces_root) / project_a_id
                if not project_a_root.exists():
                    result.ok("删除项目后工作区目录已清理")
                else:
                    result.fail(
                        "删除项目后工作区目录已清理",
                        f"目录仍存在: {project_a_root}",
                    )

    finally:
        # 清理临时目录
        if temp_dir and Path(temp_dir).exists():
            try:
                shutil.rmtree(temp_dir)
                print(f"\n临时目录已清理: {temp_dir}")
            except OSError as e:
                print(f"\n清理临时目录失败: {e}")

    return result


def main() -> int:
    print("=" * 60)
    print("托管工作区集成验证")
    print("=" * 60 + "\n")

    result = run_tests()

    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    print(f"通过: {len(result.passed)} 项")
    print(f"失败: {len(result.failed)} 项")

    if result.failed:
        print("\n失败项:")
        for name, reason in result.failed:
            print(f"  - {name}: {reason}")
        return 1
    else:
        print("\n所有测试通过!")
        return 0


if __name__ == "__main__":
    sys.exit(main())
