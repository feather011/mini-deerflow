"""诊断 MiMo API 401：测试不同端点和认证头的组合。不打印 Key。"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("MIMO_API_KEY", "")
BASE = "https://api.xiaomimimo.com"


def post(url: str, headers: dict[str, str], payload: dict) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
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
    (
        "A: /anthropic + x-api-key (Anthropic标准)",
        f"{BASE}/anthropic/v1/messages",
        {"x-api-key": API_KEY, "anthropic-version": "2023-06-01"},
        {"model": "mimo-v2.6-flash", "max_tokens": 32,
         "messages": [{"role": "user", "content": "hi"}]},
    ),
    (
        "B: /anthropic + Authorization Bearer",
        f"{BASE}/anthropic/v1/messages",
        {"Authorization": f"Bearer {API_KEY}"},
        {"model": "mimo-v2.6-flash", "max_tokens": 32,
         "messages": [{"role": "user", "content": "hi"}]},
    ),
    (
        "C: /v1/chat/completions + Bearer (OpenAI格式)",
        f"{BASE}/v1/chat/completions",
        {"Authorization": f"Bearer {API_KEY}"},
        {"model": "mimo-v2.6-flash", "max_tokens": 32,
         "messages": [{"role": "user", "content": "hi"}]},
    ),
    (
        "D: /anthropic/messages 无 /v1",
        f"{BASE}/anthropic/messages",
        {"x-api-key": API_KEY, "anthropic-version": "2023-06-01"},
        {"model": "mimo-v2.6-flash", "max_tokens": 32,
         "messages": [{"role": "user", "content": "hi"}]},
    ),
]


def main() -> None:
    print(f"Key 长度: {len(API_KEY)}")
    for name, url, headers, payload in CASES:
        status, body = post(url, headers, payload)
        print(f"\n[{name}]")
        print(f"  HTTP {status}")
        print(f"  {body}")


if __name__ == "__main__":
    main()
