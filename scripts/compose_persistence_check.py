"""Docker Compose 验收脚本（Phase 23）。

三种模式：

    # A) smoke：全部经 nginx 同源反代，验证生产链路
    uv run python scripts/compose_persistence_check.py smoke http://127.0.0.1:18080

    # B) 持久化：两步执行，中间穿插 `docker compose down && up`
    uv run python scripts/compose_persistence_check.py seed   http://127.0.0.1:18000 > /tmp/state.json
    uv run python scripts/compose_persistence_check.py verify http://127.0.0.1:18000 /tmp/state.json

设计要点：
- 用 Python 发请求而不是 curl：Windows 下 shell 会按本机代码页编码中文，
  直接 `curl -d '{"message":"计算…"}'` 会发出非法 UTF-8，后端报 400
  （实测踩到过，不是后端的问题）
- 全程走 FakeModel（provider 未设置 → 默认 fake），**不消耗真实额度**
- 只打印 thread_id / 角色序列 / 文件名等非敏感信息
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request


PAYLOAD = b"compose-persistence-bytes\n"
RUN_MESSAGE = "计算 (17 + 25) * 3"


def _upload_name() -> str:
    """带时间戳的文件名：卷在 down/up 之间不丢，固定名会撞 409。"""
    return f"compose-persist-{int(time.time())}.txt"


def _request(method: str, url: str, body: bytes | None = None,
             content_type: str | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method)
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _json(method: str, url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    status, raw = _request(method, url, data, "application/json; charset=utf-8")
    if status >= 400:
        raise SystemExit(f"{method} {url} -> HTTP {status}: {raw.decode('utf-8', 'replace')}")
    return json.loads(raw.decode("utf-8"))


def _multipart(url: str, field: str, filename: str, content: bytes) -> int:
    boundary = "----minideerflowacceptance"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
    status, raw = _request(
        "POST", url, body, f"multipart/form-data; boundary={boundary}"
    )
    if status >= 400:
        raise SystemExit(f"POST {url} -> HTTP {status}: {raw.decode('utf-8', 'replace')}")
    return status


def seed(base: str) -> None:
    api = base.rstrip("/") + "/api"
    thread = _json("POST", f"{api}/threads", {"title": "compose-persistence"})
    tid = thread["thread_id"]

    run = _json("POST", f"{api}/threads/{tid}/runs", {"message": RUN_MESSAGE})
    if "126" not in run.get("answer", ""):
        raise SystemExit(f"run 答案未包含 126: {run.get('answer')!r}")

    file_name = _upload_name()
    status = _multipart(f"{api}/files", "file", file_name, PAYLOAD)
    if status not in (200, 201):
        raise SystemExit(f"上传失败 HTTP {status}")

    # 在卷里预置一个 Artifact（Artifacts 由运行产物生成，没有上传接口）
    detail = _json("GET", f"{api}/threads/{tid}")
    state = {
        "thread_id": tid,
        "roles": [m["role"] for m in detail["messages"]],
        "run_answer_has_126": "126" in run["answer"],
        "uploaded_file": file_name,
        "uploaded_bytes": PAYLOAD.decode(),
    }
    json.dump(state, sys.stdout, ensure_ascii=False)


def verify(base: str, state_path: str) -> None:
    api = base.rstrip("/") + "/api"
    with open(state_path, encoding="utf-8") as fh:
        state = json.load(fh)

    failures: list[str] = []

    threads = _json("GET", f"{api}/threads")["threads"]
    if not any(t["thread_id"] == state["thread_id"] for t in threads):
        failures.append(f"Thread {state['thread_id']} 重启后丢失")

    detail = _json("GET", f"{api}/threads/{state['thread_id']}")
    roles = [m["role"] for m in detail["messages"]]
    if roles != state["roles"]:
        failures.append(f"消息角色序列变了: {roles} != {state['roles']}")

    entries = _json("GET", f"{api}/files")["entries"]
    if not any(e["name"] == state["uploaded_file"] for e in entries):
        failures.append(f"上传文件 {state['uploaded_file']} 丢失")

    req = urllib.request.Request(f"{api}/files/{state['uploaded_file']}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.read().decode("utf-8") != state["uploaded_bytes"]:
                failures.append("上传文件内容不一致")
    except urllib.error.HTTPError as exc:
        failures.append(f"下载上传文件失败 HTTP {exc.code}")

    req = urllib.request.Request(f"{api}/artifacts/persist.txt")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.read().decode("utf-8") != "artifact-v1\n":
                failures.append("Artifact 内容不一致")
    except urllib.error.HTTPError as exc:
        failures.append(f"下载 Artifact 失败 HTTP {exc.code}")

    if failures:
        print("持久化验收失败：")
        for item in failures:
            print(f"  - {item}")
        raise SystemExit(1)

    print("持久化验收通过")
    print(f"  Thread          : {state['thread_id']}")
    print(f"  消息角色序列     : {roles}")
    print(f"  上传文件         : {state['uploaded_file']}（内容一致）")
    print("  Artifact         : persist.txt（可下载，内容一致）")


def smoke(base_web: str) -> None:
    """Docker Compose smoke：只走同源 nginx，验证生产链路本身。

    与 seed/verify 的区别：这里访问的是 **web 服务（8080/18080）**，
    所有 API 都经 nginx `/api` 反代——这样才真的验证了「同源代理可用、
    生产无需 CORS、SPA 刷新正常」，而不是绕过 nginx 直连后端。
    """
    api = base_web.rstrip("/") + "/api"
    failures: list[str] = []

    for path, expect in (("/api/health", "ok"), ("/api/ready", "ready")):
        try:
            with urllib.request.urlopen(base_web.rstrip("/") + path, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                if payload.get("status") != expect:
                    failures.append(f"{path} status={payload.get('status')!r}")
        except Exception as exc:  # noqa: BLE001 - smoke 要的是逐条报告
            failures.append(f"{path}: {type(exc).__name__}")

    # SPA 路由刷新：非根路径必须回落到 index.html，而不是 404
    req = urllib.request.Request(base_web.rstrip("/") + "/threads/anything")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            if resp.status != 200 or "<div id=\"root\">" not in body:
                failures.append(f"SPA 回落异常 status={resp.status}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"SPA 回落: {type(exc).__name__}")

    # 经 nginx 反代完成一次真实工具闭环（FakeModel → calculator → 126）
    try:
        thread = _json("POST", f"{api}/threads", {"title": "compose-smoke"})
        run = _json(
            "POST", f"{api}/threads/{thread['thread_id']}/runs",
            {"message": RUN_MESSAGE},
        )
        if "126" not in run.get("answer", ""):
            failures.append(f"经 nginx 的 run 未得到 126: {run.get('answer')!r}")
    except SystemExit as exc:
        failures.append(str(exc))

    # 事件流也要能穿过 nginx（SSE 依赖 proxy_buffering off）
    req = urllib.request.Request(
        f"{api}/threads", method="POST",
        data=json.dumps({"title": "compose-sse"}).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            tid = json.loads(resp.read().decode("utf-8"))["thread_id"]
        req = urllib.request.Request(
            f"{api}/threads/{tid}/runs/stream",
            data=json.dumps({"message": RUN_MESSAGE}).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            text = resp.read().decode("utf-8")
        for marker in ("run.started", "tool.started", "tool.completed", "run.completed"):
            if marker not in text:
                failures.append(f"SSE 缺少事件 {marker}")
        terminals = [m for m in ("run.completed", "run.failed", "run.timed_out",
                                 "run.cancelled") if m in text]
        if len(terminals) != 1:
            failures.append(f"SSE 终态不唯一: {terminals}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"SSE: {type(exc).__name__}")

    if failures:
        print("Compose smoke 失败：")
        for item in failures:
            print(f"  - {item}")
        raise SystemExit(1)

    print("Compose smoke 通过")
    print("  /api/health、/api/ready            : ok / ready")
    print("  SPA 路由 /threads/anything         : 200 + index.html")
    print("  经 nginx 的 run（FakeModel 闭环）  : 答案含 126")
    print("  经 nginx 的 SSE                    : tool.started / tool.completed / 唯一终态")


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "seed":
        seed(argv[2])
        return 0
    if len(argv) == 4 and argv[1] == "verify":
        verify(argv[2], argv[3])
        return 0
    if len(argv) == 3 and argv[1] == "smoke":
        smoke(argv[2])
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
