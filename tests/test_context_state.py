"""Phase 24 / 7.1 上下文领域模型。

覆盖规格 7.1 列出的每一项：
ContextState 序列化与反序列化、schema_version、Summary 清理与限额、
TaskNote 状态转换、非法 category/status、旧数据兼容。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mini_deerflow.context_errors import (
    TaskNoteLimitExceededError,
    TaskNoteNotFoundError,
    TaskNoteValidationError,
)
from mini_deerflow.context_state import (
    CONTEXT_SCHEMA_VERSION,
    SUMMARY_MAX_ITEMS_PER_FIELD,
    SUMMARY_MAX_NARRATIVE_CHARS,
    SUMMARY_MAX_TOTAL_CHARS,
    SUMMARY_MAX_TOTAL_ITEMS,
    TASK_NOTE_MAX_ACTIVE,
    TASK_NOTE_MAX_CONTENT_CHARS,
    TASK_NOTE_MAX_TOTAL,
    ConversationSummary,
    ContextPolicy,
    ContextState,
    REDACTED_PLACEHOLDER,
    TaskNote,
    TaskNoteCategory,
    TaskNoteStatus,
    contains_sensitive,
    empty_context_state,
    scrub_sensitive,
)

NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)


def _note(note_id: str = "n1", *, category: TaskNoteCategory = TaskNoteCategory.DECISION,
           content: str = "用 SQLite 存储", status: TaskNoteStatus = TaskNoteStatus.ACTIVE) -> TaskNote:
    return TaskNote(
        note_id=note_id,
        category=category,
        content=content,
        status=status,
        source_run_id="run-1",
        created_at=NOW,
        updated_at=NOW,
    )


def _summary(**overrides) -> ConversationSummary:
    payload: dict = dict(
        narrative="用户要求实现离线测试",
        facts=["已确认 SQLite 可用"],
        constraints=["禁止调用真实模型"],
        decisions=["使用内存 store 作为默认"],
        failed_attempts=[],
        open_items=["补文档"],
        summarized_from=0,
        summarized_through=7,
    )
    payload.update(overrides)
    return ConversationSummary.build(**payload)


# ---------------------------------------------------------------------------
# TaskNote
# ---------------------------------------------------------------------------


def test_task_note_create_strips_and_defaults_active() -> None:
    note = TaskNote.create(
        note_id="n1",
        category=TaskNoteCategory.CONSTRAINT,
        content="  不许联网  ",
        source_run_id="run-9",
        now=NOW,
    )
    assert note.content == "不许联网"
    assert note.status is TaskNoteStatus.ACTIVE
    assert note.created_at == note.updated_at == NOW


def test_task_note_state_transitions() -> None:
    note = _note()
    assert note.status is TaskNoteStatus.ACTIVE

    resolved = note.resolve(now=NOW)
    assert resolved.status is TaskNoteStatus.RESOLVED
    assert resolved.note_id == note.note_id  # id 不变
    assert resolved.created_at == note.created_at  # 创建时间不被覆盖

    # resolve 幂等
    again = resolved.resolve(now=NOW)
    assert again.status is TaskNoteStatus.RESOLVED

    # update 只改正文与时间戳，状态与类别都不动。
    # 领域层不拦 resolved 的 update；拒绝「已解决后改写」的是 task_note 工具
    # （审计线索不能事后抹掉），那条规则在 test_context_task_notes 里。
    updated = resolved.with_content("新约束", now=NOW)
    assert updated.content == "新约束"
    assert updated.status is TaskNoteStatus.RESOLVED
    assert updated.category is note.category


def test_task_note_rejects_oversize_content() -> None:
    with pytest.raises(ValueError, match="超长"):
        TaskNote.create(
            note_id="n1",
            category=TaskNoteCategory.NEXT_STEP,
            content="x" * (TASK_NOTE_MAX_CONTENT_CHARS + 1),
            source_run_id=None,
        )


def test_task_note_rejects_empty_content() -> None:
    with pytest.raises(ValueError):
        TaskNote.create(
            note_id="n1",
            category=TaskNoteCategory.NEXT_STEP,
            content="   ",
            source_run_id=None,
        )


def test_task_note_rejects_empty_id() -> None:
    with pytest.raises(ValueError):
        TaskNote(
            note_id="  ",
            category=TaskNoteCategory.DECISION,
            content="x",
            status=TaskNoteStatus.ACTIVE,
            source_run_id=None,
            created_at=NOW,
            updated_at=NOW,
        )


def test_task_note_rejects_non_enum_category_and_status() -> None:
    # dataclass 构造期就拒绝，不会让非法值溜进 ContextState
    with pytest.raises(ValueError, match="category 非法"):
        TaskNote(
            note_id="n1",
            category="decision",  # type: ignore[arg-type]
            content="x",
            status=TaskNoteStatus.ACTIVE,
            source_run_id=None,
            created_at=NOW,
            updated_at=NOW,
        )
    with pytest.raises(ValueError, match="status 非法"):
        TaskNote(
            note_id="n1",
            category=TaskNoteCategory.DECISION,
            content="x",
            status="active",  # type: ignore[arg-type]
            source_run_id=None,
            created_at=NOW,
            updated_at=NOW,
        )


def test_task_note_roundtrip_preserves_fields() -> None:
    note = _note()
    restored = TaskNote.from_dict(note.to_dict())
    assert restored == note
    assert restored.to_dict() == note.to_dict()


def test_task_note_from_dict_rejects_unknown_category() -> None:
    data = _note().to_dict()
    data["category"] = "wishlist"
    with pytest.raises(ValueError, match="category 非法"):
        TaskNote.from_dict(data)


def test_task_note_from_dict_rejects_unknown_status() -> None:
    data = _note().to_dict()
    data["status"] = "deleted"
    with pytest.raises(ValueError, match="status 非法"):
        TaskNote.from_dict(data)


def test_task_note_missing_optional_run_id_roundtrips() -> None:
    """source_run_id 缺省时仍可恢复——老数据没有这个字段。"""
    data = _note().to_dict()
    del data["source_run_id"]
    restored = TaskNote.from_dict(data)
    assert restored.source_run_id is None


# ---------------------------------------------------------------------------
# ConversationSummary：清理与限额
# ---------------------------------------------------------------------------


def test_summary_drops_empty_and_duplicate_items() -> None:
    summary = _summary(facts=["a", "a", "  ", "", "b", "a"])
    assert summary.facts == ("a", "b")


def test_summary_strips_items() -> None:
    summary = _summary(constraints=["  必须离线  "])
    assert summary.constraints == ("必须离线",)


def test_summary_narrative_is_stripped() -> None:
    summary = _summary(narrative="   有前后空白   ")
    assert summary.narrative == "有前后空白"


@pytest.mark.parametrize(
    "text,leaked",
    [
        ("用户给的 key 是 sk-ABCDEFGHIJKLMNOPQRSTUVWX 要用", "sk-ABCDEFGHIJKLMNOPQRSTUVWX"),
        ("token: ghp_1234567890abcdefghijklmnopqrstuv", "ghp_1234567890abcdefghijklmnopqrstuv"),
        ("Header 写 Authorization: Bearer abcdefghijklmnop1234", "abcdefghijklmnop1234"),
        ("数据库 password=hunter2hunter2", "hunter2hunter2"),
        ("用的是 api_key = AKIA1234567890ABCDEF", "AKIA1234567890ABCDEF"),
    ],
)
def test_summary_scrubs_credentials_in_narrative(text: str, leaked: str) -> None:
    """持久化前的**确定性过滤**：叙述里的凭据被替换掉，摘要本身保留。

    不依赖 Summary Prompt（那是概率约束），而是在数据落库的必经之路上兜底。
    """
    summary = _summary(narrative=text)

    assert leaked not in summary.narrative, f"凭据未被过滤: {leaked}"
    assert REDACTED_PLACEHOLDER in summary.narrative
    # 摘要仍在，且其余字段未受影响
    assert summary.narrative
    assert summary.facts == ("已确认 SQLite 可用",)


def test_scrub_sensitive_reports_whether_it_changed_anything() -> None:
    clean, changed = scrub_sensitive("用户要求实现离线测试")
    assert clean == "用户要求实现离线测试"
    assert changed is False

    dirty, changed = scrub_sensitive("key 是 sk-ABCDEFGHIJKLMNOPQRSTUVWX")
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWX" not in dirty
    assert changed is True


@pytest.mark.parametrize(
    "path",
    [
        # POSIX 敏感文件
        "/etc/shadow",
        "/etc/passwd",
        "/home/alice/.ssh/id_rsa",
        "/root/.aws/credentials",
        "~/.gnupg/secring.gpg",
        "/var/run/secrets/kubernetes.io/token",
        # Windows 敏感路径
        r"C:\Users\bob\.ssh\id_ed25519",
        r"C:\Users\bob\AppData\Roaming\Microsoft\Credentials\ABCD",
        r"C:\Windows\System32\config\SAM",
        # 私钥与证书
        "-----BEGIN RSA PRIVATE KEY-----",
        "id_rsa",
        "server.pem",
        "cert.pfx",
        # 环境文件
        ".env",
        ".env.production",
    ],
)
def test_summary_scrubs_sensitive_paths(path: str) -> None:
    """敏感绝对路径同样在持久化前过滤——这是凭据泄漏最常见的形态。"""
    summary = _summary(narrative=f"配置文件在 {path} 需要检查")
    assert path not in summary.narrative
    assert REDACTED_PLACEHOLDER in summary.narrative


def test_summary_keeps_ordinary_paths() -> None:
    """普通路径不受影响：过滤不能把无害内容也一并吃掉。"""
    safe = "/app/src/config/service.py 和 D:\\projects\\app\\main.ts"
    summary = _summary(narrative=f"改动位于 {safe}")
    assert "/app/src/config/service.py" in summary.narrative
    assert REDACTED_PLACEHOLDER not in summary.narrative


def test_summary_rejects_empty_narrative() -> None:
    """空叙述不能落库——摘要的价值就在于那句连贯概述。"""
    with pytest.raises(ValueError, match="不能为空"):
        _summary(narrative="   ")


def test_purely_sensitive_narrative_becomes_placeholder() -> None:
    """原文整段都是凭据时，落库的是占位符：既不是空，更不是原样。"""
    summary = _summary(narrative="sk-ABCDEFGHIJKLMNOPQRSTUVWX")
    assert summary.narrative == REDACTED_PLACEHOLDER
    assert "sk-" not in summary.narrative


@pytest.mark.parametrize(
    "text",
    [
        "key 是 sk-ABCDEFGHIJKLMNOPQRSTUVWX",
        "Authorization: Bearer abcdefghijklmnop1234",
        "password=hunter2hunter2",
    ],
)
def test_summary_drops_credentials_in_items(text: str) -> None:
    """数组条目里的凭据整条剔除（不是打码，是丢掉）。"""
    summary = _summary(facts=["正常事实", text])
    assert summary.facts == ("正常事实",)


def test_summary_enforces_per_field_item_limit() -> None:
    items = [f"条目{i}" for i in range(SUMMARY_MAX_ITEMS_PER_FIELD + 5)]
    summary = _summary(facts=items)
    assert len(summary.facts) <= SUMMARY_MAX_ITEMS_PER_FIELD


def test_summary_enforces_total_item_limit() -> None:
    # 单字段未超限，但五项合计超过总条目上限 → 必须拒绝而不是截断
    per_field = SUMMARY_MAX_ITEMS_PER_FIELD
    big = [f"条目{i}" for i in range(per_field)]
    with pytest.raises(ValueError, match="条目总数超限"):
        _summary(
            facts=big,
            constraints=big,
            decisions=big,
            failed_attempts=big,
            open_items=big,
        )


def test_summary_enforces_total_char_limit() -> None:
    # 注意每条必须互不相同：_clean_items 会去重，
    # 全同的条目会被折叠成一条，反而测不到字符上限。
    def block(tag: str) -> str:
        return f"{tag}:" + "字" * 500

    with pytest.raises(ValueError, match="总字符超限"):
        _summary(
            facts=[block(f"事实{i}") for i in range(5)],
            constraints=[block(f"约束{i}") for i in range(4)],
        )


def test_summary_enforces_narrative_limit() -> None:
    with pytest.raises(ValueError, match="narrative 超长"):
        _summary(narrative="长" * (SUMMARY_MAX_NARRATIVE_CHARS + 1))


def test_summary_rejects_inverted_range() -> None:
    with pytest.raises(ValueError, match="范围倒挂"):
        _summary(summarized_from=7, summarized_through=3)


def test_summary_rejects_non_integer_range() -> None:
    with pytest.raises(ValueError):
        _summary(summarized_from="0", summarized_through=7)  # type: ignore[arg-type]


def test_summary_rejects_wrong_schema_version() -> None:
    with pytest.raises(ValueError, match="schema_version"):
        ConversationSummary(
            narrative="x",
            facts=(),
            constraints=(),
            decisions=(),
            failed_attempts=(),
            open_items=(),
            summarized_from=0,
            summarized_through=1,
            schema_version=999,
        )


def test_summary_roundtrip() -> None:
    summary = _summary()
    assert ConversationSummary.from_dict(summary.to_dict()) == summary


def test_summary_merge_unions_ranges_and_items() -> None:
    older = _summary(summarized_from=0, summarized_through=4, facts=["旧事实"])
    newer = _summary(
        narrative="第二段叙述",
        summarized_from=5,
        summarized_through=9,
        facts=["新事实"],
        decisions=["新决策"],
    )
    merged = older.merge(newer)
    assert merged.summarized_from == 0
    assert merged.summarized_through == 9
    assert merged.facts == ("旧事实", "新事实")
    assert "新事实" in merged.facts
    assert merged.narrative.endswith("第二段叙述") or "第二段叙述" in merged.narrative


def test_summary_merge_deduplicates() -> None:
    older = _summary(facts=["同一条约束"])
    newer = _summary(facts=["同一条约束"], summarized_from=8, summarized_through=9)
    merged = older.merge(newer)
    assert merged.facts.count("同一条约束") == 1


# ---------------------------------------------------------------------------
# ContextState
# ---------------------------------------------------------------------------


def test_empty_state_defaults() -> None:
    state = empty_context_state()
    assert state.summary is None
    assert state.task_notes == ()
    assert state.revision == 0
    assert state.summarized_through is None
    assert state.indexed_through is None
    assert state.compaction_count == 0
    assert state.schema_version == CONTEXT_SCHEMA_VERSION


def test_context_state_roundtrip_with_all_fields() -> None:
    state = ContextState(
        summary=_summary(),
        task_notes=(_note("n1"), _note("n2", status=TaskNoteStatus.RESOLVED)),
        revision=4,
        summarized_through=7,
        indexed_through=9,
        compaction_count=2,
    )
    restored = ContextState.from_dict(state.to_dict())
    assert restored == state
    assert restored.to_dict() == state.to_dict()


def test_context_state_roundtrip_empty() -> None:
    state = empty_context_state()
    assert ContextState.from_dict(state.to_dict()) == state


def test_context_state_rejects_unknown_schema_version() -> None:
    data = empty_context_state().to_dict()
    data["schema_version"] = 999
    with pytest.raises(ValueError, match="schema_version"):
        ContextState.from_dict(data)


def test_context_state_rejects_negative_revision_and_count() -> None:
    with pytest.raises(ValueError, match="revision"):
        ContextState(revision=-1)
    with pytest.raises(ValueError, match="compaction_count"):
        ContextState(compaction_count=-1)


def test_context_state_rejects_negative_watermarks() -> None:
    with pytest.raises(ValueError, match="summarized_through"):
        ContextState(summarized_through=-3)


def test_context_state_rejects_non_note_elements() -> None:
    with pytest.raises(ValueError, match="TaskNote"):
        ContextState(task_notes=("not-a-note",))  # type: ignore[arg-type]


def test_context_state_active_and_resolved_partition() -> None:
    state = ContextState(
        task_notes=(
            _note("a"),
            _note("b", status=TaskNoteStatus.RESOLVED),
            _note("c"),
        )
    )
    assert {n.note_id for n in state.active_notes} == {"a", "c"}
    assert {n.note_id for n in state.resolved_notes} == {"b"}


def test_context_state_add_note_appends() -> None:
    state = empty_context_state().add_note(_note("n1"))
    assert state.find_note("n1") is not None
    assert len(state.task_notes) == 1


def test_context_state_add_duplicate_note_rejected() -> None:
    state = empty_context_state().add_note(_note("n1"))
    with pytest.raises(TaskNoteValidationError, match="已存在"):
        state.add_note(_note("n1"))


def test_context_state_update_missing_note_raises() -> None:
    state = empty_context_state()
    with pytest.raises(TaskNoteNotFoundError):
        state.update_note(_note("ghost"))


def test_context_state_update_replaces_by_id() -> None:
    state = empty_context_state().add_note(_note("n1")).add_note(_note("n2"))
    resolved = state.find_note("n1").resolve()  # type: ignore[union-attr]
    updated = state.update_note(resolved)
    assert len(updated.task_notes) == 2
    assert updated.find_note("n1").status is TaskNoteStatus.RESOLVED  # type: ignore[union-attr]
    assert updated.find_note("n2").status is TaskNoteStatus.ACTIVE  # type: ignore[union-attr]


def test_context_state_enforces_active_limit() -> None:
    state = empty_context_state()
    for index in range(TASK_NOTE_MAX_ACTIVE):
        state = state.add_note(_note(f"n{index}"))
    with pytest.raises(TaskNoteLimitExceededError) as excinfo:
        state.add_note(_note("overflow"))
    assert excinfo.value.limit_name == "active_notes"
    assert excinfo.value.limit == TASK_NOTE_MAX_ACTIVE


def test_context_state_enforces_total_limit_after_resolving() -> None:
    """把 Note 全 resolve 后仍受总数上限约束——否则可以用循环刷到无限。

    每条立刻 resolve，因此 active 名额始终空着，命中的是 total 上限。
    """
    state = empty_context_state()
    for index in range(TASK_NOTE_MAX_TOTAL):
        state = state.add_note(_note(f"n{index}"))
        state = state.update_note(state.find_note(f"n{index}").resolve())  # type: ignore[union-attr]
    assert len(state.active_notes) == 0
    with pytest.raises(TaskNoteLimitExceededError) as excinfo:
        state.add_note(_note("overflow"))
    assert excinfo.value.limit_name == "total_notes"
    assert excinfo.value.limit == TASK_NOTE_MAX_TOTAL


def test_context_state_resolved_note_frees_active_quota() -> None:
    state = empty_context_state()
    for index in range(TASK_NOTE_MAX_ACTIVE):
        state = state.add_note(_note(f"n{index}"))
    first = state.find_note("n0")
    state = state.update_note(first.resolve())  # type: ignore[union-attr]
    # 空出一个 active 名额后可以再加
    state = state.add_note(_note("after-resolve"))
    assert len(state.active_notes) == TASK_NOTE_MAX_ACTIVE


def test_legacy_dict_without_new_fields_loads() -> None:
    """旧数据缺 indexed_through / compaction_count → 走默认值。"""
    legacy = {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "revision": 0,
        "summary": None,
        "task_notes": [],
        "summarized_through": 5,
    }
    state = ContextState.from_dict(legacy)
    assert state.summarized_through == 5
    assert state.indexed_through is None
    assert state.compaction_count == 0


# ---------------------------------------------------------------------------
# ContextPolicy
# ---------------------------------------------------------------------------


def test_context_policy_formula() -> None:
    policy = ContextPolicy(
        max_context_tokens=8000,
        reserved_output_tokens=1000,
        safety_margin_tokens=500,
        compaction_threshold=0.8,
    )
    assert policy.hard_input_limit == 6500  # 8000 - 1000 - 500
    assert policy.soft_compaction_limit == 5200  # int(6500 * 0.8)


def test_context_policy_section_caps_scale_with_hard_limit() -> None:
    policy = ContextPolicy(
        max_context_tokens=10000,
        reserved_output_tokens=1000,
        safety_margin_tokens=1000,
    )
    hard = policy.hard_input_limit
    assert policy.cap("summary") == int(hard * 0.15)
    assert policy.cap("task_notes") == int(hard * 0.10)
    assert policy.cap("memory") == int(hard * 0.10)
    assert policy.cap("skills") == int(hard * 0.15)


def test_context_policy_rejects_unknown_section() -> None:
    policy = ContextPolicy(max_context_tokens=8000, reserved_output_tokens=0, safety_margin_tokens=0)
    with pytest.raises(ValueError, match="未知弹性区块"):
        policy.cap("unknown")


def test_context_policy_rejects_non_positive_hard_limit() -> None:
    with pytest.raises(ValueError, match="硬输入上限非正"):
        ContextPolicy(
            max_context_tokens=1000,
            reserved_output_tokens=900,
            safety_margin_tokens=200,
        )


def test_context_policy_validates_bounds() -> None:
    # 每个用例都显式给出全部必填项，避免与共用 dict 的键冲突
    with pytest.raises(ValueError, match="max_context_tokens"):
        ContextPolicy(max_context_tokens=0, reserved_output_tokens=0, safety_margin_tokens=0)
    with pytest.raises(ValueError, match="不能为负"):
        ContextPolicy(max_context_tokens=8000, reserved_output_tokens=-1, safety_margin_tokens=100)
    with pytest.raises(ValueError, match="不能为负"):
        ContextPolicy(max_context_tokens=8000, reserved_output_tokens=100, safety_margin_tokens=-1)
    with pytest.raises(ValueError, match="compaction_threshold"):
        ContextPolicy(
            max_context_tokens=8000,
            reserved_output_tokens=100,
            safety_margin_tokens=100,
            compaction_threshold=1.5,
        )
    with pytest.raises(ValueError, match="compaction_threshold"):
        ContextPolicy(
            max_context_tokens=8000,
            reserved_output_tokens=100,
            safety_margin_tokens=100,
            compaction_threshold=0,
        )
    with pytest.raises(ValueError, match="summary_max_ratio"):
        ContextPolicy(
            max_context_tokens=8000,
            reserved_output_tokens=100,
            safety_margin_tokens=100,
            summary_max_ratio=1.0,
        )
    with pytest.raises(ValueError, match="skills_max_ratio"):
        ContextPolicy(
            max_context_tokens=8000,
            reserved_output_tokens=100,
            safety_margin_tokens=100,
            skills_max_ratio=-0.1,
        )
