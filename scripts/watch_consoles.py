"""在真实项目会话跑的同时，持续枚举可见控制台窗口。

用法：python watch_consoles.py <秒数>
输出：期间新出现的 ConsoleWindowClass 窗口（含 owner pid / visible / 标题）
"""

import ctypes
import sys
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
GetWindowTextW = user32.GetWindowTextW
GetClassNameW = user32.GetClassNameW
GetWindowThreadProcessId = user32.GetWindowThreadProcessId
IsWindowVisible = user32.IsWindowVisible
EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


def consoles():
    out = []
    def cb(h, l):
        cls = ctypes.create_unicode_buffer(256)
        GetClassNameW(h, cls, 256)
        if cls.value == "ConsoleWindowClass":
            t = ctypes.create_unicode_buffer(512)
            GetWindowTextW(h, t, 512)
            pid = wintypes.DWORD()
            GetWindowThreadProcessId(h, ctypes.byref(pid))
            out.append((pid.value, bool(IsWindowVisible(h)), t.value))
        return True
    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return out


def image_of(pid):
    import subprocess as sp
    r = sp.run(["powershell", "-NoProfile", "-Command",
                f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').ExecutablePath"],
               capture_output=True, text=True)
    return r.stdout.strip()


def main():
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    before = {p for p, _, _ in consoles()}
    print(f"[watch] baseline console windows: {len(before)}")
    seen = {}
    deadline = time.time() + seconds
    while time.time() < deadline:
        for pid, vis, title in consoles():
            if pid in before:
                continue
            if pid not in seen:
                seen[pid] = (vis, title, image_of(pid))
                print(f"[watch] NEW window pid={pid} visible={vis} title={title!r}")
                print(f"[watch]        image={seen[pid][2]}")
        time.sleep(0.25)
    print(f"[watch] done. new console windows during window: {len(seen)}")
    for pid, (vis, title, img) in seen.items():
        print(f"[watch]   pid={pid} visible={vis} title={title!r} image={img}")
    # 最终结论
    bad = [(pid, v) for pid, (v, _, _) in seen.items() if v]
    print(f"[watch] VISIBLE console windows: {len(bad)}")


if __name__ == "__main__":
    main()
