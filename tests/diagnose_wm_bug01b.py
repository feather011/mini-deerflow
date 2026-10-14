"""WM-BUG-01B 诊断"""
import json, sys, os, tempfile, uuid, queue as queue_module, threading as threading_module
from pathlib import Path

os.environ["MINI_DEERFLOW_MCP_CONFIG"] = ""
os.environ["MINI_DEERFLOW_PROVIDER"] = "fake"
TMP = tempfile.mkdtemp()
os.environ["MINI_DEERFLOW_WORKSPACES_ROOT"] = TMP
os.environ["MINI_DEERFLOW_HARNESS_ROOT"] = "D:/项目解读/harnessagent"
os.environ["HARNESS_USE_FAKE_LLM"] = "1"
sys.path.insert(0, "D:/项目解读/Deerflow/mini-deerflow/src")

from mini_deerflow.project_store import InMemoryProjectStore
from mini_deerflow.thread_store import InMemoryThreadStore
from mini_deerflow.tools import create_default_registry
from mini_deerflow.runtime import RuntimeConfig, create_runtime
from mini_deerflow.agent import compute_thread_tool_allow_list
from mini_deerflow.models import ToolCall, FinalAnswer
from mini_deerflow.api.services.projects import ProjectService
from mini_deerflow.api.schemas.streaming import RunEvent


class TracingModel:
    def __init__(self):
        self.calls = []
        self.first_tools = None

    def decide(self, messages, tools=None):
        tool_names = [t.name for t in (tools or [])]
        if self.first_tools is None:
            self.first_tools = tool_names
        self.calls.append({"n": len(messages), "tools": len(tools) if tools else 0})
        tool_msgs = [m for m in messages if m.role == "tool"]
        if not tool_msgs:
            print(f"\n=== D1: 模型收到的工具 ===")
            print(f"  数量: {len(tool_names)}")
            print(f"  project_execute: {'YES' if 'project_execute' in tool_names else 'NO'}")
            if "project_execute" not in tool_names:
                print(f"  全部: {tool_names}")
            if "project_execute" in tool_names:
                return ToolCall(name="project_execute",
                    arguments={"task": "创建 manual_loop_check_v2.py，打印 0~4 并运行", "context": ""})
            return FinalAnswer(content="project_execute不可用")
        return FinalAnswer(content=f"done({len(tool_msgs)})")


def main():
    print("="*50)
    print("WM-BUG-01B DIAGNOSTIC")
    print("="*50)

    ps = InMemoryProjectStore()
    store = InMemoryThreadStore()
    registry = create_default_registry()
    config = RuntimeConfig(harness_root="D:/项目解读/harnessagent")
    runtime = create_runtime(store=store, project_store=ps, registry=registry, config=config)

    # D2: 白名单
    allow = compute_thread_tool_allow_list(registry, "any-project")
    print(f"\n=== D2: 白名单 ===")
    print(f"  allow: {allow}")

    # D3: 注册表
    all_names = [t.name for t in registry.list_tools()]
    print(f"\n=== D3: 注册表 ===")
    print(f"  project_execute: {'YES' if 'project_execute' in all_names else 'NO'}")

    # D4: Agent registry
    agent = runtime.integrated_agent._agent
    print(f"\n=== D4: Agent工具spec ===")
    specs = registry.tool_specs()
    print(f"  数量: {len(specs)}")
    for s in specs:
        if "project" in s.name.lower():
            print(f"  {s.name}: {(s.description or '')[:100]}...")

    # 创建项目+Thread
    ps_svc = ProjectService(store=ps, workspaces_root=TMP, thread_store=store)
    proj = ps_svc.create("b01b-test")
    tid = f"t-{uuid.uuid4().hex[:8]}"
    store.create(tid, project_id=proj.project_id)
    print(f"\n  project_id: {proj.project_id}")
    print(f"  thread_id: {tid}")

    # D5: 白名单对具体项目
    allow_proj = compute_thread_tool_allow_list(registry, proj.project_id)
    print(f"\n=== D5: 项目白名单 ===")
    print(f"  allow: {allow_proj}")
    print(f"  含project_execute: {allow_proj is None or 'project_execute' in allow_proj}")

    # D6: 执行
    print(f"\n=== D6: 执行 ===")
    model = TracingModel()
    agent._model = model
    ans, _ = runtime.integrated_agent.run(thread_id=tid,
        user_input="创建 manual_loop_check_v2.py，打印 0~4 并运行")
    print(f"  模型调用: {len(model.calls)} 次")
    print(f"  首次工具: {model.first_tools}")
    print(f"  答案: {ans[:100]}")

    # D7: workspace文件
    ws = proj.workspace_path
    print(f"\n=== D7: workspace文件 ===")
    if ws and ws.exists():
        files = list(ws.glob("*"))
        print(f"  文件数: {len(files)}")
        for f in files:
            if f.is_file(): print(f"    {f.name}")
    else:
        print(f"  不存在: {ws}")

    # D8: run.result事件
    print(f"\n=== D8: run.result事件 ===")
    run_id = f"diag-{uuid.uuid4().hex[:8]}"
    broker = runtime.run_event_broker
    if broker:
        q = broker.subscribe(run_id)
        received = []
        def collect():
            for _ in range(200):
                try:
                    item = q.get(timeout=0.05)
                    received.append(item)
                except queue_module.Empty:
                    continue
        t = threading_module.Thread(target=collect, daemon=True)
        t.start()
        model2 = TracingModel()
        agent._model = model2
        ans2, _ = runtime.integrated_agent.run(thread_id=tid,
            user_input="写文件x", run_id=run_id)
        t.join(timeout=5)
        broker.unsubscribe(run_id)
        evs = [e for e in received if isinstance(e, RunEvent)]
        rr = [e for e in evs if e.event_type.value == "run.result"]
        print(f"  事件总数: {len(evs)}")
        print(f"  run.result数: {len(rr)}")
        if rr:
            tr = rr[0].tool_result
            if tr:
                try:
                    p = json.loads(tr)
                    print(f"  status: {p.get('status')}")
                    print(f"  files: {p.get('files_written', [])}")
                    print(f"  verif: {len(p.get('verification', []))} 条")
                except:
                    print(f"  raw: {tr[:200]}")
    else:
        print("  broker=None")

    print("\n=== DONE ===")


if __name__ == "__main__":
    main()
