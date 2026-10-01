"""诊断 MiniMax /anthropic 端点认证：直接用 urllib 测试各认证头组合。不打印 Key。"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from dotenv import load_dotenv

load_dotenv(override=True)

API_KEY = os.getenv("DASHSCOPE_API_KEY") or os.getenv("MIMO_API_KEY", "")
URL = "https://api.minimaxi.com/anthropic/v1/messages"
MODEL = os.getenv("MODEL_NAME") or os.getenv("MIMO_MODEL", "MiniMax-M3")

PAYLOAD = {
    "model": MODEL,
    "max_tokens": 32,
    "messages": [{"role": "user", "content": "hi"}],
}


def post(headers: dict[str, str]) -> tuple[int, str]:
    req = urllib.request.Request(
        URL,
        data=json.dumps(PAYLOAD).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode()[:300]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]
    except Exception as e:
        return 0, str(e)[:300]


CASES = [
    ("A: X-Api-Key", {"X-Api-Key": API_KEY}),
    ("B: x-api-key", {"x-api-key": API_KEY}),
    ("C: Authorization Bearer", {"Authorization": f"Bearer {API_KEY}"}),
    ("D: X-Api-Key + anthropic-version", {"X-Api-Key": API_KEY, "anthropic-version": "2023-06-01"}),
    ("E: x-api-key + anthropic-version", {"x-api-key": API_KEY, "anthropic-version": "2023-06-01"}),
]


def main() -> None:
    print(f"Key 长度: {len(API_KEY)}  模型: {MODEL}")
    for name, headers in CASES:
        status, body = post(headers)
        print(f"\n[{name}] HTTP {status}")
        print(f"  {body}")


if __name__ == "__main__":
    main()
