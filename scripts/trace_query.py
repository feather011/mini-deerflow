"""按 run_id 查询 Trace JSONL 文件，打印这次运行的调用链。

用法::

    # 列出文件里所有 run_id
    python scripts/trace_query.py /path/to/trace.jsonl --list-runs

    # 打印某次运行的调用链（按 parent_id 还原层级）
    python scripts/trace_query.py /path/to/trace.jsonl --run-id run-1a2b3c4d

    # 只看某类终态（例如排障超时）
    python scripts/trace_query.py /path/to/trace.jsonl --run-id run-1a2b3c4d --status timeout

Trace 文件由 Web 后端开启 Trace 后落盘（MINI_DEERFLOW_TRACE_PATH），
或由 CLI / 运行时用 `RuntimeConfig(trace_enabled=True, trace_path=...)` 指定。

文件里一条 span 有两条记录（running + 终态，共用同一个 event_id）；
手工发的事件（上下文事件、子代理事件）一条即终态。
本脚本按 event_id 分组，取终态记录，还原父子层级。

**只诊断、不修复**：文件内容永远不被修改。遇到异常数据时保留原始记录，
在 stderr 给出警告：

- 同一 event_id 有多条终态（同一 span 被写了多个终态）；
- `parent_id` 构成环（环上的 span 不在任何根链上，直接遍历会被丢弃）。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

TERMINAL_STATUSES = {"success", "error", "cancelled", "timeout"}


def _load(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(f"[警告] 第 {lineno} 行不是合法 JSON，已跳过：{exc}", file=sys.stderr)
    return events


def _terminals(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """每个 event_id 只留终态记录（没有终态的按原样保留，便于发现悬空）。

    **重复终态要警告**：同一 span 同时被记成 success 和 error 是真实存在的
    缺陷信号（终态写了两次），静默取第一条会把它藏起来。
    本工具**只诊断不修复**：文件内容不动，显示时保留第一条终态。
    """
    by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        by_id[str(event.get("event_id"))].append(event)

    result: list[dict[str, Any]] = []
    for event_id, records in by_id.items():
        terminal = [r for r in records if r.get("status") in TERMINAL_STATUSES]
        if len(terminal) > 1:
            statuses = ", ".join(str(r.get("status")) for r in terminal)
            print(
                f"[警告] event_id={event_id} 有 {len(terminal)} 条终态记录（{statuses}）"
                "—— 同一 span 被写了多个终态；文件内容未修改，显示时保留第一条。",
                file=sys.stderr,
            )
        result.append(terminal[0] if terminal else records[0])
    return result


def _find_cycles(events: list[dict[str, Any]]) -> list[list[str]]:
    """找出 `parent_id` 构成的环；每个环返回一条 event_id 路径。

    成环的 span 不在任何根链上，直接遍历会**被静默丢弃** —— 那正是
    「调用链不可信」最需要暴露的情形，所以要单独找出来并警告。
    """
    parent: dict[str, str | None] = {}
    for event in events:
        raw_parent = event.get("parent_id")
        parent[str(event.get("event_id"))] = (
            str(raw_parent) if raw_parent is not None else None
        )

    cycles: list[list[str]] = []
    done: set[str] = set()
    for start in parent:
        if start in done:
            continue
        path: list[str] = []
        index: dict[str, int] = {}
        cur: str | None = start
        while cur is not None and cur in parent and cur not in done and cur not in index:
            index[cur] = len(path)
            path.append(cur)
            cur = parent[cur]
        if cur is not None and cur in index:
            cycles.append(path[index[cur]:])
        done.update(path)
    return cycles


def _print_tree(events: list[dict[str, Any]], *, only_status: str | None) -> None:
    children: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
    known_ids = {str(e.get("event_id")) for e in events}
    for event in events:
        parent = event.get("parent_id")
        if parent is not None and str(parent) not in known_ids:
            parent = None  # 父节点不在本次筛选结果里 → 当作根
        children[str(parent) if parent is not None else None].append(event)

    cycles = _find_cycles(events)
    cycle_ids = {eid for cycle in cycles for eid in cycle}
    for cycle in cycles:
        ring = " → ".join(cycle + [cycle[0]])
        print(
            f"[警告] 检测到父子引用成环：{ring}"
            "（相关 span 仍会显示，未做修复）",
            file=sys.stderr,
        )

    printed: set[str] = set()

    def walk(parent: str | None, depth: int, on_path: frozenset[str]) -> None:
        for event in children.get(parent, []):
            event_id = str(event.get("event_id"))
            if event_id in on_path:
                # 回到当前路径上已有的 span —— 成环。不再下钻，避免无限递归。
                print(f"{'  ' * depth}- ↺ 回到已显示的 span（成环）：{event_id}")
                continue
            printed.add(event_id)
            status = event.get("status")
            duration = event.get("duration")
            detail = event.get("error_detail")
            line = (
                f"{'  ' * depth}- {event.get('operation')}/{event.get('name')}"
                f" [{status}]"
                f" id={event.get('event_id')}"
            )
            if duration is not None:
                line += f" {duration:.3f}s"
            if event.get("error_type"):
                line += f" error={event.get('error_type')}"
            if detail:
                line += f" detail={detail}"
            if event.get("kind") == "context":
                line += " (context)"
            if event_id in cycle_ids:
                line += " (循环)"
            print(line)
            walk(event_id, depth + 1, on_path | {event_id})

    walk(None, 0, frozenset())

    # 不在任何父链上的 span（孤儿 / 环）：**不能静默丢掉** —— 仍要显示。
    remaining = [e for e in events if str(e.get("event_id")) not in printed]
    if remaining:
        print(f"[提示] {len(remaining)} 个 span 不在任何父链上（孤儿或成环），单独列出：")
        for event in remaining:
            print(
                f"- {event.get('operation')}/{event.get('name')}"
                f" [{event.get('status')}]"
                f" id={event.get('event_id')}"
                f" parent={event.get('parent_id')}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按 run_id 查询 Trace JSONL")
    parser.add_argument("path", help="Trace JSONL 文件路径")
    parser.add_argument("--run-id", help="只看这一次运行的 span")
    parser.add_argument("--status", help="只显示该终态的 span（success/error/cancelled/timeout）")
    parser.add_argument("--list-runs", action="store_true", help="列出文件里所有 run_id")
    args = parser.parse_args(argv)

    path = Path(args.path)
    if not path.exists():
        print(f"文件不存在：{path}", file=sys.stderr)
        return 2

    events = _load(path)
    if not events:
        print("文件为空（没有 Trace 记录）")
        return 0

    if args.list_runs:
        run_ids: dict[str, int] = defaultdict(int)
        for event in events:
            run_ids[str(event.get("run_id"))] += 1
        for run_id, count in sorted(run_ids.items()):
            print(f"{run_id}\t{count} 条记录")
        return 0

    if not args.run_id:
        parser.error("需要 --run-id 或 --list-runs")

    selected = [e for e in events if str(e.get("run_id")) == args.run_id]
    if not selected:
        print(f"没有 run_id={args.run_id} 的记录", file=sys.stderr)
        return 1

    terminals = _terminals(selected)
    if args.status:
        terminals = [e for e in terminals if e.get("status") == args.status]

    print(f"run_id={args.run_id}：{len(selected)} 条记录 / {len(terminals)} 个 span")
    _print_tree(terminals, only_status=args.status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
