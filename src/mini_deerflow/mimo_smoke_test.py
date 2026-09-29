"""MiniMax API 连通性验证（Anthropic 兼容接口，不接入 Agent）。"""

from __future__ import annotations

import os

import anthropic
from dotenv import load_dotenv


def main() -> None:
    load_dotenv(override=True)

    api_key = os.getenv("DASHSCOPE_API_KEY") or os.getenv("MIMO_API_KEY")
    base_url = (
        os.getenv("BASE_URL")
        or os.getenv("MIMO_BASE_URL")
        or "https://api.minimaxi.com/anthropic"
    )
    model = (
        os.getenv("MODEL_NAME")
        or os.getenv("MIMO_MODEL")
        or "MiniMax-M3"
    )

    if not api_key:
        raise SystemExit(
            "错误：未找到 DASHSCOPE_API_KEY。\n"
            "请复制 .env.example 为 .env 并填入你的 API Key。"
        )

    print(f"模型: {model}")

    client = anthropic.Anthropic(
        api_key=api_key,
        base_url=base_url,
        default_headers={"X-Api-Key": api_key},
    )

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
