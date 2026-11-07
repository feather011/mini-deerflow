"""TOOL-07 用户验收环境（人工体验用）。

**这是一个测试入口，不是正常聊天入口。** 它把生产模型换成了脚本化的
`_AcceptanceModel`（见下方警告横幅），因此只能验证"流程"，不能验证对话质量。
正常聊天请用生产入口 `uvicorn mini_deerflow.api.app:app`。

目的：让验收人能在浏览器里真实体验四件事 —— **派发 / 进度 / 停止 / 结果**，
并且 **全程不调用任何付费模型、不外网**。

组成：

- **后端**：复用生产的 `create_app()` + 全部生产路由，只替换三项外部依赖：
  1. **模型** → `_AcceptanceModel`（脚本化）：讨论类问题直接给只读回答；
     明确的编码请求才派发 `project_execute`（工具链路是真的）。
  2. **harness** → 真实子进程，但用 harness 自带的 `FakeLLMClient`
     （`HARNESS_USE_FAKE_LLM=1`）：它会真的写 `hello.c`、真的产出 diff，
     但**不读配置、不发任何网络请求**。
  3. **项目仓库** → 自动创建的临时 git 仓库，验收人在界面里绑定它。

- **前端**：仓库自带的 `npm run dev`（开发模式，本机访问）。

启动：

    python scripts/tool07_acceptance_server.py            # 后端，默认 127.0.0.1:8000
    cd frontend && npm run dev                            # 前端，http://localhost:5173

脚本会打印：后端地址、前端地址、要绑定的仓库路径、以及建议体验的 4 个动作。

注意：这是**验收辅助脚本**，不属于生产入口，也不进运行时依赖；
它的存在只是让"人工体验"不依赖付费模型。生产入口没有配置模型 provider 时
会**明确报错**（`ModelConfigError`），不会退回这里的固定回复。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HARNESS_ROOT = Path("D:/项目解读/harnessagent")

# 编码类请求的触发词（脚本化模型据此决定是否派发 harness）
_CODING_KEYWORDS = ("写", "创建", "新建", "实现", "生成", "新增", "hello", "代码")


def _banner(text: str) -> None:
    print(f"\n=== {text} ===", flush=True)


def _make_scratch_repo(root: Path) -> Path:
    """建一个临时 git 仓库作为验收用的项目目录。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text(
        "# TOOL-07 验收仓库\n\n验收用的空仓库：harness 会在此创建 hello.c。\n",
        encoding="utf-8",
    )
    try:
        subprocess.run(
            ["git", "init", "-q"],
            cwd=str(root),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        subprocess.run(
            ["git", "add", "README.md"],
            cwd=str(root),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.email=acceptance@example.invalid",
                "-c",
                "user.name=TOOL-07 Acceptance",
                "commit",
                "-q",
                "-m",
                "验收仓库初始状态",
            ],
            cwd=str(root),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"[warn] git 初始化失败（不影响验收，diff 会退化为文件列表）: {exc}")
    return root


def _write_fake_harness_env() -> Path:
    """给 harness 准备一份**独立**的假 .env（只有假凭证）。"""
    env_dir = Path(tempfile.mkdtemp(prefix="tool07-acceptance-env-"))
    env_file = env_dir / ".env"
    env_file.write_text(
        "MIMO_API_KEY=fake-key-for-acceptance\n"
        "HARNESS_MIMO_BASE_URL=http://127.0.0.1:1/unused\n",
        encoding="utf-8",
    )
    return env_file


class _AcceptanceModel:
    """脚本化模型：只用于验收，替代真实模型（不联网、不计费）。

    - 讨论类问题 → 直接给只读回答（**不派发** harness）
    - 编码类请求 → 派发 `project_execute`（工具链路真实）
    - 工具结果回来 → 汇总成最终回答
    """

    def __init__(self, delay_seconds: float = 0.6) -> None:
        self._delay = delay_seconds

    def decide(self, messages, tools=None):
        import time

        from mini_deerflow.models import FinalAnswer, ToolCall

        if self._delay > 0:
            time.sleep(self._delay)

        last_user = ""
        last_is_tool_result = False
        for msg in messages:
            if msg.role == "user":
                last_user = msg.content
                last_is_tool_result = False
            elif msg.role == "tool":
                last_is_tool_result = True

        # 只有"最后一条消息是工具结果"才表示本轮 harness 刚跑完。
        # 否则（用户又发了一条新消息）必须重新按请求类型决策，
        # 否则"停止后再次执行"永远派发不出去。
        if last_is_tool_result:
            return FinalAnswer(
                content="已完成：harness 在项目仓库里创建了 hello.c。"
                "修改说明、diff、新文件与验证结果见右侧「查看任务」面板。"
            )

        if any(kw in last_user for kw in _CODING_KEYWORDS):
            return ToolCall(
                name="project_execute",
                arguments={"task": last_user, "context": ""},
            )

        return FinalAnswer(
            content="这是只读讨论：我不会改动你的项目仓库。"
            "如果要真正动手改代码，请明确说「写一个 hello.c」这类编码请求。"
        )


