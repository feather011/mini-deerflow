"""E2E 专用后端启动入口（20F）。

与生产入口 `mini_deerflow.api.app` 的关系：
- 复用同一个 `create_app()` 工厂和**全部生产路由**，不新增任何测试专用路由
- 只在进程启动时替换四项外部依赖：
  1. workspace 根目录 → 固定临时目录（可预测、可清理）
  2. 预置一个 Artifact 文件（artifacts/ 只读视图需要一个可下载的样本）
  3. CORS origin → 指向 Vite dev server
  4. 模型 → FakeModel 外包一层确定性延迟

模型确定性：
- 底层仍是 `FakeModel`，不消耗 API Key、不发起任何外部网络请求
- 输入 "计算 1+1" 的行为完全确定：
  decide() → calculator 工具调用 → 工具结果回灌 → decide() → FinalAnswer("计算结果: 2")
- 延迟是**每步 decide 固定 sleep**，不改变事件序列，只是让「运行中」状态
  在 localhost 上保持足够长的窗口——否则整轮 <100ms，浏览器根本看不到
  「运行中」「停止接收」这类瞬时 UI（必测项 5 无法观察）。

启动：
    uv run python -m tests.e2e_server --port 8000

环境变量：
    MINI_DEERFLOW_E2E_WORKSPACE     workspace 目录（默认：系统临时目录）
    MINI_DEERFLOW_E2E_ARTIFACT      预置 Artifact 文件名（默认 artifact-test.txt）
    MINI_DEERFLOW_E2E_MODEL_DELAY_MS 每步 decide 的延迟毫秒数（默认 500；0 = 关闭）
"""

from __future__ import annotations

import argparse
import os
import tempfile
import time
from pathlib import Path

from mini_deerflow.api.app import create_app
from mini_deerflow.api.config import ApiSettings
from mini_deerflow.models import FakeModel, Model, ToolCall, FinalAnswer, Message
from mini_deerflow.runtime import RuntimeConfig, create_runtime
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tools import create_default_registry

# 预置 Artifact 的固定内容。E2E 断言下载内容与它逐字节相等。
ARTIFACT_CONTENT = "mini-deerflow-20f-e2e-artifact\n"
ARTIFACT_NAME = "artifact-test.txt"

DEFAULT_MODEL_DELAY_MS = 500


class SlowFakeModel:
    """在 FakeModel 外包一层固定延迟，用于让「运行中」状态可观察。

    不改变 decide 的返回值，只推迟返回时机，因此事件序列与纯 FakeModel 完全一致。
    """

    def __init__(self, base: Model, delay_seconds: float) -> None:
        self._base = base
        self._delay = delay_seconds

    def decide(self, messages: list[Message], tools=None) -> ToolCall | FinalAnswer:
        if self._delay > 0:
            time.sleep(self._delay)
        # 必须把 tools 继续转发：包装器若丢弃，模型将看不到任何可调用工具描述，
        # Agent 的工具循环会在 E2E 里静默失效（FakeModel 自身忽略 tools，不受影响）。
        return self._base.decide(messages, tools)


def _resolve_workspace() -> Path:
    """返回 workspace 根目录，未指定时创建临时目录。"""
    raw = os.environ.get("MINI_DEERFLOW_E2E_WORKSPACE")
    if raw:
        root = Path(raw)
    else:
        root = Path(tempfile.mkdtemp(prefix="mdf_e2e_ws_"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def seed_artifact(workspace: Path, name: str = ARTIFACT_NAME) -> Path:
    """在 workspace/artifacts/ 下写入预置 Artifact。

    WorkspaceFileService 构造时会自动创建 uploads/ 与 artifacts/ 子目录，
    但种子文件必须在服务启动前写入，因此这里显式创建目录。
    """
    artifacts_dir = workspace / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    target = artifacts_dir / name
    # 必须用 write_bytes：write_text 在 Windows 文本模式下会把 \n 译成 \r\n，
    # 导致落盘字节数与 E2E 侧常量不一致，逐字节断言会失败。
    target.write_bytes(ARTIFACT_CONTENT.encode("utf-8"))
    return target


def _model_delay_seconds() -> float:
    raw = os.environ.get("MINI_DEERFLOW_E2E_MODEL_DELAY_MS")
    ms = DEFAULT_MODEL_DELAY_MS if raw is None else int(raw)
    return max(ms, 0) / 1000.0


def build_e2e_app(workspace: Path | None = None, delay_seconds: float | None = None):
    """构造 E2E 用 FastAPI 应用。"""
    root = workspace or _resolve_workspace()
    seed_artifact(root)
    delay = _model_delay_seconds() if delay_seconds is None else delay_seconds

    # Vite 端口由 E2E_FRONTEND_PORT 控制（Playwright 用同一个变量启动 vite）。
    # 这里必须跟着它走：原先硬编码 5173–5179 区间，一旦换端口跑 E2E，
    # 全部请求会被 CORS 拦下，18 个用例会同时"莫名其妙"失败。
    # 保留默认区间，使多份 E2E 并行执行时互不冲突。
    ports: set[int] = set(range(5173, 5180))
    raw_port = os.environ.get("E2E_FRONTEND_PORT", "").strip()
    if raw_port.isdigit():
        ports.add(int(raw_port))

    vite_origins = [
        f"http://{host}:{p}"
        for host in ("localhost", "127.0.0.1")
        for p in sorted(ports)
    ]

    settings = ApiSettings(
        cors_origins=vite_origins,
        workspace_root=str(root),
        # E2E 需要稳定复现 413，因此把上限调到 1 MiB（够小，又不至于误伤正常上传）
        max_upload_bytes=1024 * 1024,
    )

    def runtime_factory():
        return create_runtime(
            store=InMemoryThreadStore(),
            registry=create_default_registry(),
            config=RuntimeConfig(
                workspace_root=str(settings.workspace_root),
                workspace_max_upload_bytes=settings.max_upload_bytes,
                workspace_max_list_entries=settings.max_list_entries,
            ),
            model=SlowFakeModel(FakeModel(), delay),
        )

    return create_app(settings=settings, runtime_factory=runtime_factory)


def main() -> None:
    parser = argparse.ArgumentParser(description="E2E 专用后端入口")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    import uvicorn

    workspace = _resolve_workspace()
    app = build_e2e_app(workspace)
    print(f"[e2e] workspace: {workspace}", flush=True)
    print(f"[e2e] artifact seeded: artifacts/{ARTIFACT_NAME}", flush=True)
    print(f"[e2e] model delay: {_model_delay_seconds() * 1000:.0f}ms/step", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
