"""F1：新增文件必须带 diff 正文，且 unified diff 的换行不得粘连。

背景（浏览器人工验收发现）：

- `_diff_snapshots` 的 added 分支只给 `path` / `status`，**没有** `unified_diff`，
  前端「变化」页签因此显示 `hello.c +0 −0`，看不到任何新增内容。
- `_compute_diff` 用 `lineterm=""` + `"".join` 拼接：`difflib.unified_diff` 的
  控制行（`---` / `+++` / `@@`）在 `lineterm=""` 时**不带结尾换行**，直接拼接
  会把文件头、hunk 头与首行内容粘成一行 —— 前端按行解析时整段 diff 丢失。
  因此只补字段是不够的，必须同时修正换行拼接。
"""

from pathlib import Path

from mini_deerflow.project_tools import (
    _compute_diff,
    _diff_snapshots,
    _snapshot_workspace,
)


def _added_diff(root: Path, name: str, content: str) -> dict:
    before = _snapshot_workspace(root)
    (root / name).write_text(content, encoding="utf-8")
    after = _snapshot_workspace(root)
    files = _diff_snapshots(before, after, root)["files"]
    assert len(files) == 1
    return files[0]


def test_新增文件带完整_diff正文(tmp_path: Path):
    entry = _added_diff(tmp_path, "hello.c", "int main(){return 0;}\n")

    assert entry["status"] == "added"
    assert entry["diff_truncated"] is False
    unified = entry["unified_diff"]
    assert "+int main(){return 0;}" in unified.split("\n")


def test_diff_按行分隔_文件头与首行不粘连(tmp_path: Path):
    """控制行（--- / +++ / @@）必须各占一行，首行内容不能被粘在 hunk 头上。"""
    entry = _added_diff(tmp_path, "hello.c", "line1\nline2\n")

    lines = entry["unified_diff"].split("\n")
    assert lines[0] == "--- /dev/null"
    assert lines[1] == "+++ b/hello.c"
    assert lines[2].startswith("@@ ")
    assert lines[3] == "+line1"
    assert lines[4] == "+line2"


def test_修改文件_diff_按行分隔(tmp_path: Path):
    (tmp_path / "a.py").write_text("line1\nline2\n", encoding="utf-8")
    before = _snapshot_workspace(tmp_path)
    (tmp_path / "a.py").write_text("line1\nCHANGED\n", encoding="utf-8")
    after = _snapshot_workspace(tmp_path)

    entry = _diff_snapshots(before, after, tmp_path)["files"][0]

    assert entry["status"] == "modified"
    lines = entry["unified_diff"].split("\n")
    assert lines[0] == "--- a/a.py"
    assert lines[1] == "+++ b/a.py"
    assert lines[2].startswith("@@ ")
    assert " line1" in lines
    assert "-line2" in lines
    assert "+CHANGED" in lines


def test_diff_超长时截断并置标记(tmp_path: Path):
    long_content = "".join(f"line{i}\n" for i in range(300))
    entry = _added_diff(tmp_path, "big.txt", long_content)

    assert entry["diff_truncated"] is True
    lines = entry["unified_diff"].split("\n")
    # 截断标记独占一行，且以 "\" 开头（diff 元信息惯例，前端解析时跳过）
    assert lines[-1].startswith("\\")


def test_二进制新增文件_不给_diff正文(tmp_path: Path):
    before = _snapshot_workspace(tmp_path)
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01\x02")
    after = _snapshot_workspace(tmp_path)

    entry = _diff_snapshots(before, after, tmp_path)["files"][0]

    assert entry["status"] == "added"
    assert entry["reason"] == "binary file"
    assert "unified_diff" not in entry


def test_compute_diff_空内容不报错():
    result = _compute_diff("", "", fromfile="/dev/null", tofile="b/x")
    assert result["truncated"] is False
