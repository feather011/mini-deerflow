"""对照实验：修复前 / 修复后的 creationflags 是否弹出可见控制台窗口。

做法：按 `HarnessRunner` 的方式，用**同一个解释器**（当前 venv 的
python.exe）拉起一个控制台子进程，分别用两套标志：

- 旧标志 = `CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_NO_WINDOW`
- 新标志 = `CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`

只观察「有没有新出现的可见控制台窗口」，不执行任何外部仓库的代码。

为什么用 venv 的 python.exe 就够：uv venv 的 `Scripts\\python.exe` 是
**转发器**，会再 fork 出真正的解释器；`DETACHED_PROCESS` 只作用在转发器
那一层，第二层解释器自己分配控制台 → 弹出窗口，标题恰是 venv 的 python
路径（与用户看到的完全一致）。
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
GetWindowTextW = user32.GetWindowTextW
GetClassNameW = user32.GetClassNameW
GetWindowThreadProcessId = user32.GetWindowThreadProcessId
IsWindowVisible = user32.IsWindowVisible
EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


def consoles() -> list[tuple[int, bool, str]]:
    out: list[tuple[int, bool, str]] = []

    def cb(hwnd, _lparam):
        cls = ctypes.create_unicode_buffer(256)
        GetClassNameW(hwnd, cls, 256)
        if cls.value == "ConsoleWindowClass":
            title = ctypes.create_unicode_buffer(512)
            GetWindowTextW(hwnd, title, 512)
            pid = wintypes.DWORD()
            GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            out.append((pid.value, bool(IsWindowVisible(hwnd)), title.value))
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return out


def probe(label: str, flags: int) -> bool:
    """拉起一个控制台子进程，返回是否出现**可见**控制台窗口。"""
    before = {pid for pid, _, _ in consoles()}
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(3)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        creationflags=flags,
    )
    time.sleep(1.5)
    new = [w for w in consoles() if w[0] not in before]
    visible = [w for w in new if w[1]]
    print(f"[control] {label}  flags={flags}")
    for pid, vis, title in new:
        print(f"[control]    new console window: pid={pid} visible={vis} title={title!r}")
    if not new:
        print("[control]    (no new console window)")
    proc.kill()
    proc.wait()
    print(f"[control] => {'POPUP' if visible else 'NO-POPUP'}")
    return bool(visible)


def main() -> None:
    print(f"[control] interpreter: {sys.executable}")
    old = (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    new = (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )

    old_popup = probe("OLD (HEAD, 含 DETACHED_PROCESS)", old)
    new_popup = probe("NEW (本次修复)", new)

    print(f"[control] SUMMARY old={'POPUP' if old_popup else 'NO-POPUP'} "
          f"new={'POPUP' if new_popup else 'NO-POPUP'}")
    if old_popup and not new_popup:
        print("[control] VERDICT: 修复有效（旧弹窗、新不弹）")
        return
    print("[control] VERDICT: 未复现预期差异，需人工核查")


if __name__ == "__main__":
    main()
