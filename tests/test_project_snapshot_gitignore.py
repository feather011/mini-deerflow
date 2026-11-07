"""快照采集必须尊重 Git 的 ignore 规则（真实临时仓库验证）。

背景（V1 实战实测）：执行代理在工作区里装出来的 ``.venv/`` 与
``.pytest_cache/`` 全部进了 diff —— 4278 个文件，真 deliverable
（``.github/workflows/pr-checks.yml``）被淹没；``last_run_result`` 涨到 5.18 MB。

注意因果：**错误的 PYTHONPATH 只能解释"执行器为什么装了 venv"，
解释不了"为什么已忽略的文件进了 diff"** —— 采集器压根没问过 Git。
两条链分开修：这里修的是采集器。

本文件用**真实临时 git 仓库**验证四件事：
- 新增的业务文件（未跟踪、未被忽略）能出现
- 已被 ignore 的 ``.venv/``、``.pytest_cache/`` 不出现
- 已跟踪文件仍正常显示（哪怕后来被 ignore 规则命中）
- diff 里只有应展示的项目变更
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mini_deerflow.project_tools import _diff_snapshots, _snapshot_workspace


def _git(repo: Path, *args: str) -> None:
    """跑一条只读/本地 git 命令（固定 argv，不经 shell）。"""
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        shell=False,
        timeout=30,
    )


def _paths(snapshot: dict) -> set[str]:
    return set(snapshot.get("files", {}))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """一个真实 git 仓库：.gitignore 忽略 .venv/ 与 .pytest_cache/。"""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "Test")
    (root / ".gitignore").write_text(".venv/\n.pytest_cache/\n", encoding="utf-8")
    (root / "app.py").write_text("print('hello')\n", encoding="utf-8")
    _git(root, "add", ".gitignore", "app.py")
    _git(root, "commit", "-q", "-m", "init")
    return root


class TestSnapshotRespectsGitIgnore:
    def test_已跟踪文件正常显示(self, repo: Path) -> None:
        snapshot = _snapshot_workspace(repo)
        assert snapshot["ignore_mode"] == "git"
        assert "app.py" in _paths(snapshot)

    def test_新增业务文件出现(self, repo: Path) -> None:
        (repo / "feature.py").write_text("x = 1\n", encoding="utf-8")
        snapshot = _snapshot_workspace(repo)
        assert "feature.py" in _paths(snapshot)

    def test_被忽略的环境目录不出现(self, repo: Path) -> None:
        # 执行代理装出来的东西：都在 ignore 规则里
        (repo / ".venv" / "Lib" / "site-packages").mkdir(parents=True)
        (repo / ".venv" / "Lib" / "site-packages" / "pkg.py").write_text(
            "x = 1\n", encoding="utf-8"
        )
        (repo / ".pytest_cache" / "v" / "cache").mkdir(parents=True)
        (repo / ".pytest_cache" / "v" / "cache" / "lastfailed").write_text(
            "{}\n", encoding="utf-8"
        )
        (repo / "feature.py").write_text("y = 2\n", encoding="utf-8")

        snapshot = _snapshot_workspace(repo)
        paths = _paths(snapshot)
        assert "feature.py" in paths
        assert not any(p.startswith(".venv") for p in paths)
        assert not any(p.startswith(".pytest_cache") for p in paths)

    def test_已跟踪但被忽略规则命中的文件仍显示(self, repo: Path) -> None:
        # 先提交一个文件，再把它加进 .gitignore —— 它已被跟踪就该继续显示
        tracked = repo / "keep.py"
        tracked.write_text("k = 1\n", encoding="utf-8")
        _git(repo, "add", "keep.py")
        _git(repo, "commit", "-q", "-m", "add keep")
        (repo / ".gitignore").write_text(
            ".venv/\n.pytest_cache/\nkeep.py\n", encoding="utf-8"
        )
        snapshot = _snapshot_workspace(repo)
        assert "keep.py" in _paths(snapshot)

    def test_diff_只包含应展示的项目变更(self, repo: Path) -> None:
        before = _snapshot_workspace(repo)
        # 执行代理装环境 + 交付一个业务文件
        (repo / ".venv" / "Lib").mkdir(parents=True)
        (repo / ".venv" / "Lib" / "big.py").write_text("y = 2\n", encoding="utf-8")
        (repo / ".pytest_cache").mkdir()
        (repo / ".pytest_cache" / "CACHEDIR.TAG").write_text("t\n", encoding="utf-8")
        (repo / "feature.py").write_text("y = 2\n", encoding="utf-8")
        after = _snapshot_workspace(repo)

        changed = _diff_snapshots(before, after, repo)["files"]
        paths = [entry["path"] for entry in changed]
        assert paths == ["feature.py"]

    def test_非git目录退回目录遍历且仍能采到文件(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "a.txt").write_text("a\n", encoding="utf-8")
        snapshot = _snapshot_workspace(plain)
        assert snapshot["ignore_mode"] == "fallback"
        assert "a.txt" in _paths(snapshot)

    def test_非git目录不会把_git内部算成项目文件(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain2"
        (plain / ".git" / "objects").mkdir(parents=True)
        (plain / ".git" / "objects" / "aa").write_text("blob\n", encoding="utf-8")
        (plain / "a.txt").write_text("a\n", encoding="utf-8")
        snapshot = _snapshot_workspace(plain)
        paths = _paths(snapshot)
        assert "a.txt" in paths
        assert not any(p.startswith(".git") for p in paths)
