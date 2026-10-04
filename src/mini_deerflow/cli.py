"""命令行界面。"""

from __future__ import annotations

import argparse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from mini_deerflow.agent import Agent
from mini_deerflow.memory import Memory, MemoryNotFoundError
from mini_deerflow.memory_context import MemoryContextBuilder
from mini_deerflow.memory_dedup import ExactMemoryDeduplicator
from mini_deerflow.memory_extractor import RuleBasedMemoryExtractor
from mini_deerflow.memory_manager import MemoryManager
from mini_deerflow.memory_store_sqlite import SQLiteMemoryStore
from mini_deerflow.models import FakeModel
from mini_deerflow.mcp_config import load_mcp_config
from mini_deerflow.model_config import load_model_settings
from mini_deerflow.skill_catalog import SkillCatalog, SkillNotFoundError
from mini_deerflow.skill_context import SkillContextBuilder
from mini_deerflow.skill_loader import ProgressiveSkillLoader
from mini_deerflow.skill_metadata import MarkdownSkillMetadataParser, SkillMetadata
from mini_deerflow.skill import MarkdownSkillParser
from mini_deerflow.skill_registry import SkillRegistry
from mini_deerflow.skill_runtime import DefaultSkillContextProvider, SkillContextProvider
from mini_deerflow.skill_selector import RuleBasedSkillSelector
from mini_deerflow.runtime import RuntimeConfig, create_runtime, load_context_config
from mini_deerflow.thread_state import ThreadState
from mini_deerflow.thread_store import (
    SQLiteThreadStore,
    ThreadAlreadyExistsError,
    ThreadNotFoundError,
    ThreadStore,
)
from mini_deerflow.tools import ToolRegistry, create_default_registry

from mini_deerflow.sandbox_cli import (
    SandboxStatus,
    SandboxStartupError,
    create_sandbox_runtime,
    format_sandbox_status,
    format_tools,
    get_sandbox_status,
    preflight_docker,
    SANDBOX_HELP_ADDITION,
    SANDBOX_NOTE,
)

DEFAULT_DB_PATH = "./mini-deerflow.db"
DEFAULT_SKILLS_DIR = "./skills"

# Sandbox 默认值
DEFAULT_SANDBOX_IMAGE = "python:3.12-slim"
DEFAULT_SANDBOX_TIMEOUT = 30.0
DEFAULT_SANDBOX_MAX_OUTPUT = 1_048_576
DEFAULT_SANDBOX_MAX_READ = 10_485_760
DEFAULT_SANDBOX_MAX_WRITE = 10_485_760


@dataclass
class CLIState:
    """CLI 的可变运行状态：当前 Thread ID。"""
    current_thread_id: str


def format_trace(trace: list[dict]) -> str:
    """将执行轨迹格式化为可读字符串。"""
    lines: list[str] = []
    for entry in trace:
        step = entry["step"]
        action = entry.get("action")
        entry_type = entry.get("type")

        if action == "model_decision":
            lines.append(f"  [步骤 {step}] 模型决策: {entry['detail']}")
        elif action == "tool_call":
            lines.append(
                f"  [步骤 {step}] 工具调用: {entry['detail']['name']}({entry['detail']['args']!r})"
                f" → {entry['detail']['result']}"
            )
        elif action == "final_answer":
            lines.append(f"  [步骤 {step}] 最终回答: {entry['detail']}")
        elif entry_type == "memory_saved":
            lines.append(f"  [步骤 {step}] 已保存长期记忆: {entry['count']} 条")
        elif entry_type == "memory_warning":
            lines.append(f"  [步骤 {step}] 长期记忆处理失败: {entry['error']}")

    return "\n".join(lines)


def format_thread_list(store: ThreadStore, current: str) -> str:
    """列出所有 Thread；当前 Thread 前加 *，其他前加 2 个空格。"""
    lines: list[str] = []
    for t in store.list_threads():
        prefix = "* " if t.thread_id == current else "  "
        lines.append(f"{prefix}{t.thread_id}")
    return "\n".join(lines)


def format_history(thread: ThreadState) -> str:
    """格式化 Thread 的消息历史。空历史返回提示。"""
    snapshot = thread.snapshot()
    if not snapshot:
        return "（空会话）"
    return "\n".join(f"[{m.role}] {m.content}" for m in snapshot)


