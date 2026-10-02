"""Phase 21C 真实模型收口验收（显式执行，不进 pytest）。

刻意只做**两轮最小验收**：

1. 纯文本：`请只回复：MIMO_TEXT_OK`
2. 工具闭环：`请必须使用计算器计算 (17 + 25) * 3，并返回结果。`

第 2 轮通过 FastAPI SSE 走完整生产链路，用以证明：
- 真实模型返回 `ToolCall`
- `calculator` 真实执行
- 工具结果回传模型并收敛出最终答案（含 126）
- SSE 出现 `tool.started` / `tool.completed`
- Thread 最终消息已持久化
- 输出中不含任何 API Key

执行方式（注意：系统环境变量优先于 .env，若 shell 里残留旧的
`MIMO_API_KEY` 会把 .env 的值挡住并导致 401，因此显式 `env -u` 掉它）：

    env -u MIMO_API_KEY uv run python scripts/phase21c_acceptance.py

退出码：0 = 验收通过；1 = 验收失败（会在最后打印失败原因，不含凭据）。
"""

from __future__ import annotations

import json
import re
import sys

from fastapi.testclient import TestClient

from mini_deerflow.api.app import create_app
from mini_deerflow.model_config import load_model_settings
from mini_deerflow.model_factory import create_model
from mini_deerflow.models import FinalAnswer, Message, ToolCall
from mini_deerflow.runtime import RuntimeConfig, create_runtime
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tools import create_default_registry

# 明确的输出脱敏规则：任何形似 API Key 的长串都必须被打码
_KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]{10,}|[A-Za-z0-9]{40,}")

TEXT_PROMPT = "请只回复：MIMO_TEXT_OK"
TOOL_PROMPT = "请必须使用计算器计算 (17 + 25) * 3，并返回结果。"


def scrub(text: str) -> str:
    """脱敏：把任何疑似凭据的长串替换成掩码，防止进入日志/报告。"""
    return _KEY_PATTERN.sub("***REDACTED***", text)


def fail(reason: str) -> int:
    print(f"\n验收失败: {scrub(reason)}")
    return 1


def run_text_acceptance() -> tuple[bool, str]:
    """第 1 轮：纯文本回答。"""
    settings = load_model_settings()
    model = create_model(settings)
    print(f"[1/2] provider={settings.provider} model={settings.model}")
    decision = model.decide([Message(role="user", content=TEXT_PROMPT)])
    if not isinstance(decision, FinalAnswer):
        return False, f"期望 FinalAnswer，实际 {type(decision).__name__}"
    answer = decision.content
    print(f"[1/2] 回答: {scrub(answer)}")
    if "MIMO_TEXT_OK" not in answer:
        return False, f"回答未包含 MIMO_TEXT_OK: {scrub(answer)}"
    return True, answer


def _parse_sse(body: str) -> list[dict]:
    """SSE 文本 → [{id, event, data}]。"""
    messages: list[dict] = []
    msg: dict = {}
    for line in body.splitlines():
        if line.startswith(":"):
            continue
        if line == "":
            if msg:
                messages.append(msg)
                msg = {}
            continue
        if line.startswith("id:"):
            msg["id"] = int(line[3:].strip())
        elif line.startswith("event:"):
            msg["event"] = line[6:].strip()
        elif line.startswith("data:"):
            raw = line[5:].strip()
            try:
                msg["data"] = json.loads(raw)
            except json.JSONDecodeError:
                msg["data_raw"] = raw
    if msg:
        messages.append(msg)
    return messages


def run_tool_acceptance() -> tuple[bool, str, int]:
    """第 2 轮：工具闭环（返回 是否通过 / 说明 / 本轮模型请求次数）。"""
    settings = load_model_settings()
    model = create_model(settings)

    def factory():
        return create_runtime(
            store=InMemoryThreadStore(),
            registry=create_default_registry(),
            config=RuntimeConfig(trace_enabled=True),
            model=model,
        )

    app = create_app(runtime_factory=factory)
    with TestClient(app) as client:
        resp = client.post("/api/threads", json={"title": "21C"})
        if resp.status_code != 201:
            return False, f"创建 Thread 失败: HTTP {resp.status_code}", 0
        thread_id = resp.json()["thread_id"]

        with client.stream(
            "POST",
            f"/api/threads/{thread_id}/runs/stream",
            json={"message": TOOL_PROMPT},
        ) as stream:
            body = "".join(stream.iter_text())

        events = _parse_sse(body)

        persisted = client.get(f"/api/threads/{thread_id}").json()["messages"]

    names = [e["event"] for e in events]
    model_calls = names.count("model.started")
    tool_started = [e for e in events if e["event"] == "tool.started"]
    tool_completed = [e for e in events if e["event"] == "tool.completed"]
    completed = [e for e in events if e["event"] == "run.completed"]
    failed = [e for e in events if e["event"] == "run.failed"]

    print(f"[2/2] 事件序列: {' → '.join(names)}")

    checks: list[str] = []
    if not tool_started:
        checks.append("SSE 缺少 tool.started")
    if not tool_completed:
        checks.append("SSE 缺少 tool.completed")
    if tool_started and tool_completed:
        names_idx = names
        if names_idx.index("tool.started") > names_idx.index("tool.completed"):
            checks.append("tool.started 顺序晚于 tool.completed")

    if not completed:
        detail = failed[0]["data"].get("error", "") if failed else "无终态事件"
        checks.append(f"缺少 run.completed（{scrub(str(detail))}）")

    answer = completed[0]["data"].get("answer", "") if completed else ""
    if answer:
        print(f"[2/2] 最终答案: {scrub(answer)}")
    if "126" not in answer:
        checks.append(f"最终答案未包含 126: {scrub(answer)}")

    tool_name = tool_started[0]["data"].get("tool_name") if tool_started else None
    if tool_name != "calculator":
        checks.append(f"实际执行的工具不是 calculator: {tool_name!r}")

    # 持久化：最终 Thread 应为 user / tool / assistant 三段
    roles = [m["role"] for m in persisted]
    print(f"[2/2] 落库消息: {roles}")
    if roles != ["user", "tool", "assistant"]:
        checks.append(f"落库角色序列异常: {roles}")
    elif "126" not in persisted[1]["content"]:
        checks.append("落库的工具结果未包含 126")

    if checks:
        return False, "；".join(checks), model_calls
    return True, f"工具 {tool_name} 执行 + 两轮收敛，落库 {roles}", model_calls


def main() -> int:
    settings = load_model_settings()
    print(f"provider={settings.provider} model={settings.model} "
          f"base_url={settings.base_url}")
    print("（不输出任何 API Key）\n")

    ok1, info1 = run_text_acceptance()
    if not ok1:
        return fail(f"第 1 轮纯文本: {info1}")

    ok2, info2, model_calls = run_tool_acceptance()
    if not ok2:
        return fail(f"第 2 轮工具闭环: {info2}")

    total = 1 + model_calls
    print("\n验收通过")
    print(f"  第 1 轮 纯文本回答     : {scrub(info1)}")
    print(f"  第 2 轮 工具闭环       : {info2}")
    print(f"  第 2 轮模型请求次数     : {model_calls}（1 次选工具 + 1 次收敛）")
    print(f"  两轮验收合计模型请求    : {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
