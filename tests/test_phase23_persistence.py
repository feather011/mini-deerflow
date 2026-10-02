"""Phase 23 持久化验收（自动化，不依赖 Docker、不消耗真实额度）。

覆盖「docker compose down → up 之后数据还在」这一目标的**代码侧等价物**：
同一个 SQLite 文件 + 同一个 workspace 目录，跨「应用重启」验证：

- Thread 与消息仍在
- 上传文件仍可列出与下载
- Artifact 仍可下载
- 未配置 `MINI_DEERFLOW_DB_PATH` 时仍走内存实现（默认行为不变）
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mini_deerflow.api.app import create_app
from mini_deerflow.api.config import ApiSettings


def _app(db_path: Path | None, workspace: Path) -> FastAPI:
    """构造挂在固定 SQLite / 固定 workspace 上的应用（与容器里的配置一致）。"""
    return create_app(
        ApiSettings(
            db_path=str(db_path) if db_path else None,
            workspace_root=str(workspace),
        )
    )


def _create_thread(client: TestClient) -> str:
    resp = client.post("/api/threads", json={"title": "持久化"})
    assert resp.status_code == 201, resp.text
    return resp.json()["thread_id"]


# ---------------------------------------------------------------------------
# Thread 与消息
# ---------------------------------------------------------------------------


def test_thread_and_messages_survive_restart(tmp_path: Path) -> None:
    db = tmp_path / "data" / "app.db"  # 故意用尚不存在的嵌套目录
    workspace = tmp_path / "ws"

    with TestClient(_app(db, workspace)) as first:
        tid = _create_thread(first)
        run = first.post(
            f"/api/threads/{tid}/runs", json={"message": "计算 1+1"}
        )
        assert run.status_code == 200, run.text
        assert run.json()["answer"] == "计算结果: 2"

    # 第一次 lifespan 已 shutdown（store 关闭）；模拟容器重启
    with TestClient(_app(db, workspace)) as second:
        listed = second.get("/api/threads").json()["threads"]
        assert [t["thread_id"] for t in listed] == [tid]

        detail = second.get(f"/api/threads/{tid}")
        assert detail.status_code == 200
        messages = detail.json()["messages"]
        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert messages[0]["content"] == "计算 1+1"
        assert "2" in messages[1]["content"]


def test_database_file_is_created_in_missing_parent_dir(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "deeper" / "app.db"
    with TestClient(_app(db, tmp_path / "ws")):
        pass
    assert db.is_file()


def test_uses_sqlite_store_when_db_path_configured(tmp_path: Path) -> None:
    from mini_deerflow.thread_store import SQLiteThreadStore

    with TestClient(_app(tmp_path / "app.db", tmp_path / "ws")) as client:
        runtime = client.app.state.runtime
        assert isinstance(runtime.store, SQLiteThreadStore)


def test_default_is_in_memory_store(tmp_path: Path) -> None:
    """未配置 db_path 时必须保持内存实现——这是既有开发/测试的默认行为。"""
    from mini_deerflow.thread_store import InMemoryThreadStore

    with TestClient(create_app(ApiSettings(workspace_root=str(tmp_path)))) as client:
        runtime = client.app.state.runtime
        assert isinstance(runtime.store, InMemoryThreadStore)


def test_in_memory_threads_do_not_leak_between_app_instances(
    tmp_path: Path,
) -> None:
    with TestClient(create_app(ApiSettings(workspace_root=str(tmp_path)))) as first:
        _create_thread(first)

    with TestClient(create_app(ApiSettings(workspace_root=str(tmp_path)))) as second:
        assert second.get("/api/threads").json()["threads"] == []


def test_db_path_env_var_is_honoured(
    tmp_path: Path, monkeypatch
) -> None:
    """系统环境变量 MINI_DEERFLOW_DB_PATH 决定持久化开关（容器就是这么配的）。"""
    from mini_deerflow.thread_store import SQLiteThreadStore

    db = tmp_path / "from-env.db"
    monkeypatch.setenv("MINI_DEERFLOW_DB_PATH", str(db))
    settings = ApiSettings()
    assert settings.db_path == str(db)

    with TestClient(create_app(settings)) as client:
        assert isinstance(client.app.state.runtime.store, SQLiteThreadStore)


def test_blank_db_path_falls_back_to_memory(tmp_path: Path) -> None:
    """db_path 为空串视为未配置，不会创建名为 '' 的数据库。"""
    from mini_deerflow.thread_store import InMemoryThreadStore

    with TestClient(
        create_app(ApiSettings(db_path="", workspace_root=str(tmp_path)))
    ) as client:
        assert isinstance(client.app.state.runtime.store, InMemoryThreadStore)


# ---------------------------------------------------------------------------
# 上传文件与产物
# ---------------------------------------------------------------------------


def test_upload_survives_restart(tmp_path: Path) -> None:
    db = tmp_path / "app.db"
    workspace = tmp_path / "ws"
    payload = b"persistence-check-bytes\n"

    with TestClient(_app(db, workspace)) as first:
        resp = first.post(
            "/api/files",
            params={"dest": "note.txt"},
            files={"file": ("note.txt", payload, "text/plain")},
        )
        assert resp.status_code == 201, resp.text

    with TestClient(_app(db, workspace)) as second:
        entries = second.get("/api/files").json()["entries"]
        assert any(e["name"] == "note.txt" for e in entries)
        downloaded = second.get("/api/files/note.txt")
        assert downloaded.status_code == 200
        assert downloaded.content == payload


def test_artifact_survives_restart(tmp_path: Path) -> None:
    db = tmp_path / "app.db"
    workspace = tmp_path / "ws"

    # Artifact 由运行产物生成；这里直接落盘，验证重启后仍可下载。
    # 用 write_bytes 而非 write_text：Windows 文本模式会把 \n 换成 \r\n，
    # 断言字节内容时会被平台差异干扰。
    artifacts = workspace / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "report.txt").write_bytes(b"artifact-content\n")

    with TestClient(_app(db, workspace)):
        pass

    with TestClient(_app(db, workspace)) as second:
        resp = second.get("/api/artifacts/report.txt")
        assert resp.status_code == 200
        # 比 bytes 而不是 resp.text：后者会做换行规范化，掩盖真实内容
        assert resp.content == b"artifact-content\n"