def format_memories(memories: Sequence[Memory]) -> str:
    """格式化长期记忆列表为空字符串表示无记忆。"""
    if not memories:
        return "暂无长期记忆"

    lines: list[str] = []
    for m in memories:
        lines.append(f"[{m.memory_id}] {m.kind}")
        lines.append(f"内容：{m.content}")
        lines.append(f"来源：{m.source_thread_id}")
        lines.append("")  # 空行分隔

    return "\n".join(lines).strip()


def format_skill_list(metadata: Sequence[SkillMetadata]) -> str:
    """格式化 Skill 列表为空字符串表示无可用 Skill。

    纯函数：不读取文件、不调用 Loader、不加载 instructions。
    按 name 升序排序保证输出稳定。
    """
    if not metadata:
        return "暂无可用 Skill"

    # 按 name 升序排序保证输出稳定
    sorted_metadata = sorted(metadata, key=lambda m: m.name)

    lines = ["可用 Skills："]
    for m in sorted_metadata:
        lines.append(f"- {m.name}：{m.description}")

    lines.append("")
    lines.append("使用方式：在消息中输入 @skill-name")

    return "\n".join(lines)


def format_skill_metadata(metadata: SkillMetadata) -> str:
    """格式化单个 Skill 元数据。

    纯函数：不显示 instructions、不显示 source_path。
    """
    return (
        f"Skill: {metadata.name}\n"
        f"用途：{metadata.description}\n"
        f"使用：@{metadata.name}"
    )


def create_skill_runtime(
    skills_dir: Path,
) -> tuple[SkillCatalog, DefaultSkillContextProvider]:
    """创建 Skill 运行时组件（装配模式）。

    渐进式披露：
    - 启动时只加载 Front Matter 到 Catalog
    - Registry 初始为空
    - 用户通过 @skill-name 指定后才完整加载

    目录不存在或为空时返回空 Catalog。
    解析失败时异常原样传播。
    """
    # 1. 使用 MetadataParser 只读取 Front Matter
    metadata_parser = MarkdownSkillMetadataParser()
    catalog = SkillCatalog(metadata_parser)

    # 2. 加载目录（不存在或为空目录时合法返回）
    if skills_dir.exists() and skills_dir.is_dir():
        catalog.load_directory(skills_dir)

    # 3. 创建完整解析器用于按需加载
    full_parser = MarkdownSkillParser()
    registry = SkillRegistry(full_parser)

    # 4. 创建选择器（默认 rules 为空，仅支持显式 @skill-name）
    selector = RuleBasedSkillSelector(rules={}, max_skills=3)

    # 5. 创建渐进式加载器
    loader = ProgressiveSkillLoader(parser=full_parser, registry=registry)

    # 6. 创建上下文构建器
    builder = SkillContextBuilder()

    # 7. 创建 Skill 上下文提供者
    provider = DefaultSkillContextProvider(
        catalog=catalog,
        selector=selector,
        loader=loader,
        builder=builder,
    )

    return catalog, provider


HELP_TEXT = (
    "命令:\n"
    "  /new <thread_id>      创建并切换到新 Thread\n"
    "  /switch <thread_id>    切换到已有 Thread\n"
    "  /list                  列出所有 Thread，当前以 * 标记\n"
    "  /history               显示当前 Thread 的消息历史\n"
    "  /delete <thread_id>    删除 Thread（不能删除当前）\n"
    "  /current               显示当前 Thread ID\n"
    "  /memories              查看长期记忆\n"
    "  /forget <memory_id>    删除一条长期记忆\n"
    "  /skills                查看可用 Skills\n"
    "  /skill <name>          查看 Skill 元数据\n"
    "  /tools                 查看当前可用工具\n"
    "  /sandbox               查看 Sandbox 状态\n"
    "  /help                  显示本帮助\n"
    "\n"
    "在普通消息中使用 @skill-name 启用 Skill\n"
)


