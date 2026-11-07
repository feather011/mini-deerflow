"""验证与 Guard：主 Agent 的结论必须和执行证据一致。

阶段目标（对应验收口径）：**同一条任务链路**分别跑出三种结果 ——
「验证通过」「有改动但未验证」「验证失败」—— 主 Agent 的结论必须与证据一致。
其中最关键的一条：**harness 执行结束，不等于用户的验收条件已经满足。**

三层验收：

1. **判定层**（单元）：`verification_guard.evaluate_verification` 从
   "命令 + 退出码 + 验收条件"算出三值结论，证据不足一律往"未验证"降。
2. **工具层**（集成）：`project_execute` 的结果里带 `guard` 字段；
   执行器跑完（status=completed）但没跑任何命令时，`guard.verdict` 必须是
   `unverified` 而不是 `verified`。
3. **会话层**（端到端）：脚本化主 Agent 在三种证据下**都自述"验证通过"**
   （模拟过度宣称），最终交给用户的结论仍必须与代码判定一致 ——
   冲突时以代码判定为准并被显式纠正。

不调用付费模型：主 Agent 与执行器都是仓内替身，不执行任何外部仓库代码。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from mini_deerflow.verification_guard import (
    LABEL_FAILED,
    LABEL_UNVERIFIED,
    LABEL_UNVERIFIED_WITH_CHANGES,
    LABEL_VERIFIED,
    VERDICT_FAILED,
    VERDICT_UNVERIFIED,
    VERDICT_VERIFIED,
    evaluate_verification,
    parse_declared_verdict,
    rewrite_conclusion,
    summarize_verdicts,
)

CALC_SOURCE = 'def add(a, b):\n    return a + b\n'

CONFIRMED_TASK = "在工作区新增 calc.py（add 函数），并跑一次 pytest"
CONFIRMED_CONTEXT = "用户已确认：这是一个独立练习目录，不要引入其它文件"
CONFIRMED_SCOPE = "只新增 calc.py 一个文件；不修改其它文件"
CONFIRMED_ACCEPTANCE = "运行 pytest 全部通过（退出码 0）"


def _pytest_command() -> str:
    """用当前解释器跑 pytest：路径显式化，避免 PATH 差异导致结果不可复现。"""
    exe = sys.executable.replace("\\", "/")
    return f'"{exe}" -m pytest test_calc.py -q -p no:cacheprovider'


# ---------------------------------------------------------------------------
# 1. 判定层（单元）
# ---------------------------------------------------------------------------


def test_没有任何验证命令_判为未验证():
    """harness 跑完了但没跑命令 —— 绝不能判成"验证通过"。"""
    verdict = evaluate_verification([], CONFIRMED_ACCEPTANCE, has_changes=True)
    assert verdict["verdict"] == VERDICT_UNVERIFIED
    assert verdict["label"] == LABEL_UNVERIFIED_WITH_CHANGES
    assert "没有执行任何验证命令" in verdict["reason"]


def test_命令退出码非零_判为验证失败():
    verdict = evaluate_verification(
        [{"command": "pytest -q", "exit_code": 1}],
        CONFIRMED_ACCEPTANCE,
        has_changes=True,
    )
    assert verdict["verdict"] == VERDICT_FAILED
    assert verdict["label"] == LABEL_FAILED
    assert verdict["failed_commands"] == ["pytest -q"]


def test_命令全过且与验收条件对得上_判为验证通过():
    verdict = evaluate_verification(
        [{"command": _pytest_command(), "exit_code": 0}],
        CONFIRMED_ACCEPTANCE,
        has_changes=True,
    )
    assert verdict["verdict"] == VERDICT_VERIFIED
    assert verdict["label"] == LABEL_VERIFIED
    assert verdict["acceptance_covered"] is True


def test_命令全过但与验收条件无关_保守降级为未验证():
    """跑的是 ls，验收要的是 pytest —— 证据不足以说"验证通过"。"""
    verdict = evaluate_verification(
        [{"command": "ls -la", "exit_code": 0}],
        CONFIRMED_ACCEPTANCE,
        has_changes=True,
    )
    assert verdict["verdict"] == VERDICT_UNVERIFIED
    assert verdict["acceptance_covered"] is False
    assert "pytest" in verdict["reason"]


def test_命令只是提到pytest而没有真的跑_不能判为通过():
    """`echo pytest` 退出码 0，但它**没有跑 pytest** —— 不得判为验证通过。"""
    verdict = evaluate_verification(
        [{"command": "echo pytest", "exit_code": 0}],
        CONFIRMED_ACCEPTANCE,
        has_changes=True,
    )
    assert verdict["verdict"] == VERDICT_UNVERIFIED, verdict
    assert verdict["acceptance_covered"] is False, verdict


def test_命令只是提到pytest而没有真的跑_会话结论也被纠正(isolated_workspaces_root):
    """同一条链路：执行器跑了 `echo pytest`（退出码 0），结论不能是"验证通过"。"""
    answer, trace, model, _, _ = _run_chain(
        "echo_only", "验证通过", isolated_workspaces_root
    )
    assert model.tool_result["guard"]["verdict"] == VERDICT_UNVERIFIED
    assert "验证结论：验证通过" not in answer, answer
    assert "验证结论：有改动但未验证" in answer, answer
    assert [
        t for t in trace if t.get("action") == "conclusion_corrected"
    ], trace


def test_退出码解析不出来_不能判为通过():
    verdict = evaluate_verification(
        [{"command": "pytest -q", "exit_code": None}],
        CONFIRMED_ACCEPTANCE,
        has_changes=True,
    )
    assert verdict["verdict"] == VERDICT_UNVERIFIED
    assert "退出码无法解析" in verdict["reason"]


def test_没有改动且没有验证_措辞是未验证而不是有改动但未验证():
    verdict = evaluate_verification([], "", has_changes=False)
    assert verdict["verdict"] == VERDICT_UNVERIFIED
    assert verdict["label"] == LABEL_UNVERIFIED


def test_多条执行取最保守的一个():
    """一轮里跑了三次，只要有一次失败，整体就不能是"通过"。"""
    verdicts = [
        evaluate_verification([{"command": "pytest -q", "exit_code": 0}], ""),
        evaluate_verification([{"command": "pytest -q", "exit_code": 1}], ""),
    ]
    summary = summarize_verdicts(verdicts)
    assert summary is not None
    assert summary["verdict"] == VERDICT_FAILED

    summary = summarize_verdicts(verdicts[:1])
    assert summary["verdict"] == VERDICT_VERIFIED

    assert summarize_verdicts([]) is None


def test_解析模型自述的结论():
    assert parse_declared_verdict("做完了。\n验证结论：验证通过") == VERDICT_VERIFIED
    assert parse_declared_verdict("验证结论：验证失败") == VERDICT_FAILED
    assert parse_declared_verdict("验证结论：有改动但未验证") == VERDICT_UNVERIFIED
    assert parse_declared_verdict("**验证结论**：验证通过") == VERDICT_VERIFIED
    assert parse_declared_verdict("没有结论行") is None


def test_结论与判定冲突时按代码判定纠正():
    verdict = evaluate_verification(
        [{"command": "pytest -q", "exit_code": 1}], CONFIRMED_ACCEPTANCE
    )
    content, corrected = rewrite_conclusion(
        "已写好 calc.py，全部通过。\n验证结论：验证通过", verdict
    )
    assert corrected is True
    assert "验证结论：验证失败" in content
    assert "验证结论：验证通过" not in content
    assert "结论纠正" in content
    assert "判定依据" in content


def test_结论与判定一致时原样保留():
    verdict = evaluate_verification(
        [{"command": "pytest -q", "exit_code": 0}], CONFIRMED_ACCEPTANCE
    )
    original = "已写好 calc.py。\n验证结论：验证通过"
    content, corrected = rewrite_conclusion(original, verdict)
    assert corrected is False
    assert content == original


# ---------------------------------------------------------------------------
# 仓内替身：执行器 + 主 Agent
# ---------------------------------------------------------------------------


class _ScriptedRunner:
    """仓内执行器替身 —— **不执行任何外部仓库代码**。

    它顶替的只有"跑 harness 子进程"这一步，按脚本产出 tool_events，
    从而稳定复现三种证据形态：

    - ``passed``：跑了一条退出码 0 的命令
    - ``no_command``：只写了文件，**一条命令都没跑**（harness 却正常结束）
    - ``failed``：跑了一条退出码 1 的命令
    """

    is_configured = True

    def __init__(self, script: str) -> None:
        self.script = script
        self.prompts: list[str] = []

    def run(
        self,
        task_id: str,
        repo_path: str,
        prompt: str,
        on_event=None,
        stop_event=None,
        extra_env=None,
    ):
        from mini_deerflow.harness_runner import (
            STATUS_COMPLETED,
            HarnessRunResult,
        )

        self.prompts.append(prompt)
        root = Path(repo_path)
        (root / "calc.py").write_text(CALC_SOURCE, encoding="utf-8")

        events = [{
            "seq": 0,
            "name": "write_file",
            "duration_s": 0.0,
            "output": "Wrote calc.py",
            "output_truncated": False,
        }]
        if self.script == "passed":
            events.append({
                "seq": 1,
                "name": "bash",
                "duration_s": 0.0,
                "output": "[exit 0] 1 passed",
                "output_truncated": False,
                "command_preview": _pytest_command(),
                "command_truncated": False,
            })
        elif self.script == "failed":
            events.append({
                "seq": 1,
                "name": "bash",
                "duration_s": 0.0,
                "output": "[exit 1] 1 failed",
                "output_truncated": False,
                "command_preview": _pytest_command(),
                "command_truncated": False,
            })
        elif self.script == "echo_only":
            # 只**提到** pytest，没有真的跑它（误报场景）
            events.append({
                "seq": 1,
                "name": "bash",
                "duration_s": 0.0,
                "output": "[exit 0] pytest",
                "output_truncated": False,
                "command_preview": "echo pytest",
                "command_truncated": False,
            })

        return HarnessRunResult(
            status=STATUS_COMPLETED,
            task_id=task_id,
            final_answer="已创建 calc.py。",
            tool_events=events,
            tool_calls=len(events),
            exit_code=0,
            files_written=["calc.py"],
        )


class _LaunchFailedRunner:
    """执行器根本起不来的替身（launch_failed）。"""

    is_configured = True

    def run(
        self,
        task_id: str,
        repo_path: str,
        prompt: str,
        on_event=None,
        stop_event=None,
        extra_env=None,
    ):
        from mini_deerflow.harness_runner import (
            STATUS_LAUNCH_FAILED,
            HarnessRunResult,
        )

        return HarnessRunResult(
            status=STATUS_LAUNCH_FAILED,
            task_id=task_id,
            error="执行器不存在",
            tool_events=[],
            tool_calls=0,
            files_written=[],
        )


class _ScriptedMainAgent:
    """主 Agent 替身：先派发，再汇总。

    汇总时**故意**声明固定的结论标签（默认"验证通过"）—— 模拟过度宣称，
    用来证明系统的结论以代码判定为准，而不是以模型自述为准。
    """

    def __init__(self, declared: str = "验证通过") -> None:
        self.declared = declared
        self.calls = 0
        self.tool_result: dict = {}
        self.final_answer = ""

    def decide(self, messages, tools=None):
        from mini_deerflow.models import FinalAnswer, ToolCall

        self.calls += 1
        tool_msgs = [m for m in messages if m.role == "tool"]
        if not tool_msgs:
            return ToolCall(
                name="project_execute",
                arguments={
                    "task": CONFIRMED_TASK,
                    "context": CONFIRMED_CONTEXT,
                    "scope": CONFIRMED_SCOPE,
                    "acceptance": CONFIRMED_ACCEPTANCE,
                },
            )
        self.tool_result = json.loads(tool_msgs[-1].content)
        self.final_answer = (
            f"本轮改动：calc.py。\n验证结论：{self.declared}"
        )
        return FinalAnswer(content=self.final_answer)


@pytest.fixture
def isolated_workspaces_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "workspaces"
    root.mkdir()
    monkeypatch.setenv("MINI_DEERFLOW_WORKSPACES_ROOT", str(root))
    return root


def _make_project(store, project_store, workspaces_root, name):
    from mini_deerflow.api.services.projects import ProjectService

    project_service = ProjectService(
        store=project_store,
        workspaces_root=str(workspaces_root),
        thread_store=store,
    )
    project = project_service.create(name)
    thread = store.create(f"t-{name}", project_id=project.project_id)
    return project, thread


def _run_chain(script: str, declared: str, workspaces_root, runner=None):
    """跑完整链路：主 Agent → project_execute → 主 Agent 汇总。"""
    import uuid

    from mini_deerflow.project_store import InMemoryProjectStore
    from mini_deerflow.project_tools import create_project_execute_tool
    from mini_deerflow.runtime import RuntimeConfig, create_runtime, _get_project_lock
    from mini_deerflow.thread_store import InMemoryThreadStore
    from mini_deerflow.tools import create_default_registry

    unique = uuid.uuid4().hex[:8]
    project_store = InMemoryProjectStore()
    store = InMemoryThreadStore()
    registry = create_default_registry()

    runtime = create_runtime(
        store=store,
        project_store=project_store,
        registry=registry,
        config=RuntimeConfig(harness_root=None),
    )
    if runner is None:
        runner = _ScriptedRunner(script)
    registry.register(create_project_execute_tool(
        thread_store=store,
        project_store=project_store,
        runner=runner,
        get_lock=_get_project_lock,
    ))

    model = _ScriptedMainAgent(declared=declared)
    runtime.integrated_agent._agent._model = model

    project, thread = _make_project(
        store, project_store, workspaces_root, f"guard-{script}-{unique}"
    )

    answer, trace = runtime.integrated_agent._agent.run(
        thread.thread_id, f"{CONFIRMED_TASK}\n\n{CONFIRMED_CONTEXT}"
    )
    return answer, trace, model, runner, project


# ---------------------------------------------------------------------------
# 2. 工具层（集成）
# ---------------------------------------------------------------------------


def test_执行器跑完但没跑命令_状态是完成但判定是未验证(isolated_workspaces_root):
    """**harness 执行结束 ≠ 用户的验收条件已经满足**。"""
    _, _, model, _, _ = _run_chain(
        "no_command", "验证通过", isolated_workspaces_root
    )
    result = model.tool_result
    assert result["ok"] is True
    assert result["status"] == "completed", "执行器确实正常结束了"
    assert result["guard"]["verdict"] == VERDICT_UNVERIFIED
    assert result["guard"]["label"] == LABEL_UNVERIFIED_WITH_CHANGES
    assert result["verification"] == []


# ---------------------------------------------------------------------------
# 3. 会话层（端到端验收）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script, expected_verdict, expected_label",
    [
        ("passed", VERDICT_VERIFIED, LABEL_VERIFIED),
        ("no_command", VERDICT_UNVERIFIED, LABEL_UNVERIFIED_WITH_CHANGES),
        ("failed", VERDICT_FAILED, LABEL_FAILED),
    ],
)
def test_同一条任务链路_三种结果_主Agent结论与证据一致(
    isolated_workspaces_root, script, expected_verdict, expected_label
):
    """同一条链路跑出三种结果，主 Agent 的结论必须与证据一致。

    主 Agent 在三种情况下都自述"验证通过"（过度宣称）——
    正确的那一档保持原样，错的两档被纠正。
    """
    answer, trace, model, runner, project = _run_chain(
        script, "验证通过", isolated_workspaces_root
    )

    # ── 证据侧：权威判定由代码算出 ────────────────────────────────────
    guard = model.tool_result["guard"]
    assert guard["verdict"] == expected_verdict, guard
    assert guard["label"] == expected_label, guard
    assert len(runner.prompts) == 1

    # ── 结论侧：交给用户的结论必须与证据一致 ──────────────────────────
    declared_lines = [
        line for line in answer.splitlines() if line.startswith("验证结论：")
    ]
    assert len(declared_lines) == 1, f"结论行不唯一：{declared_lines}"
    assert declared_lines[0].startswith(f"验证结论：{expected_label}"), answer

    if expected_verdict != VERDICT_VERIFIED:
        # 过度宣称必须被纠正，并留下纠正记录
        assert "验证结论：验证通过" not in answer, answer
        assert "结论纠正" in answer, answer
        assert "判定依据" in answer, answer
        corrected = [
            t for t in trace if t.get("action") == "conclusion_corrected"
        ]
        assert corrected, trace
        assert corrected[0]["detail"]["verdict"] == expected_verdict
    else:
        assert "结论纠正" not in answer, answer
        assert not [
            t for t in trace if t.get("action") == "conclusion_corrected"
        ], trace

    # ── 真实副作用：工作区里确实有改动 ───────────────────────────────
    workspace = project.workspace_path
    assert workspace is not None
    assert (workspace / "calc.py").read_text(encoding="utf-8") == CALC_SOURCE


def test_执行器起不来_结论同样被约束(isolated_workspaces_root):
    """launch_failed 拿不到任何验证记录，但也不能让模型写"已完成"。"""
    answer, trace, model, _, _ = _run_chain(
        "launch_failed", "验证通过", isolated_workspaces_root,
        runner=_LaunchFailedRunner(),
    )
    result = model.tool_result
    assert result["ok"] is False
    assert result["guard"]["verdict"] == VERDICT_FAILED
    assert "验证结论：验证通过" not in answer, answer
    assert "验证结论：验证失败" in answer, answer
    assert [
        t for t in trace if t.get("action") == "conclusion_corrected"
    ], trace


def test_主Agent自述正确结论时不被改写(isolated_workspaces_root):
    """判定为失败、模型也如实说失败 → 结论原样保留，不做无谓改写。"""
    answer, trace, model, _, _ = _run_chain(
        "failed", "验证失败", isolated_workspaces_root
    )
    assert "验证结论：验证失败" in answer
    assert "结论纠正" not in answer
    assert not [t for t in trace if t.get("action") == "conclusion_corrected"]


def test_项目会话提示词写明验证结论契约():
    """防回归：验证结论契约不能被静默删掉。"""
    from mini_deerflow.agent import PROJECT_SESSION_PROMPT

    for marker in ("验证结论契约", "验证结论：验证通过", "guard",
                   "unverified", "failed", "不等于"):
        assert marker in PROJECT_SESSION_PROMPT, f"提示词缺少 {marker}"
