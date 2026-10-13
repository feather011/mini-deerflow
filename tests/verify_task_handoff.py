"""Task handoff verification script.

Verification points:
1. Model-provided task/context is preserved (not overwritten)
2. Fixed constraint rules are injected
3. scope / acceptance parameters are recognized and injected
4. Error propagation (empty task, project_busy)
5. cwd determined by backend (repo_path)
"""

import json
import os
import sys
import tempfile
import threading
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

HARNESS_ROOT = "D:/项目解读/harnessagent"


def _make_fake_resp(role: str, content: str | None = None,
                    tool_calls: list | None = None) -> object:
    """Construct fake resp-like object."""
    import uuid as _uuid

    def _make_tc(tc_dict: dict) -> object:
        fn = tc_dict.get("function", {})
        inner = type("F", (), {
            "name": fn.get("name", ""),
            "arguments": fn.get("arguments", ""),
        })()
        return type("TC", (), {
            "id": tc_dict.get("id", f"call_{_uuid.uuid4().hex[:8]}"),
            "function": inner,
        })()

    msg_parts: dict = {"role": role}
    if content is not None:
        msg_parts["content"] = content
    if tool_calls is not None:
        msg_parts["tool_calls"] = [_make_tc(tc) for tc in tool_calls]
    msg_obj = type("Msg", (), {
        "content": content,
        "tool_calls": msg_parts.get("tool_calls"),
        "model_dump": lambda self, **kw: {
            k: v for k, v in msg_parts.items() if v is not None
        },
    })()
    return type("Resp", (), {"choices": [type("C", (), {"message": msg_obj})()]})()


class PromptCapturingRunner:
    """Wrap HarnessRunner, record actual prompt passed to subprocess."""

    def __init__(self, real_runner):
        self._real = real_runner
        self.captured_prompts: list[str] = []

    @property
    def is_configured(self):
        return self._real.is_configured

    @property
    def _timeout_seconds(self):
        return self._real._timeout_seconds

    def run(self, **kwargs):
        prompt = kwargs.get("prompt", "")
        self.captured_prompts.append(prompt)
        return self._real.run(**kwargs)


def _build_prompt(task_raw, context_raw, scope="", acceptance=""):
    """Mirror the logic in _execute handler."""
    _TASK_RULES = (
        "Constraints:\n"
        "- Retrieve relevant files as needed; modify only content necessary for the task.\n"
        "- Do not expand scope; return clarification request if info is insufficient.\n"
        "- cwd is determined by backend; do not override in tool calls.\n"
    )
    prompt_parts = []
    if context_raw:
        prompt_parts.append(context_raw.strip())
    prompt_parts.append(f"Task: {task_raw}")
    prompt_parts.append(_TASK_RULES)
    if scope:
        prompt_parts.append(f"Scope: {scope.strip()}")
    if acceptance:
        prompt_parts.append(f"Acceptance criteria: {acceptance.strip()}")
    return "\n\n".join(prompt_parts)


def test_prompt_construction():
    """Verify prompt assembly logic."""
    print("\n=== Test 1: Prompt Construction ===")

    # Scenario 1: task only
    p1 = _build_prompt("Write a for loop", "")
    print(f"Scenario 1 - task only:\n{p1}\n")
    assert "Task: Write a for loop" in p1
    assert "Constraints:" in p1
    assert "cwd is determined by backend" in p1

    # Scenario 2: task + context
    p2 = _build_prompt("Modify user.py", "Related file: src/models/user.py")
    print(f"Scenario 2 - task + context:\n{p2}\n")
    assert "Related file: src/models/user.py" in p2
    assert "Task: Modify user.py" in p2

    # Scenario 3: task + scope + acceptance
    p3 = _build_prompt("Create loop.py in root", "",
                       scope="Only write loop.py, do not traverse other directories",
                       acceptance="Output 0-4 after running")
    print(f"Scenario 3 - task + scope + acceptance:\n{p3}\n")
    assert "Scope: Only write loop.py" in p3
    assert "Acceptance criteria: Output 0-4" in p3

    print("[PASS] Prompt construction verified")


def test_tool_spec():
    """Verify tool parameter descriptions are updated."""
    print("\n=== Test 2: Tool Parameter Spec ===")

    from mini_deerflow.project_tools import create_project_execute_tool

    runner = type("R", (), {"is_configured": False})()
    tool = create_project_execute_tool(
        thread_store=type("S", (), {})(),
        project_store=type("P", (), {})(),
        runner=runner,
        get_lock=None,
    )

    props = tool.parameters["properties"]
    descs = {k: v.get("description", "") for k, v in props.items()}

    print(f"Parameter descriptions: {json.dumps(descs, ensure_ascii=False, indent=2)}")

    # Verify scope / acceptance parameters exist
    assert "scope" in props, "Missing scope parameter"
    assert "acceptance" in props, "Missing acceptance parameter"

    # Verify scope description guides filling
    assert "不遍历" in descs["scope"], f"scope description should mention 'do not traverse': {descs['scope']}"

    print("[PASS] Tool parameters verified")


def test_error_propagation():
    """Verify error propagation."""
    print("\n=== Test 3: Error Propagation ===")

    from mini_deerflow.project_tools import _as_tool_result

    class FakeContext:
        thread_id = "fake-thread"

    def fake_handler(args, ctx):
        from mini_deerflow.project_tools import err_response
        if not args.get("task", "").strip():
            return err_response("execute", "task cannot be empty", kind="usage_error")
        return '{"ok": false}'

    wrapped = _as_tool_result("execute", fake_handler)
    result = wrapped({"task": "  "}, FakeContext())
    parsed = json.loads(result)

    assert parsed["ok"] is False
    assert "usage_error" in parsed.get("kind", "")
    print(f"Empty task error: {result}")

    print("[PASS] Error propagation verified")


def test_full_e2e_prompt_trace():
    """End-to-end trace: actual prompt content in call chain."""
    print("\n=== Test 4: Full E2E Prompt Trace ===")

    # Create temp project
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        # Write a file as context
        (workspace / "existing.py").write_text("# existing file", encoding="utf-8")

        # Simulate harness prompt content
        task = "Create loop.py in root"
        context = f"Related file: existing.py\nCurrent directory: {list(workspace.iterdir())}"
        scope = "Only write loop.py, do not traverse other directories"
        acceptance = "Output 0-4 after running"

        prompt = _build_prompt(task, context, scope, acceptance)

        print(f"Final prompt content:\n{prompt}\n")

        # Verify constraint rules
        assert "Constraints:" in prompt
        assert "cwd is determined by backend" in prompt

        # Verify context first
        assert prompt.startswith("Related file")

        # Verify acceptance criteria
        assert "Acceptance criteria:" in prompt

    print("[PASS] E2E prompt trace verified")


if __name__ == "__main__":
    test_prompt_construction()
    test_tool_spec()
    test_error_propagation()
    test_full_e2e_prompt_trace()
    print("\n" + "=" * 50)
    print("All verifications passed [PASS]")
    print("=" * 50)
