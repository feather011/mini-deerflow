"""Xiaomi MiMo API 连通性验证（Anthropic 格式端点，不接入 Agent）。"""

from __future__ import annotations

import os

import anthropic
from dotenv import load_dotenv


def main() -> None:
    load_dotenv()

    api_key = os.getenv("MIMO_API_KEY")
    base_url = os.getenv("MIMO_BASE_URL", "https://api.xiaomimimo.com/anthropic")
    model = os.getenv("MIMO_MODEL", "mimo-v2.6-flash")

    if not api_key:
        raise SystemExit(
            "错误：未找到 MIMO_API_KEY。\n"
            "请复制 .env.example 为 .env 并填入你的 API Key。"
        )

    print(f"模型: {model}")

    client = anthropic.Anthropic(api_key=api_key, base_url=base_url)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": "只回复：连接成功"}],
        )
    except Exception as e:
        print(f"\n请求失败: {e}")
        raise SystemExit(1)

    print(f"回复: {response.content[0].text}")


if __name__ == "__main__":
    main()
