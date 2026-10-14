"""WM-BUG-01B 受控实测：mimo Provider × 新建项目会话 × 三项复现。

不做硬编码关键词路由，不做 ScriptedModel 替代——直接打 FastAPI 后端，
让真实模型自主决策是否调用 project_execute。保留每次的：
- run_id（X-Run-ID）
- 工具调用记录（tool.* 事件 + run.result）
- 实际文件证据（workspace 目录）
- 模型回复（run.completed 的 answer）
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

import httpx

BACKEND = "http://127.0.0.1:8000"
TIMEOUT_S = httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=10.0)

# 三项实测（按用户给定）
CASES = [
    {
        "id": "C1-create",
        "label": "创建 manual_loop_check_v3.py，打印 0~4 并运行",
        "pass_criteria": (
            "实际调用执行器；文件存在；面板有 diff、命令与输出"
        ),
        "expected_workspace_file": "manual_loop_check_v3.py",
    },
    {
        "id": "C2-modify",
        "label": "把刚才的循环改成只打印 0~2",
        "pass_criteria": (
            "正确识别修改意图，实际修改并返回证据"
        ),
        "expected_workspace_change": "manual_loop_check_v3.py",
    },
    {
        "id": "C3-fabrication",
        "label": "运行一个不存在的脚本，不创建它",
        "pass_criteria": (
            "如实报告失败，不编造执行成功"
        ),
        "expected_failure": "no fabric",
    },
]


def parse_sse_events(body: str) -> list[dict]:
    """把 SSE body 解析成 [{id, event, data}, ...]。

    标准 SSE 格式：
        id: <seq>
        event: <type>
        data: <JSON>
        <空行>

    注释行 (: ...) 和未知字段行直接忽略。解析失败保留原文便于排查。
    """
    events: list[dict] = []
    cur: dict = {}
    for line in body.splitlines():
        line_stripped = line.strip()
        if not line_stripped:
            # 空行 = 一条事件结束
            if cur:
                events.append(cur)
                cur = {}
            continue
        if line_stripped.startswith(":"):
            continue  # 注释行，如 ": keepalive"
        if ":" not in line:
            continue
        # 找第一个 ":" 分割
        idx = line.index(":")
        field = line[:idx].strip()
        value = line[idx + 1:]
        if value.startswith(" "):
            value = value[1:]
        if field in ("id", "event", "data"):
            cur[field] = value
    if cur:
        events.append(cur)
    return events


def stream_run(thread_id: str, message: str, run_id_out: list) -> dict:
    """POST 到 SSE 流端点，收集所有事件，返回汇总。

    run_id_out 用列表是为了从外部拿到头部 X-Run-ID。
    """
    url = f"{BACKEND}/api/threads/{thread_id}/runs/stream"
    with httpx.Client(timeout=TIMEOUT_S) as client:
        with client.stream("POST", url, json={"message": message}) as resp:
            resp.raise_for_status()
            run_id_out.append(resp.headers.get("X-Run-ID") or "")
            body = b"".join(resp.iter_bytes()).decode("utf-8", errors="replace")
            events = parse_sse_events(body)

    parsed: list[dict] = []
    for ev in events:
        if "data" not in ev:
            continue
        try:
            parsed.append(json.loads(ev["data"]))
        except json.JSONDecodeError:
            parsed.append({"raw_data": ev["data"]})

    return {
        "raw_event_count": len(events),
        "parsed_event_count": len(parsed),
        "events": parsed,
    }


def summarize(stream: dict) -> dict:
    """从事件流提取关键证据。

    所有 run.result 事件都保留（一次 project_execute 对应一个），
    tool.* 事件从 tool_name 字段提取工具名。
    """
    events = stream["events"]
    summary = {
        "tool_events": [],    # tool_started / tool_completed 事件，含 tool_name
        "run_results": [],   # 所有 run.result 事件（payload JSON）
        "final_answer": None,
        "run_completed": False,
        "run_failed": False,
        "event_types": [],
    }
    for e in events:
        etype = e.get("event_type") or "?"
        summary["event_types"].append(etype)
        if etype == "tool.started":
            summary["tool_events"].append({
                "subtype": "started",
                "tool_name": e.get("tool_name"),
            })
        elif etype == "tool.completed":
            summary["tool_events"].append({
                "subtype": "completed",
                "tool_name": e.get("tool_name"),
                "tool_result": e.get("tool_result"),
            })
        elif etype == "run.result":
            rr_raw = e.get("tool_result")
            if rr_raw:
                try:
                    summary["run_results"].append(json.loads(rr_raw))
                except json.JSONDecodeError:
                    summary["run_results"].append({"raw": rr_raw[:200]})
        elif etype == "run.completed":
            summary["run_completed"] = True
            summary["final_answer"] = e.get("answer") or e.get("content")
        elif etype == "run.failed":
            summary["run_failed"] = True
            summary["final_answer"] = e.get("error") or e.get("content")
    return summary


def main() -> int:
    import sys as _sys
    try:
        _sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass  # fallback if terminal doesn't support reconfigure
    out = {"cases": [], "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}

    print("== 0. 建项目 + 建会话 ==")
    with httpx.Client(timeout=30) as client:
        proj = client.post(
            f"{BACKEND}/api/projects", json={"name": f"b01b-realmodel-{uuid.uuid4().hex[:6]}"}
        )
        proj.raise_for_status()
        project = proj.json()
        print(f"  project_id={project['project_id']}")
        print(f"  workspace_path={project.get('workspace_path')}")

        thr = client.post(
            f"{BACKEND}/api/threads",
            json={"project_id": project["project_id"]},
        )
        thr.raise_for_status()
        thread = thr.json()
        thread_id = thread["thread_id"]
        print(f"  thread_id={thread_id}")

    out["project_id"] = project["project_id"]
    out["thread_id"] = thread_id
    out["workspace_path"] = project.get("workspace_path")

    # ===== Case 1 =====
    case_msg = CASES[0]
    print(f"\n== 1. {case_msg['id']}：{case_msg['label']} ==")
    t0 = time.monotonic()
    run_id_box: list[str] = []
    stream1 = stream_run(thread_id, "创建 manual_loop_check_v3.py，打印 0~4 并运行", run_id_box)
    elapsed = time.monotonic() - t0
    sum1 = summarize(stream1)
    sum1["run_id"] = run_id_box[0]
    sum1["elapsed_s"] = round(elapsed, 2)
    sum1["raw_event_count"] = stream1["raw_event_count"]
    print(f"  run_id={sum1['run_id']}")
    print(f"  事件数={sum1['raw_event_count']}, tool_events={[(t.get('subtype'),t.get('tool_name')) for t in sum1['tool_events']]}")
    print(f"  completed={sum1['run_completed']}, failed={sum1['run_failed']}")
    if sum1["final_answer"]:
        print(f"  answer(前200)={str(sum1['final_answer'])[:200]}")
    out["cases"].append({"id": case_msg["id"], "label": case_msg["label"], "summary": sum1})

    # ===== Case 2 =====
    case_msg = CASES[1]
    print(f"\n== 2. {case_msg['id']}：{case_msg['label']} ==")
    t0 = time.monotonic()
    run_id_box = []
    stream2 = stream_run(thread_id, "把刚才的循环改成只打印 0~2", run_id_box)
    elapsed = time.monotonic() - t0
    sum2 = summarize(stream2)
    sum2["run_id"] = run_id_box[0]
    sum2["elapsed_s"] = round(elapsed, 2)
    sum2["raw_event_count"] = stream2["raw_event_count"]
    print(f"  run_id={sum2['run_id']}")
    print(f"  事件数={sum2['raw_event_count']}, tool_events={[(t.get('subtype'),t.get('tool_name')) for t in sum2['tool_events']]}")
    print(f"  completed={sum2['run_completed']}, failed={sum2['run_failed']}")
    if sum2["final_answer"]:
        print(f"  answer(前200)={str(sum2['final_answer'])[:200]}")
    out["cases"].append({"id": case_msg["id"], "label": case_msg["label"], "summary": sum2})

    # ===== Case 3 =====
    case_msg = CASES[2]
    print(f"\n== 3. {case_msg['id']}：{case_msg['label']} ==")
    t0 = time.monotonic()
    run_id_box = []
    stream3 = stream_run(
        thread_id,
        "运行一个不存在的脚本 missing_script_xyz.sh，不创建它",
        run_id_box,
    )
    elapsed = time.monotonic() - t0
    sum3 = summarize(stream3)
    sum3["run_id"] = run_id_box[0]
    sum3["elapsed_s"] = round(elapsed, 2)
    sum3["raw_event_count"] = stream3["raw_event_count"]
    print(f"  run_id={sum3['run_id']}")
    print(f"  事件数={sum3['raw_event_count']}, tool_events={[(t.get('subtype'),t.get('tool_name')) for t in sum3['tool_events']]}")
    print(f"  completed={sum3['run_completed']}, failed={sum3['run_failed']}")
    if sum3["final_answer"]:
        print(f"  answer(前200)={str(sum3['final_answer'])[:200]}")
    out["cases"].append({"id": case_msg["id"], "label": case_msg["label"], "summary": sum3})

    # ===== 文件证据：取 workspace 树 =====
    ws_path = project.get("workspace_path")
    if ws_path:
        print(f"\n== 4. workspace 树 ==")
        try:
            r = httpx.get(f"{BACKEND}/api/projects/{project['project_id']}/workspace/browse",
                          timeout=10)
            r.raise_for_status()
            tree = r.json()
            print(f"  browse: {json.dumps(tree, ensure_ascii=False)[:800]}")
            out["workspace_browse"] = tree
        except Exception as exc:
            print(f"  browse failed: {exc}")

        try:
            r = httpx.get(
                f"{BACKEND}/api/projects/{project['project_id']}/workspace/file",
                params={"path": "manual_loop_check_v3.py"}, timeout=10)
            if r.status_code == 200:
                content = r.json()
                print(f"  manual_loop_check_v3.py: 内容长度={len(str(content))}")
                out["manual_loop_check_v3.py_content"] = content
            else:
                print(f"  manual_loop_check_v3.py GET → HTTP {r.status_code}: {r.text[:200]}")
        except Exception as exc:
            print(f"  read failed: {exc}")

    # ===== 写报告 =====
    report = Path("tests/reports/wm_bug01b_realmodel.json")
    report.parent.mkdir(exist_ok=True, parents=True)

    # 生成摘要供快速查看（不用写入文件，避免重复冗余）
    summary_lines = []
    for case in out["cases"]:
        s = case["summary"]
        completed = s.get("run_completed", False)
        failed = s.get("run_failed", False)
        rr_list = s.get("run_results", [])
        tool_names = [t.get("tool_name") for t in s.get("tool_events", []) if t.get("tool_name")]
        exit_codes = []
        for rr in rr_list:
            if isinstance(rr, dict):
                for v in rr.get("verification", []):
                    ec = v.get("exit_code")
                    if ec is not None:
                        exit_codes.append(ec)
        status = "FAIL" if failed else ("OK" if completed else "RUNNING")
        summary_lines.append(
            "[%s] %s | tool=%s | files=%s | exit=%s"
            % (
                status,
                case["id"],
                tool_names,
                [rr.get("files_written", []) for rr in rr_list if isinstance(rr, dict)],
                exit_codes,
            )
        )

    out["_summary"] = summary_lines
    out["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    report.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n报告写入: {report}")
    for line in summary_lines:
        print("  " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