def handle_command(
    raw: str,
    state: CLIState,
    store: ThreadStore,
    agent: Agent,
    memory_store: "SQLiteMemoryStore | None" = None,
    skill_catalog: "SkillCatalog | None" = None,
    sandbox_status: "SandboxStatus | None" = None,
    registry: "ToolRegistry | None" = None,
    context_assembler: object | None = None,
) -> None:
    """处理以 / 开头的命令。"""
    parts = raw.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/help":
        print(HELP_TEXT)
        if sandbox_status is not None:
            print(SANDBOX_NOTE)

    elif cmd == "/tools":
        if registry is None:
            print("工具注册表不可用")
            return
        print(format_tools(registry.list_tools()))

    elif cmd == "/sandbox":
        if sandbox_status is None:
            print("Sandbox 状态不可用")
            return
        print(format_sandbox_status(sandbox_status))

    elif cmd == "/new":
        if not arg:
            print("用法：/new <thread_id>")
            return
        try:
            store.create(arg)
        except ThreadAlreadyExistsError:
            print(f"线程已存在: {arg}")
            return
        state.current_thread_id = arg
        print(f"已创建并切换到：{arg}")

    elif cmd == "/switch":
        if not arg:
            print("用法：/switch <thread_id>")
            return
        if not store.exists(arg):
            print(f"未知线程: {arg}")
            return
        state.current_thread_id = arg
        print(f"已切换到：{arg}")

    elif cmd == "/list":
        print(format_thread_list(store, state.current_thread_id))

    elif cmd == "/history":
        try:
            thread = store.get(state.current_thread_id)
        except ThreadNotFoundError:
            print(f"未知线程: {state.current_thread_id}")
            return
        print(format_history(thread))

    elif cmd == "/delete":
        if not arg:
            print("用法：/delete <thread_id>")
            return
        if arg == state.current_thread_id:
            print("不能删除当前 Thread，请先 /switch 到其他 Thread")
            return
        try:
            store.delete(arg)
        except ThreadNotFoundError:
            print(f"未知线程: {arg}")
            return
        # 24：事实源删成功后同步回收 ContextState 与向量索引。
        # 回收失败**不能仍然打印「已删除」**——那会让用户以为全清了，
        # 而共享 store 里还留着指向已消失线程的孤儿数据。
        drop = getattr(context_assembler, "drop_thread_projections", None)
        if callable(drop):
            try:
                drop(arg)
            except Exception as exc:
                print(f"线程 {arg} 已删除，但上下文数据清理未完成：{type(exc).__name__}: {exc}")
                return
        print(f"已删除: {arg}")

    elif cmd == "/current":
        print(state.current_thread_id)

    elif cmd == "/memories":
        if memory_store is None:
            print("长期记忆未启用")
            return
        try:
            memories = memory_store.list_memories()
            print(format_memories(memories))
        except Exception as e:
            print(f"查询长期记忆失败: {e}")

    elif cmd == "/forget":
        if not arg:
            print("用法：/forget <memory_id>")
            return
        if memory_store is None:
            print("长期记忆未启用")
            return
        try:
            memory_store.delete(arg)
            print(f"已删除长期记忆: {arg}")
        except MemoryNotFoundError:
            print(f"未找到长期记忆: {arg}")

    elif cmd == "/skills":
        if skill_catalog is None:
            print("Skill 功能未启用")
            return
        metadata = skill_catalog.list_metadata()
        print(format_skill_list(metadata))

    elif cmd == "/skill":
        if not arg:
            print("用法：/skill <name>")
            return
        if skill_catalog is None:
            print("Skill 功能未启用")
            return
        try:
            metadata = skill_catalog.get(arg)
            print(format_skill_metadata(metadata))
        except SkillNotFoundError:
            print(f"未找到 Skill: {arg}")

    else:
        print(f"未知命令: {cmd}")


def create_store(database_path: str) -> SQLiteThreadStore:
    """根据数据库路径创建 SQLiteThreadStore；default 不存在时创建，存在时不覆盖。"""
    store = SQLiteThreadStore(database_path)
    if not store.exists("default"):
        store.create("default")
    return store


def create_memory_store(
    database_path: str,
    max_memories: int | None = None,
) -> SQLiteMemoryStore:
    """根据数据库路径创建 SQLiteMemoryStore。"""
    # 使用与 thread store 相同的数据库文件
    return SQLiteMemoryStore(database_path, max_memories=max_memories)


