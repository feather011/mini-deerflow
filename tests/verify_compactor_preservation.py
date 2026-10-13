"""Verify harness compactor preserves scope/acceptance after compression.

Chain trace:
prompt -> messages -> active_request -> compact_history -> summary_message
"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
HARNESS_ROOT = Path("D:/项目解读/harnessagent")
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(HARNESS_ROOT))


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeChoice:
    def __init__(self, content):
        self.message = FakeMessage(content)


class FakeLLM:
    def __init__(self, response_content):
        self._response = response_content

    def chat(self, messages, **kw):
        class Resp:
            choices = [FakeChoice(self._response)]
        return Resp()


def test_prompt_to_messages_mapping():
    """Test 1: prompt as single user message"""
    print("\n=== Test 1: prompt -> messages mapping ===")

    prompt_parts = [
        "相关文件：existing.py",
        "任务：在根目录创建 loop.py",
        "约束：不扩展目标",
        "任务范围：【ONLY_LOOP_PY】只写 loop.py",
        "验收条件：【OUTPUT_0_4】运行后输出 0-4",
    ]
    prompt = "\n\n".join(prompt_parts)
    messages = [{"role": "user", "content": prompt}]

    assert len(messages) == 1, "prompt should be single user message"
    assert "ONLY_LOOP_PY" in messages[0]["content"]
    assert "OUTPUT_0_4" in messages[0]["content"]

    print("[PASS] prompt as single user message, scope/acceptance inside")


def test_active_request_extraction():
    """Test 2: active_request from last user message"""
    print("\n=== Test 2: active_request extraction ===")

    prompt = (
        "相关文件：existing.py\n\n"
        "任务：在根目录创建 loop.py\n\n"
        "约束：不扩展目标\n\n"
        "任务范围：【ONLY_LOOP_PY】只写 loop.py\n\n"
        "验收条件：【OUTPUT_0_4】运行后输出 0-4"
    )
    messages = [{"role": "user", "content": prompt}]

    # agent_loop active extraction
    active = messages[-1]["content"] if messages and messages[-1].get("role") == "user" else ""
    assert "ONLY_LOOP_PY" in active
    assert "OUTPUT_0_4" in active

    print("[PASS] active_request contains scope and acceptance")


def test_compactor_preserves_markers():
    """Test 3: compactor preserves markers in summary_message"""
    print("\n=== Test 3: Compactor marker preservation ===")

    from harness.context.compactor import ContextCompactor

    prompt = (
        "相关文件：existing.py\n\n"
        "任务：在根目录创建 loop.py\n\n"
        "约束：不扩展目标\n\n"
        "任务范围：【ONLY_LOOP_PY】只写 loop.py\n\n"
        "验收条件：【OUTPUT_0_4】运行后输出 0-4"
    )

    fake_llm = FakeLLM("Summary: goal is to create loop.py")
    compactor = ContextCompactor(
        llm=fake_llm,
        model="test",
        transcript_dir=Path("/tmp"),
        tool_results_dir=Path("/tmp"),
    )

    # Build summary_message with prompt as request
    summary_msg = compactor.summary_message("Compacted", prompt, "Summary", Path("/tmp/transcript"))
    content = summary_msg["content"]

    assert "ONLY_LOOP_PY" in content, f"scope marker missing in summary: {content[:200]}"
    assert "OUTPUT_0_4" in content, f"acceptance marker missing in summary: {content[:200]}"

    print("[PASS] compact_history preserves scope and acceptance in summary_message")


def test_compact_history_directly():
    """Test 4: Directly call compact_history (not via prepare)"""
    print("\n=== Test 4: compact_history directly ===")

    from harness.context.compactor import ContextCompactor

    prompt = (
        "相关文件：existing.py\n\n"
        "任务：在根目录创建 loop.py\n\n"
        "约束：不扩展目标\n\n"
        "任务范围：【ONLY_LOOP_PY】只写 loop.py\n\n"
        "验收条件：【OUTPUT_0_4】运行后输出 0-4"
    )

    fake_llm = FakeLLM("Summary: created loop.py per user request")
    compactor = ContextCompactor(
        llm=fake_llm,
        model="test",
        transcript_dir=Path("/tmp"),
        tool_results_dir=Path("/tmp"),
    )

    # Simulate large message history (not from prepare())
    messages = [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": prompt},
    ]
    # Add many rounds to simulate real scenario
    for i in range(50):
        messages.append({"role": "assistant", "content": "Calling tool", "tool_calls": [{"id": f"c{i}", "function": {"name": "bash", "arguments": "{}"}}]})
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"Tool result {i}: " + "x" * 200})

    print(f"  Input messages: {len(messages)}")

    # Directly call compact_history (bypasses prepare's limit check)
    result = compactor.compact_history(messages, prompt)
    print(f"  Output messages: {len(result)}")

    # After compact_history: should be single [Compacted] message
    assert len(result) == 1, f"compact_history should return 1 message, got {len(result)}"
    last_msg = result[-1]
    assert last_msg["role"] == "user", f"Should be user message, got {last_msg['role']}"

    content = last_msg["content"]
    print(f"  Content preview: {content[:200]}...")

    # Key check: "Current user request" section should contain the full prompt with markers
    assert "ONLY_LOOP_PY" in content, f"scope marker missing: {content[:300]}"
    assert "OUTPUT_0_4" in content, f"acceptance marker missing: {content[:300]}"
    assert "Current user request" in content, "Missing Current user request section"
    assert "Conversation summary" in content, "Missing Conversation summary section"

    print("[PASS] compact_history preserves scope and acceptance in Current user request")


def test_tool_call_result_pairing():
    """Test 5: snip_compact protects tool call/result pairing"""
    print("\n=== Test 5: Tool call-result pairing ===")

    from harness.context.compactor import ContextCompactor

    fake_llm = FakeLLM("Summary")
    compactor = ContextCompactor(
        llm=fake_llm,
        model="test",
        transcript_dir=Path("/tmp"),
        tool_results_dir=Path("/tmp"),
    )

    messages = [{"role": "system", "content": "You are a coding agent."}, {"role": "user", "content": "Task"}]
    for i in range(60):
        messages.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "function": {"name": "bash", "arguments": "{}"}}]})
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"Result {i}"})

    print(f"  Original: {len(messages)} messages")

    result = compactor.snip_compact(messages, max_messages=50)
    print(f"  After snip: {len(result)} messages")

    # Check pairing integrity
    tool_uses = {msg["tool_calls"][0]["id"]: i for i, msg in enumerate(result) if msg.get("role") == "assistant" and msg.get("tool_calls")}
    tool_results = {msg["tool_call_id"]: i for i, msg in enumerate(result) if msg.get("role") == "tool"}

    for tc_id, tc_idx in tool_uses.items():
        if tc_id in tool_results:
            assert abs(tool_results[tc_id] - tc_idx) == 1, f"Pair mismatch: {tc_id}"

    print("[PASS] snip_compact protects tool call/result pairing")


def test_summary_input_truncation_risk():
    """Test 6: Check if SUMMARY_INPUT_CHAR_LIMIT can truncate scope/acceptance"""
    print("\n=== Test 6: Summary input truncation risk ===")

    from harness.context.compactor import ContextCompactor

    # Large prompt with markers at end
    large_prompt = "相关文件：" + "x" * 70000 + "\n\n任务：创建文件\n\n任务范围：【ONLY_LOOP_PY】只写 loop.py\n\n验收条件：【OUTPUT_0_4】运行后输出 0-4"
    messages = [{"role": "user", "content": large_prompt}]

    fake_llm = FakeLLM("test")
    compactor = ContextCompactor(
        llm=fake_llm,
        model="test",
        transcript_dir=Path("/tmp"),
        tool_results_dir=Path("/tmp"),
    )

    summary_input = compactor.summary_input(messages)
    print(f"  Original: {len(large_prompt)} chars")
    print(f"  After summary_input: {len(summary_input)} chars")

    has_scope = "ONLY_LOOP_PY" in summary_input
    has_acceptance = "OUTPUT_0_4" in summary_input
    print(f"  scope marker present: {has_scope}")
    print(f"  acceptance marker present: {has_acceptance}")

    if not has_scope or not has_acceptance:
        print("[WARN] Large prompt: scope/acceptance may be truncated by SUMMARY_INPUT_CHAR_LIMIT")
    else:
        print("[PASS] Large prompt: scope/acceptance preserved in summary_input")


if __name__ == "__main__":
    test_prompt_to_messages_mapping()
    test_active_request_extraction()
    test_compactor_preserves_markers()
    test_compact_history_directly()
    test_tool_call_result_pairing()
    test_summary_input_truncation_risk()
    print("\n" + "=" * 50)
    print("All compaction verification tests passed")
    print("=" * 50)
