"""Verify snapshot and diff functionality."""

import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def test_snapshot_basic():
    """Test basic workspace snapshot."""
    print("\n=== Test: Basic snapshot ===")

    from mini_deerflow.project_tools import _snapshot_workspace, _is_binary_file

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # Create test files
        (root / "hello.py").write_text("print('hello')", encoding="utf-8")
        (root / "readme.md").write_text("# Readme", encoding="utf-8")

        snapshot = _snapshot_workspace(root)

        assert snapshot.get("complete") is True
        assert "hello.py" in snapshot["files"]
        assert "readme.md" in snapshot["files"]
        assert snapshot["files"]["hello.py"]["type"] == "text"
        assert snapshot["files"]["hello.py"]["content"] == "print('hello')"

        print(f"  [PASS] Snapshot complete: {len(snapshot['files'])} files")
        print(f"  [PASS] Files: {list(snapshot['files'].keys())}")


def test_snapshot_binary_detection():
    """Test binary file detection."""
    print("\n=== Test: Binary file detection ===")

    from mini_deerflow.project_tools import _is_binary_file

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # Text file
        text_file = root / "text.txt"
        text_file.write_bytes(b"hello world")
        assert _is_binary_file(text_file) is False

        # Binary file
        bin_file = root / "binary.bin"
        bin_file.write_bytes(b"\x00\x01\x02\x03")
        assert _is_binary_file(bin_file) is True

        print("  [PASS] Binary detection works")


def test_diff_added():
    """Test diff detection for new files."""
    print("\n=== Test: Diff - added files ===")

    from mini_deerflow.project_tools import _snapshot_workspace, _diff_snapshots

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # Before snapshot
        before = _snapshot_workspace(root)

        # Add new file
        (root / "new.py").write_text("print('new')", encoding="utf-8")

        # After snapshot
        after = _snapshot_workspace(root)

        diff = _diff_snapshots(before, after, root)

        added = [f["path"] for f in diff["files"] if f["status"] == "added"]
        assert "new.py" in added

        print(f"  [PASS] Added files detected: {added}")


def test_diff_modified():
    """Test diff detection for modified files."""
    print("\n=== Test: Diff - modified files ===")

    from mini_deerflow.project_tools import _snapshot_workspace, _diff_snapshots

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # Create file
        (root / "hello.py").write_text("print('hello')", encoding="utf-8")

        # Before snapshot
        before = _snapshot_workspace(root)

        # Modify file
        (root / "hello.py").write_text("print('world')", encoding="utf-8")

        # After snapshot
        after = _snapshot_workspace(root)

        diff = _diff_snapshots(before, after, root)

        modified = [f for f in diff["files"] if f["status"] == "modified"]
        assert len(modified) == 1
        assert modified[0]["path"] == "hello.py"
        assert modified[0]["change"] == "content_changed"
        assert "unified_diff" in modified[0]

        print(f"  [PASS] Modified files: {[f['path'] for f in modified]}")


def test_diff_deleted():
    """Test diff detection for deleted files."""
    print("\n=== Test: Diff - deleted files ===")

    from mini_deerflow.project_tools import _snapshot_workspace, _diff_snapshots

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # Create file
        (root / "delete_me.py").write_text("print('delete')", encoding="utf-8")

        # Before snapshot
        before = _snapshot_workspace(root)

        # Delete file
        (root / "delete_me.py").unlink()

        # After snapshot
        after = _snapshot_workspace(root)

        diff = _diff_snapshots(before, after, root)

        deleted = [f["path"] for f in diff["files"] if f["status"] == "deleted"]
        assert "delete_me.py" in deleted

        print(f"  [PASS] Deleted files: {deleted}")


def test_diff_with_existing_changes():
    """Test diff when workspace already has changes before execution."""
    print("\n=== Test: Diff with existing changes ===")

    from mini_deerflow.project_tools import _snapshot_workspace, _diff_snapshots

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # Create existing file
        (root / "existing.py").write_text("# existing", encoding="utf-8")

        # Baseline: existing state
        baseline = _snapshot_workspace(root)

        # Agent makes changes
        (root / "new.py").write_text("print('new')", encoding="utf-8")
        (root / "existing.py").write_text("# modified", encoding="utf-8")

        # After execution snapshot
        after = _snapshot_workspace(root)

        diff = _diff_snapshots(baseline, after, root)

        added = [f["path"] for f in diff["files"] if f["status"] == "added"]
        modified = [f["path"] for f in diff["files"] if f["status"] == "modified"]
        deleted = [f["path"] for f in diff["files"] if f["status"] == "deleted"]

        # Should show only changes made during execution
        assert "new.py" in added
        assert "existing.py" in modified
        # Should NOT show "existing.py" as deleted
        assert "existing.py" not in deleted

        print(f"  [PASS] Only execution changes detected:")
        print(f"    added: {added}")
        print(f"    modified: {modified}")


def test_full_response_structure():
    """Test full project_execute response with changes."""
    print("\n=== Test: Full response structure ===")

    from mini_deerflow.project_tools import ok_response

    body = {
        "ok": True,
        "status": "completed",
        "task_id": "test123",
        "answer": "Done",
        "files_written": ["hello.c"],
        "verification": [
            {
                "command": "gcc hello.c && ./a.out",
                "command_truncated": False,
                "output": "[exit 0] hello world",
                "output_truncated": False,
                "exit_code": 0,
                "duration_s": 1.5,
                "has_output": True,
            }
        ],
        "changes": {
            "files": [
                {
                    "path": "hello.c",
                    "status": "added",
                    "unified_diff": "--- /dev/null\n+++ hello.c",
                    "diff_truncated": False,
                }
            ],
            "baseline_complete": True,
            "snapshot_complete": True,
        },
        "tool_events": [{"name": "write_file", "duration_s": 0.1}],
        "tool_calls": 1,
    }

    response = ok_response(**body)
    parsed = json.loads(response)

    assert "changes" in parsed
    assert "baseline_complete" in parsed["changes"]
    assert "snapshot_complete" in parsed["changes"]
    assert len(parsed["changes"]["files"]) == 1
    assert parsed["changes"]["files"][0]["status"] == "added"

    print(f"  [PASS] Response has unified files structure: {len(parsed['changes']['files'])} files")


if __name__ == "__main__":
    test_snapshot_basic()
    test_snapshot_binary_detection()
    test_diff_added()
    test_diff_modified()
    test_diff_deleted()
    test_diff_with_existing_changes()
    test_full_response_structure()
    print("\n" + "=" * 50)
    print("All snapshot verification tests passed [PASS]")
    print("=" * 50)
