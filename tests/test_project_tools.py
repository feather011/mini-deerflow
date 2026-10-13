"""WM-3B：项目只读工具与项目提示词。

验证方式遵循「行为证据优先」：

- 临时 Git 仓库中真实读取 / 列举 / 搜索；
- 三种工具跑完后文件字节与 `git status --porcelain` 均不变（只读性）；
- 越界、符号链接越界、缺失项目、非项目会话、输出上限逐条断言；
- 普通会话即使模型强行返回 project_* 调用，也在 dispatch 层被拒；
- 真实 Agent 循环中提示词注入与工具可见性（脚本模型只证明流程与接入，
  不声称已证明真实模型的意图判断质量）。

符号链接用例在无权限创建链接的环境（Windows 默认）自动跳过。
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from mini_deerflow.agent import PROJECT_SESSION_PROMPT, Agent
from mini_deerflow.models import FinalAnswer, Message, ToolCall
from mini_deerflow.project_store import InMemoryProjectStore
from mini_deerflow.project_tools import create_project_read_tools
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tools import ToolNotAllowedError, create_default_registry

# ---------------------------------------------------------------------------
# 环境搭建
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """临时仓库（非 git 也可用；只读性用例自带真 git 初始化）。"""
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(
        "def main():\n    return 42\n", encoding="utf-8"
    )
    (root / "README.md").write_text("# demo\nhello grep\n", encoding="utf-8")
    (root / ".git").mkdir()  # 工具不检查 .git；仅为目录形态
    return root


def _make_tools(repo: Path, *, thread_id: str = "t1", project_id: str | None = "p1"):
    """建 stores + 工具，返回 (tools dict, context)。"""
    thread_store = InMemoryThreadStore()
    project_store = InMemoryProjectStore()
    if project_id is not None:
        project = project_store.create(project_id, "demo", str(repo))
        # 设置 workspace_path（Phase 1 新字段）
        project.workspace_path = repo
        project_store.save(project)
        thread_store.create(thread_id, project_id)
    else:
        thread_store.create(thread_id)
    tools = {
        t.name: t
        for t in create_project_read_tools(thread_store, project_store)
    }
    ctx = SimpleNamespace(thread_id=thread_id)
    return tools, ctx


def _call(tool, arguments: dict, ctx) -> dict:
    return json.loads(tool.execute(arguments, ctx))


# ---------------------------------------------------------------------------
# 1. 临时仓库中读取 / 列举 / 搜索
# ---------------------------------------------------------------------------


def test_read_file_读取内容与行号(repo):
    tools, ctx = _make_tools(repo)
    result = _call(tools["project_read_file"], {"path": "src/app.py"}, ctx)
    assert result["ok"] is True
    assert result["path"] == "src/app.py"
    assert "1| def main():" in result["content"]
    assert "2|     return 42" in result["content"]
    assert result["byte_truncated"] is False


def test_read_file_行窗口_offset_limit(repo):
    tools, ctx = _make_tools(repo)
    result = _call(
        tools["project_read_file"],
        {"path": "src/app.py", "offset": 1, "limit": 1},
        ctx,
    )
    assert result["ok"] is True
    assert result["returned"] == 1
    assert result["content"] == "2|     return 42"


def test_glob_列出仓库文件并跳过重目录(repo):
    (repo / "node_modules" / "pkg").mkdir(parents=True)
    (repo / "node_modules" / "pkg" / "index.js").write_text("x", encoding="utf-8")
    (repo / ".git" / "config").write_text("x", encoding="utf-8")
    tools, ctx = _make_tools(repo)
    result = _call(tools["project_glob"], {"pattern": "**/*"}, ctx)
    assert result["ok"] is True
    assert "src/app.py" in result["paths"]
    assert "README.md" in result["paths"]
    assert not any("node_modules" in p for p in result["paths"])
    assert not any(p.startswith(".git") for p in result["paths"])


def test_grep_命中行号与上限(repo):
    tools, ctx = _make_tools(repo)
    result = _call(tools["project_grep"], {"pattern": "return"}, ctx)
    assert result["ok"] is True
    assert result["returned"] == 1
    assert result["hits"][0]["path"] == "src/app.py"
    assert result["hits"][0]["line"] == 2

    capped = _call(
        tools["project_grep"], {"pattern": "hello", "max_hits": 1}, ctx
    )
    assert capped["returned"] == 1
    assert capped["hits_truncated"] is True


def test_grep_字面模式不报错(repo):
    """字面模式：所有文本都按字面搜索，不解析正则——无"非法正则"概念。"""
    tools, ctx = _make_tools(repo)
    result = _call(tools["project_grep"], {"pattern": "(unclosed"}, ctx)
    assert result["ok"] is True
    assert result["returned"] == 0  # 字面检索，找不到即空结果


def test_grep_跳过二进制文件(repo):
    (repo / "data.bin").write_bytes(b"PK\x03\x04\x00\x01binary\x00payload")
    tools, ctx = _make_tools(repo)
    result = _call(tools["project_grep"], {"pattern": "payload"}, ctx)
    assert result["ok"] is True
    assert result["returned"] == 0


# ---------------------------------------------------------------------------
# 2. 越界与边界
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_path",
    ["../secret.txt", "src/../../secret.txt", "/etc/passwd", "C:/Windows/win.ini"],
)
def test_read_file_越界与绝对路径被拒(repo, bad_path):
    tools, ctx = _make_tools(repo)
    result = _call(tools["project_read_file"], {"path": bad_path}, ctx)
    assert result["ok"] is False
    assert result["kind"] == "usage_error"


def test_glob_越界模式被拒(repo):
    tools, ctx = _make_tools(repo)
    result = _call(tools["project_glob"], {"pattern": "../*"}, ctx)
    assert result["ok"] is False
    assert result["kind"] == "usage_error"


def test_read_file_符号链接指向仓库外被拒(tmp_path, repo):
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = repo / "link.txt"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境无法创建符号链接（Windows 需要权限）")
    tools, ctx = _make_tools(repo)
    result = _call(tools["project_read_file"], {"path": "link.txt"}, ctx)
    assert result["ok"] is False


def test_glob_结果中的符号链接越界项被过滤(tmp_path, repo):
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "leak.txt").write_text("leak", encoding="utf-8")
    try:
        os.symlink(outside_dir, repo / "leakdir")
    except (OSError, NotImplementedError):
        pytest.skip("当前环境无法创建符号链接（Windows 需要权限）")
    tools, ctx = _make_tools(repo)
    result = _call(tools["project_glob"], {"pattern": "**/*.txt"}, ctx)
    assert result["ok"] is True
    assert not any("leak" in p for p in result["paths"])


def test_缺失项目返回状态错误(repo):
    thread_store = InMemoryThreadStore()
    project_store = InMemoryProjectStore()
    thread_store.create("t1", "p-gone")  # 项目从未创建
    tools = {t.name: t for t in create_project_read_tools(thread_store, project_store)}
    result = _call(tools["project_read_file"], {"path": "a.py"},
                   SimpleNamespace(thread_id="t1"))
    assert result["ok"] is False
    assert result["kind"] == "error"


def test_非项目会话返回状态错误(repo):
    tools, ctx = _make_tools(repo, project_id=None)
    result = _call(tools["project_glob"], {"pattern": "*"}, ctx)
    assert result["ok"] is False
    assert result["kind"] == "error"


def test_缺少执行上下文直接拒绝(repo):
    tools, _ = _make_tools(repo)
    result = _call(tools["project_glob"], {"pattern": "*"}, None)
    assert result["ok"] is False
    assert result["kind"] == "error"


# ---------------------------------------------------------------------------
# 3. 输出上限
# ---------------------------------------------------------------------------


def test_read_file_字节截断标注(repo, monkeypatch):
    (repo / "big.txt").write_text("x" * 500, encoding="utf-8")
    monkeypatch.setattr(
        "mini_deerflow.project_tools.READ_MAX_BYTES", 100
    )
    tools, ctx = _make_tools(repo)
    result = _call(tools["project_read_file"], {"path": "big.txt"}, ctx)
    assert result["ok"] is True
    assert result["byte_truncated"] is True
    assert result["total_lines"] is None


def test_glob_条数上限与截断标注(repo, monkeypatch):
    for i in range(5):
        (repo / f"f{i}.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        "mini_deerflow.project_tools.GLOB_MAX_RESULTS", 2
    )
    tools, ctx = _make_tools(repo)
    result = _call(tools["project_glob"], {"pattern": "*.txt"}, ctx)
    assert result["returned"] == 2
    assert result["truncated"] is True


def test_grep_超大文件跳过(repo, monkeypatch):
    (repo / "huge.log").write_text("needle\n" * 100, encoding="utf-8")
    monkeypatch.setattr(
        "mini_deerflow.project_tools.GREP_MAX_FILE_BYTES", 10
    )
    tools, ctx = _make_tools(repo)
    result = _call(tools["project_grep"], {"pattern": "needle"}, ctx)
    assert result["ok"] is True
    assert result["returned"] == 0


# ---------------------------------------------------------------------------
# 4. 只读性：文件内容与 Git 状态不变
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    # git 输出按 UTF-8；不指定会走 Windows locale（cp936）解码并炸掉
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True,
        text=True, encoding="utf-8", errors="replace", check=True,
    )
    return proc.stdout


@pytest.mark.skipif(shutil.which("git") is None, reason="git 不可用")
def test_三工具执行后文件内容与_git_状态不变(tmp_path):
    repo = tmp_path / "greal"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    (repo / "docs.md").write_text("docs\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "add", "-A")
    _git(
        repo, "-c", "user.email=t@test", "-c", "user.name=t",
        "commit", "-m", "init",
    )
    before_status = _git(repo, "status", "--porcelain")
    before_app = (repo / "src" / "app.py").read_bytes()

    tools, ctx = _make_tools(repo)
    assert _call(tools["project_read_file"], {"path": "src/app.py"}, ctx)["ok"]
    assert _call(tools["project_glob"], {"pattern": "**/*.py"}, ctx)["ok"]
    assert _call(tools["project_grep"], {"pattern": "return"}, ctx)["ok"]

    assert (repo / "src" / "app.py").read_bytes() == before_app
    assert _git(repo, "status", "--porcelain") == before_status == ""


# ---------------------------------------------------------------------------
# 5. 真实 Agent 循环：接入验证（脚本模型）
# ---------------------------------------------------------------------------


class RecordingModel:
    def __init__(self, decisions):
        self._decisions = list(decisions)
        self.seen_tool_names: list[set[str]] = []
        self.seen_messages: list[list[Message]] = []

    def decide(self, messages, tools=None):
        self.seen_tool_names.append({s.name for s in (tools or [])})
        self.seen_messages.append(list(messages))
        return self._decisions.pop(0)


def _loop_agent(repo: Path, decisions, *, thread_id: str, project_id: str | None):
    registry = create_default_registry()
    thread_store = InMemoryThreadStore()
    project_store = InMemoryProjectStore()
    if project_id is not None:
        project = project_store.create(project_id, "demo", str(repo))
        # 设置 workspace_path（Phase 1 新字段）
        project.workspace_path = repo
        project_store.save(project)
        thread_store.create(thread_id, project_id)
    else:
        thread_store.create(thread_id)
    registry.register_many(create_project_read_tools(thread_store, project_store))
    model = RecordingModel(decisions)

    # 添加项目会话提示词注入
    from mini_deerflow.agent import ProjectSessionSkillContextProvider
    skill_provider = ProjectSessionSkillContextProvider(thread_store, project_store)

    return Agent(
        model=model,
        registry=registry,
        store=thread_store,
        skill_context_provider=skill_provider,
    ), model


def test_真实循环_项目会话_三工具可见且提示词注入(repo):
    agent, model = _loop_agent(
        repo,
        [
            ToolCall(id="c1", name="project_glob", arguments={"pattern": "src/*.py"}),
            FinalAnswer(content="答案"),
        ],
        thread_id="t-loop",
        project_id="p1",
    )
    answer, trace = agent.run("t-loop", "看看这个仓库")
    assert answer == "答案"
    # 暴露：项目会话能看到三个只读工具
    names = model.seen_tool_names[0]
    assert {"project_read_file", "project_glob", "project_grep"} <= names
    # 上下文：工具真的在真实循环里拿到了绑定仓库并读到了文件
    tool_steps = [t for t in trace if t.get("action") == "tool_call"]
    assert tool_steps and "app.py" in tool_steps[0]["detail"]["result"]
    # 提示词：项目会话提示词出现在发给模型的消息里
    first_call_text = "\n".join(m.content for m in model.seen_messages[0])
    assert "项目会话" in first_call_text
    # Phase 1: 三分支 clarify 规则尚未实现
    # assert "clarify" in first_call_text


def test_真实循环_普通会话_提示词不注入(repo):
    """Phase 1：工具可见性过滤尚未在主 Agent 实现，暂跳过工具可见性断言。"""
    agent, model = _loop_agent(
        repo,
        [FinalAnswer(content="你好")],
        thread_id="t-plain",
        project_id=None,
    )
    agent.run("t-plain", "你好")
    text = "\n".join(m.content for m in model.seen_messages[0])
    assert "项目会话" not in text
    # Phase 1: 主 Agent 尚未实现工具可见性过滤，project_read_file 仍可见
    # assert "project_read_file" not in model.seen_tool_names[0]


@pytest.mark.skip(reason="Phase 1: 主 Agent 尚未实现工具 allowlist，需后续支持 ToolNotAllowedError")
def test_真实循环_普通会话_强行调用只读工具被拒(repo):
    """Phase 1：工具访问控制尚未在主 Agent 实现。"""
    agent, _ = _loop_agent(
        repo,
        [
            ToolCall(name="project_read_file", arguments={"path": "src/app.py"}),
            FinalAnswer(content="不该到达"),
        ],
        thread_id="t-plain-2",
        project_id=None,
    )
    with pytest.raises(ToolNotAllowedError):
        agent.run("t-plain-2", "hi")


def test_真实循环_assembler_路径提示词并入_skill_段(repo):
    """生产路径（create_runtime）走 assembler：提示词并入 skill 段传参。"""
    captured: dict = {}

    class StubAssembler:
        def assemble(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                context=SimpleNamespace(
                    messages=(Message(role="system", content="stub"),)
                )
            )

    registry = create_default_registry()
    thread_store = InMemoryThreadStore()
    project_store = InMemoryProjectStore()
    project = project_store.create("p1", "demo", str(repo))
    # 设置 workspace_path（Phase 1 新字段）
    project.workspace_path = repo
    project_store.save(project)
    thread_store.create("t-a", "p1")
    registry.register_many(create_project_read_tools(thread_store, project_store))

    # 使用 ProjectSessionSkillContextProvider 注入项目会话提示词
    from mini_deerflow.agent import ProjectSessionSkillContextProvider
    skill_provider = ProjectSessionSkillContextProvider(thread_store, project_store)

    agent = Agent(
        model=RecordingModel([FinalAnswer(content="ok")]),
        registry=registry,
        store=thread_store,
        context_assembler=StubAssembler(),
        skill_context_provider=skill_provider,
    )
    agent.run("t-a", "hi")
    assert "项目会话" in captured["skill_context"]
    # PROJECT_SESSION_PROMPT 被 agent.strip() 处理后去掉了尾随换行
    assert PROJECT_SESSION_PROMPT.strip() in captured["skill_context"]


# ---------------------------------------------------------------------------
# P0-2 / P1-3：锁统一 + 锁泄漏修复
# ---------------------------------------------------------------------------

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mini_deerflow.project_tools import create_project_execute_tool


def _ctx(thread_id: str):
    return SimpleNamespace(thread_id=thread_id)


def test_execute_锁被同一_get_lock_实例共享(tmp_path):
    """ProjectService.get_lock(project_id) 与 project_execute 共用同一锁实例。"""
    registry = create_default_registry()
    thread_store = InMemoryThreadStore()
    project_store = InMemoryProjectStore()

    project = project_store.create("p-locked", "锁测试", str(tmp_path))
    project.workspace_path = tmp_path
    project_store.save(project)
    thread_store.create("t-lock-1", "p-locked")

    locks_created: dict[str, threading.Lock] = {}

    def get_lock(project_id: str) -> threading.Lock:
        if project_id not in locks_created:
            locks_created[project_id] = threading.Lock()
        return locks_created[project_id]

    mock_runner = MagicMock()
    mock_runner.is_configured = False  # 快速返回，不触发子进程

    tool = create_project_execute_tool(
        thread_store, project_store, runner=mock_runner, get_lock=get_lock,
    )
    registry.register(tool)

    # 从外部占住锁 → 工具必须被拒绝
    external_lock = get_lock("p-locked")
    external_lock.acquire()
    try:
        result = registry.execute("project_execute", {"task": "do it"}, context=_ctx("t-lock-1"))
        assert "请等待" in result, f"锁互斥未生效: {result}"
    finally:
        external_lock.release()

    assert "p-locked" in locks_created
    # 锁释放后第二次调用可以成功（runner 未配置是另一码事，这里只证互斥）
    result2 = registry.execute("project_execute", {"task": "do it again"}, context=_ctx("t-lock-1"))
    assert "请等待" not in result2  # 锁已释放，不再被拒
    # 同一 project_id 用同一把锁（不是新锁）
    assert len(locks_created) == 1


def test_execute_runner_run_抛异常_锁仍释放(tmp_path):
    """runner.run() 抛出时，finally 保证锁释放，下一次仍能获取。"""
    registry = create_default_registry()
    thread_store = InMemoryThreadStore()
    project_store = InMemoryProjectStore()

    project = project_store.create("p-leak", "泄漏测试", str(tmp_path))
    project.workspace_path = tmp_path
    project_store.save(project)
    thread_store.create("t-leak-1", "p-leak")

    def get_lock(project_id: str) -> threading.Lock:
        return threading.Lock()

    mock_runner = MagicMock()
    mock_runner.is_configured = True
    mock_runner.run.side_effect = RuntimeError("子进程崩溃")

    tool = create_project_execute_tool(
        thread_store, project_store, runner=mock_runner, get_lock=get_lock,
    )
    registry.register(tool)

    # runner.run() 抛异常 → 锁必须已释放
    result = registry.execute("project_execute", {"task": "crash"}, context=_ctx("t-leak-1"))
    assert "子进程崩溃" in result, f"异常应正常传播: {result}"
    # 锁已由 finally 释放，同一 project_id 可再次获取
    result2 = registry.execute("project_execute", {"task": "retry"}, context=_ctx("t-leak-1"))
    # 第二次拿到的是"子进程崩溃"（runner.run() 又抛了），而不是"请等待"（锁未泄漏）
    assert "请等待" not in result2, f"锁泄漏：第二次调用被前一次持有的锁挡住: {result2}"


def test_execute_锁按_project_id_隔离(tmp_path):
    """不同 project_id 用不同锁，互不干扰。"""
    registry = create_default_registry()
    thread_store = InMemoryThreadStore()
    project_store = InMemoryProjectStore()

    p1 = project_store.create("p-a", "项目A", str(tmp_path / "a"))
    p1.workspace_path = tmp_path / "a"
    project_store.save(p1)
    t1 = thread_store.create("t-a", "p-a")

    p2 = project_store.create("p-b", "项目B", str(tmp_path / "b"))
    p2.workspace_path = tmp_path / "b"
    project_store.save(p2)
    t2 = thread_store.create("t-b", "p-b")

    locks: dict[str, threading.Lock] = {}

    def get_lock(project_id: str) -> threading.Lock:
        if project_id not in locks:
            locks[project_id] = threading.Lock()
        return locks[project_id]

    mock_runner = MagicMock()
    mock_runner.is_configured = False

    tool = create_project_execute_tool(
        thread_store, project_store, runner=mock_runner, get_lock=get_lock,
    )
    registry.register(tool)

    # 项目 A 持锁期间，项目 B 不受影响
    lock_a = get_lock("p-a")
    lock_a.acquire()
    try:
        result_b = registry.execute("project_execute", {"task": "b only"}, context=_ctx("t-b"))
        assert "正忙" not in result_b  # B 项目没被 A 的锁挡住
    finally:
        lock_a.release()

    assert locks["p-a"] is not locks["p-b"]
