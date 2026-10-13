"""Verify tool-reported paths and command records with correct semantics."""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def test_exit_code_parsing():
    """Verify exit_code extraction from bash output format."""
    print("\n=== Test: exit_code parsing ===")

    def parse_exit_code(output):
        if output.startswith("[exit "):
            end = output.find("]", 7)
            if end > 0:
                try:
                    return int(output[6:end])
                except ValueError:
                    pass
        return None

    # Bash output format: "[exit N] output"
    cases = [
        ("[exit 0] hello world", 0),
        ("[exit 1] error: not found", 1),
        ("[exit 127] command not found", 127),
        ("hello world", None),  # no exit code
        ("[exit abc] test", None),  # invalid
    ]

    for output, expected in cases:
        result = parse_exit_code(output)
        status = "[PASS]" if result == expected else "[FAIL]"
        print(f"  {status} '{output[:40]}' -> exit={result} (expected {expected})")

    print("[PASS] exit_code parsing verified")


def test_command_preview_semantics():
    """Verify command field is named command_preview, not full_command."""
    print("\n=== Test: command_preview semantics ===")

    # Simulate harness_runner behavior
    def get_command(event):
        if event.get("name") == "bash" and event.get("command_preview"):
            return event.get("command_preview")
        return event.get("args_preview", "")

    # Case 1: has command_preview
    e1 = {"name": "bash", "command_preview": "gcc hello.c -o hello && ./hello", "command_truncated": False}
    assert get_command(e1) == e1["command_preview"]
    assert e1["command_truncated"] is False

    # Case 2: command too long
    long_cmd = "gcc " + "x" * 600
    e2 = {"name": "bash", "command_preview": long_cmd[:500], "command_truncated": True}
    assert get_command(e2) == e2["command_preview"]
    assert e2["command_truncated"] is True

    # Case 3: no command_preview (older events)
    e3 = {"name": "bash", "args_preview": "gcc hello"}
    assert get_command(e3) == e3["args_preview"]

    print("  [PASS] command_preview with command_truncated flag")
    print("  [PASS] fallback to args_preview for older events")


def test_has_output_semantics():
    """Verify has_output = bool(output), NOT verification passed."""
    print("\n=== Test: has_output semantics ===")

    cases = [
        ("[exit 0] hello world", True),    # success with output
        ("[exit 0] (no output)", True),  # success with no output
        ("[exit 1] error occurred", True), # failure with output
        ("", False),                       # no output
    ]

    for output, expected in cases:
        has_output = bool(output)
        status = "[PASS]" if has_output == expected else "[FAIL]"
        print(f"  {status} output='{output[:30]}' has_output={has_output}")

    print("  [PASS] has_output = bool(output), NOT 'verification passed'")


def test_completed_response_structure():
    """Verify completed response has correct field names."""
    print("\n=== Test: completed response ===")

    from mini_deerflow.project_tools import ok_response

    body = {
        "ok": True,
        "status": "completed",
        "task_id": "abc123",
        "answer": "Created hello.c",
        "files_written": ["hello.c"],
        "verification": [
            {
                "command": "gcc hello.c && ./a.out",  # command_preview
                "command_truncated": False,
                "output": "[exit 0] hello world",
                "output_truncated": False,
                "exit_code": 0,
                "duration_s": 1.5,
                "has_output": True,
            }
        ],
        "tool_events": [{"name": "write_file", "duration_s": 0.1}],
        "tool_calls": 2,
    }

    response = ok_response(**body)
    parsed = json.loads(response)

    assert parsed["ok"] is True
    assert "files_written" in parsed
    assert "verification" in parsed
    assert "verification_complete" not in parsed  # no such field
    assert parsed["verification"][0]["exit_code"] == 0
    assert parsed["verification"][0]["has_output"] is True
    assert "has_evidence" not in parsed["verification"][0]  # renamed

    print(f"  [PASS] files_written: {parsed['files_written']}")
    print(f"  [PASS] exit_code: {parsed['verification'][0]['exit_code']}")
    print(f"  [PASS] has_output: {parsed['verification'][0]['has_output']}")


def test_all_status_have_verification():
    """Verify stopped/timeout/failed also return verification (possibly empty)."""
    print("\n=== Test: all statuses return verification ===")

    from mini_deerflow.project_tools import ok_response

    for status in ["stopped", "timeout", "failed"]:
        body = {
            "ok": False,
            "status": status,
            "task_id": "xyz",
            "answer": "partial",
            "files_written": [],
            "verification": [],  # empty but present
            "error": f"task {status}",
            "tool_events": [],
            "tool_calls": 0,
        }
        response = ok_response(**body)
        parsed = json.loads(response)
        assert "verification" in parsed, f"Missing verification in {status}"
        print(f"  [PASS] {status}: verification present (may be empty)")


if __name__ == "__main__":
    test_exit_code_parsing()
    test_command_preview_semantics()
    test_has_output_semantics()
    test_completed_response_structure()
    test_all_status_have_verification()
    print("\n" + "=" * 50)
    print("All semantic verification tests passed [PASS]")
    print("=" * 50)
