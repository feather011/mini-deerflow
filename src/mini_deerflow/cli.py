"""命令行界面。"""

from __future__ import annotations

import argparse

from mini_deerflow.agent import Agent
from mini_deerflow.models import FakeModel
from mini_deerflow.tools import create_default_registry


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


def main() -> None:
    """启动交互式命令行。"""
    parser = argparse.ArgumentParser(description="Mini DeerFlow 命令行")
    parser.add_argument("--trace", action="store_true", help="显示执行轨迹")
    args = parser.parse_args()

    agent = Agent(model=FakeModel(), registry=create_default_registry())

    print("Mini DeerFlow（输入 exit 或 quit 退出）")

    while True:
        try:
            user_input = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            print("再见！")
            break

        answer, trace = agent.run(user_input)

        if args.trace:
            print("\n--- 执行轨迹 ---")
            print(format_trace(trace))
            print("--- 结束 ---")

        print(f"\n{answer}")
