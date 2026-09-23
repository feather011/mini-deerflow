"""命令行界面。"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from mini_deerflow.agent import Agent
from mini_deerflow.models import FakeModel
from mini_deerflow.thread_state import ThreadState
from mini_deerflow.thread_store import (
    InMemoryThreadStore,
    ThreadAlreadyExistsError,
    ThreadNotFoundError,
    ThreadStore,
)
from mini_deerflow.tools import create_default_registry


@dataclass
class CLIState:
    """CLI 的可变运行状态：当前 Thread ID。"""
    current_thread_id: str


def format_trace(trace: list[dict]) -> str:
    """将执行轨迹格式化为可读字符串。"""
    lines: list[str] = []
    for entry in trace:
        step = entry["step"]
        action = entry["action"]
        detail = entry["detail"]

        if action == "model_decision":
            lines.append(f"  [步骤 {step}] 模型决策: {detail}")
        elif action == "tool_call":
            lines.append(
                f"  [步骤 {step}] 工具调用: {detail['name']}({detail['args']!r})"
                f" → {detail['result']}"
            )
        elif action == "final_answer":
            lines.append(f"  [步骤 {step}] 最终回答: {detail}")

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


HELP_TEXT = (
    "命令:\n"
    "  /new <thread_id>      创建并切换到新 Thread\n"
    "  /switch <thread_id>    切换到已有 Thread\n"
    "  /list                  列出所有 Thread，当前以 * 标记\n"
    "  /history               显示当前 Thread 的消息历史\n"
    "  /delete <thread_id>    删除 Thread（不能删除当前）\n"
    "  /current               显示当前 Thread ID\n"
    "  /help                  显示本帮助"
)


def handle_command(raw: str, state: CLIState, store: ThreadStore, agent: Agent) -> None:
    """处理以 / 开头的命令。"""
    parts = raw.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/help":
        print(HELP_TEXT)

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
        print(f"已删除: {arg}")

    elif cmd == "/current":
        print(state.current_thread_id)

    else:
        print(f"未知命令: {cmd}")


def main() -> None:
    """启动交互式命令行。"""
    parser = argparse.ArgumentParser(description="Mini DeerFlow 命令行")
    parser.add_argument("--trace", action="store_true", help="显示执行轨迹")
    args = parser.parse_args()

    store = InMemoryThreadStore()
    store.create("default")
    state = CLIState(current_thread_id="default")
    agent = Agent(
        model=FakeModel(),
        registry=create_default_registry(),
        store=store,
    )

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
            handle_command(raw, state, store, agent)
            continue

        answer, trace = agent.run(state.current_thread_id, raw)

        if args.trace:
            print("\n--- 执行轨迹 ---")
            print(format_trace(trace))
            print("--- 结束 ---")

        print(f"\n{answer}")


if __name__ == "__main__":
    main()