def build_app(*, harness_root: Path, scratch_repo: Path, port: int):
    """构造验收用 FastAPI 应用（复用生产 create_app + 全部生产路由）。"""
    from mini_deerflow.api.app import create_app
    from mini_deerflow.api.config import ApiSettings
    from mini_deerflow.project_store import InMemoryProjectStore
    from mini_deerflow.runtime import RuntimeConfig, create_runtime
    from mini_deerflow.thread_store import InMemoryThreadStore
    from mini_deerflow.tools import create_default_registry

    frontend_ports = range(5173, 5180)
    settings = ApiSettings(
        cors_origins=[
            f"http://{host}:{p}"
            for host in ("localhost", "127.0.0.1")
            for p in frontend_ports
        ],
        workspace_root=str(scratch_repo.parent / "_workspace"),
        harness_root=str(harness_root),
        harness_timeout_seconds=180.0,
    )

    # 线程/项目存储在进程内保持（runtime_factory 只在 lifespan 里调用一次）
    store = InMemoryThreadStore()
    project_store = InMemoryProjectStore()

    def runtime_factory():
        return create_runtime(
            store=store,
            project_store=project_store,
            registry=create_default_registry(),
            config=RuntimeConfig(
                workspace_root=str(settings.workspace_root),
                harness_root=settings.harness_root,
                harness_timeout_seconds=settings.harness_timeout_seconds,
                disable_budget=True,
            ),
            model=_AcceptanceModel(),
        )

    return create_app(settings=settings, runtime_factory=runtime_factory)


def main() -> int:
    # 终端/重定向场景下让输出即时可见且不乱码（Windows 默认 GBK）
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    except (AttributeError, OSError):  # pragma: no cover - 极老的 Python/非标准流
        pass

    parser = argparse.ArgumentParser(description="TOOL-07 验收用后端")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--harness-root",
        default=str(DEFAULT_HARNESS_ROOT),
        help="harnessagent 仓库路径（默认 %(default)s）",
    )
    parser.add_argument(
        "--scratch-repo",
        default="",
        help="验收用项目仓库路径（默认自动建临时目录）",
    )
    parser.add_argument(
        "--fake-llm-delay",
        type=float,
        default=0.0,
        help="harness 假 LLM 每次调用的额外延迟（秒）。"
        "设成 20~30 可留出点击「停止」的窗口；默认 0（跑得很快）。",
    )
    args = parser.parse_args()

    harness_root = Path(args.harness_root).resolve()
    if not (harness_root / "harness" / "noninteractive.py").exists():
        print(f"[error] harness 根目录无效: {harness_root}", file=sys.stderr)
        return 2

    scratch = (
        Path(args.scratch_repo).resolve()
        if args.scratch_repo
        else _make_scratch_repo(
            Path(tempfile.mkdtemp(prefix="tool07-acceptance-repo-"))
        )
    )

    # --- 隔离：绝不让 harness 或本进程用到真实 Provider ---
    env_file = _write_fake_harness_env()
    os.environ["HARNESS_ENV_FILE"] = str(env_file)
    os.environ["HARNESS_USE_FAKE_LLM"] = "1"
    os.environ["HARNESS_ROOT"] = str(harness_root)
    if args.fake_llm_delay > 0:
        os.environ["HARNESS_FAKE_LLM_DELAY"] = str(args.fake_llm_delay)
    # 清掉真实凭证（子进程会继承 os.environ）
    for var in ("MINIMAX_API_KEY", "MINIMAX_OPENAI_BASE_URL", "MINIMAX_MODEL",
                "MIMO_API_KEY", "HARNESS_MIMO_BASE_URL", "HARNESS_MIMO_API_KEY"):
        os.environ.pop(var, None)

    app = build_app(harness_root=harness_root, scratch_repo=scratch, port=args.port)

    import uvicorn

    _banner("TOOL-07 验收环境")
    print("!! 脚本模型，只用于流程验收 —— 不具备正常聊天能力 !!")
    print("   本入口用 _AcceptanceModel 替换了生产模型：它只按关键词返回固定文案，")
    print("   连续问不同的问题也只会得到同一段回答。这**不是**缺陷，是这个入口的用途。")
    print("   正常聊天请用生产入口：uvicorn mini_deerflow.api.app:app --port 8000")
    print("")
    print(f"后端 API      : http://{args.host}:{args.port}")
    print(f"前端（另开终端）: cd frontend && npm run dev   → http://localhost:5173")
    print(f"要绑定的仓库  : {scratch}")
    print(f"harness 根目录: {harness_root}（假 LLM，不联网、不计费）")
    print("")
    print("建议体验顺序：")
    print("  1) 派发：普通会话问「这个仓库是做什么的」→ 应得到只读回答；")
    print("     项目会话问「写一个 hello.c」→ 应派发 harness（事件里出现 tool.started project_execute）")
    print("  2) 进度：观察「正在执行什么」是否可读")
    print("  3) 停止：运行中点「停止」→ 任务真正停止；随后能再次执行")
    print("  4) 结果：修改说明、diff、新文件、验证结果是否清楚准确")
    print("")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