def run_cli(
    thread_store: SQLiteThreadStore,
    memory_store: SQLiteMemoryStore | None = None,
    trace_enabled: bool = False,
    skill_context_provider: SkillContextProvider | None = None,
    skill_catalog: SkillCatalog | None = None,
    sandbox_status: SandboxStatus | None = None,
    registry: ToolRegistry | None = None,
) -> None:
    """运行交互主循环（不负责 Store 生命周期）。

    始终通过 IntegratedAgent 执行 agent.run，从而走 Middleware 管线
    （Logging / Trace / Retry / Budget），即使未启用 Subagent。
    """
    state = CLIState(current_thread_id="default")

    # 装配长期记忆组件
    context_provider = None
    memory_processor = None

    if memory_store is not None:
        extractor = RuleBasedMemoryExtractor(
            id_factory=lambda: uuid.uuid4().hex,
        )
        deduplicator = ExactMemoryDeduplicator()
        memory_manager = MemoryManager(
            extractor=extractor,
            store=memory_store,
            deduplicator=deduplicator,
        )
        context_builder = MemoryContextBuilder(store=memory_store)
        context_provider = context_builder
        memory_processor = memory_manager

    # 始终走 IntegratedAgent（即使没启用 Subagent）
    # model_settings 走统一 Model Factory（21A）：默认 fake，选 minimax 时
    # 缺 Key 会在 create_model 内 fail-closed 抛错，不静默降级。
    config = RuntimeConfig(
        subagent_enabled=False,
        trace_enabled=trace_enabled,
        model_settings=load_model_settings(),
        # 22：未配置 MINI_DEERFLOW_MCP_CONFIG 时返回 [] → 不启动任何 MCP
        mcp_servers=load_mcp_config(),
        # 24：上下文工程的环境变量（未设置即走代码默认值）
        **load_context_config(),
    )
    ctx_runtime = create_runtime(
        store=thread_store,
        registry=registry or create_default_registry(),
        config=config,
        run_id="cli-main",
    )
    # 注入 Agent 的 memory + skill provider
    agent = ctx_runtime.agent
    agent._context_provider = context_provider
    agent._memory_processor = memory_processor
    agent._skill_context_provider = skill_context_provider
    integrated_agent = ctx_runtime.integrated_agent

    print("Mini DeerFlow（输入 exit 或 quit 退出，输入 /help 查看命令）")

    while True:
        try:
            raw = input(f"\n[{state.current_thread_id}]> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if not raw:
            continue
        if raw.lower() in ("exit", "quit"):
            print("再见！")
            break

        if raw.startswith("/"):
            handle_command(
                raw, state, thread_store, agent, memory_store, skill_catalog,
                sandbox_status, registry, ctx_runtime.context_assembler,
            )
            continue

        answer, trace = integrated_agent.run(state.current_thread_id, raw)

        if trace_enabled:
            print("\n--- 执行轨迹 ---")
            print(format_trace(trace))
            print("--- 结束 ---")

        print(f"\n{answer}")


def main(
    argv: list[str] | None = None,
    thread_store: SQLiteThreadStore | None = None,
    memory_store: SQLiteMemoryStore | None = None,
) -> None:
    """启动交互式命令行。可注入 store 以便测试。"""
    parser = argparse.ArgumentParser(description="Mini DeerFlow 命令行")
    parser.add_argument("--trace", action="store_true", help="显示执行轨迹")
    parser.add_argument(
        "--db",
        default=DEFAULT_DB_PATH,
        help=f"SQLite 数据库路径（默认: {DEFAULT_DB_PATH}）",
    )
    parser.add_argument(
        "--memory-max",
        type=int,
        default=None,
        help="长期记忆最大容量（默认不限制）",
    )
    parser.add_argument(
        "--skills-dir",
        type=str,
        default=None,
        help=f"Skills 目录路径（默认: {DEFAULT_SKILLS_DIR}）",
    )
    # --- Sandbox 参数 ---
    parser.add_argument(
        "--sandbox-root",
        type=str,
        default=None,
        help="Sandbox Workspace 根目录路径（启用 Sandbox）",
    )
    parser.add_argument(
        "--sandbox-image",
        type=str,
        default=DEFAULT_SANDBOX_IMAGE,
        help=f"Sandbox Docker 镜像（默认: {DEFAULT_SANDBOX_IMAGE}）",
    )
    parser.add_argument(
        "--sandbox-timeout",
        type=float,
        default=DEFAULT_SANDBOX_TIMEOUT,
        help=f"命令执行超时秒数（默认: {DEFAULT_SANDBOX_TIMEOUT}）",
    )
    parser.add_argument(
        "--sandbox-max-output",
        type=int,
        default=DEFAULT_SANDBOX_MAX_OUTPUT,
        help=f"命令输出最大字节数（默认: {DEFAULT_SANDBOX_MAX_OUTPUT}）",
    )
    parser.add_argument(
        "--sandbox-max-read",
        type=int,
        default=DEFAULT_SANDBOX_MAX_READ,
        help=f"文件读取最大字节数（默认: {DEFAULT_SANDBOX_MAX_READ}）",
    )
    parser.add_argument(
        "--sandbox-max-write",
        type=int,
        default=DEFAULT_SANDBOX_MAX_WRITE,
        help=f"文件写入最大字节数（默认: {DEFAULT_SANDBOX_MAX_WRITE}）",
    )
    args = parser.parse_args(argv)

    # 确保数据库目录存在
    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # 创建 Skill 运行时（与 Sandbox 无关，失败不影响 Sandbox 决策）
    skill_catalog = None
    skill_context_provider = None

    skills_dir = Path(args.skills_dir) if args.skills_dir else Path(DEFAULT_SKILLS_DIR)
    if skills_dir.exists():
        if not skills_dir.is_dir():
            raise ValueError(f"Skills 路径不是目录: {skills_dir}")
        skill_catalog, skill_context_provider = create_skill_runtime(skills_dir)
    else:
        from mini_deerflow.skill_metadata import MarkdownSkillMetadataParser
        skill_catalog = SkillCatalog(MarkdownSkillMetadataParser())

    # --- Sandbox 装配（fail-closed） ---
    # 默认关闭：未提供 --sandbox-root 时完全不访问 Docker
    sandbox_status: SandboxStatus | None = None
    registry: ToolRegistry = create_default_registry()

    if args.sandbox_root:
        # 步骤 1：验证路径
        root_path = Path(args.sandbox_root).resolve()
        if not root_path.exists():
            raise SandboxStartupError(
                f"Sandbox Workspace 路径不存在: {root_path}"
            )
        if not root_path.is_dir():
            raise SandboxStartupError(
                f"Sandbox Workspace 路径不是目录: {root_path}"
            )

        # 步骤 2：创建运行时（内部会定位 docker.exe）
        files, docker_runner, local_runner, tool_set = create_sandbox_runtime(
            root=root_path,
            image=args.sandbox_image,
            timeout_seconds=args.sandbox_timeout,
            max_output_bytes=args.sandbox_max_output,
            max_read_bytes=args.sandbox_max_read,
            max_write_bytes=args.sandbox_max_write,
        )

        # 步骤 3：注册工具
        from mini_deerflow.sandbox_tools import register_sandbox_tools
        register_sandbox_tools(registry, tool_set)

        # 步骤 4：Docker 预检（使用 local_runner，不使用 docker_runner）
        preflight_docker(local_runner, args.sandbox_image)

        # 步骤 5：构建状态
        sandbox_status = get_sandbox_status(
            enabled=True,
            docker_runner=docker_runner,
            timeout_seconds=args.sandbox_timeout,
            max_output_bytes=args.sandbox_max_output,
            max_read_bytes=args.sandbox_max_read,
            max_write_bytes=args.sandbox_max_write,
        )
        print(f"Sandbox 已启用: Workspace={args.sandbox_root}")

    # 创建 Stores（在 Sandbox 验证通过之后）
    if thread_store is None:
        thread_store = create_store(args.db)

    if memory_store is None:
        memory_store = create_memory_store(args.db, max_memories=args.memory_max)

    try:
        run_cli(
            thread_store,
            memory_store,
            trace_enabled=args.trace,
            skill_context_provider=skill_context_provider,
            skill_catalog=skill_catalog,
            sandbox_status=sandbox_status,
            registry=registry,
        )
    finally:
        if memory_store is not None:
            memory_store.close()
        if thread_store is not None:
            thread_store.close()


if __name__ == "__main__":
    main()