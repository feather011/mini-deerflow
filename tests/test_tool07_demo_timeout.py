"""TOOL-07 R6：演示脚本 4b（真实 HarnessRunner 子进程超时）的自动化证据。

R6 修订点：旧版 4b 只设空的 `HARNESS_ENV_FILE` 与错误的变量名，
harness 的 `load_config()` 优先读 `MINIMAX_API_KEY` → 子进程很可能选中
父进程的真实 Provider 配置；而且只断言 `ok=false`，启动/配置/协议失败
都会伪装成"真实超时通过"。

新版 4b 的证据（本文件逐条覆盖）：
- 真实终态是 timeout（不是 launch_failed / protocol_error）
- 任务进程被回收（无孤儿）
- 项目锁已释放
- 父进程的真实 Provider 没被选中（子进程无真实凭证 + 走假 LLM）
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

_DEFAULT_HARNESS_REPO = "D:/项目解读/harnessagent"
HARNESS_REPO_ROOT = Path(os.environ.get("HARNESS_REPO_ROOT", _DEFAULT_HARNESS_REPO))
DEMO_PATH = Path(__file__).resolve().parents[1] / "scripts" / "tool07_demo.py"

pytestmark = pytest.mark.skipif(
    not (HARNESS_REPO_ROOT / "harness" / "noninteractive.py").exists(),
    reason=(
        f"harness 仓库不存在或结构不符: {HARNESS_REPO_ROOT}"
        f"（设 HARNESS_REPO_ROOT 指向本地副本）"
    ),
)


def _load_demo():
    spec = importlib.util.spec_from_file_location("tool07_demo_r6", DEMO_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # 让演示脚本用与测试一致的 harness 根目录
    module.HARNESS_ROOT = str(HARNESS_REPO_ROOT)
    return module


def test_演示4b_真实子进程超时_隔离Provider():
    """真实子进程超时演示必须成立（含 Provider 隔离核查）。"""
    module = _load_demo()
    assert module.demo_timeout_real_subprocess() is True, (
        "演示 4b 失败：真实子进程超时链路未通过"
    )
