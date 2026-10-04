"""Phase 24 上下文工程领域模型。

三份状态的边界（规格 4.1 事实与投影分离）：

```
ThreadState    完整原始消息，唯一事实源，压缩永不修改它
ContextState   摘要 / Task Notes / 压缩水位 / 索引水位（投影）
ModelContext   单次模型调用的临时输入，用完即弃
```

消息身份：仓库的 `Message` 没有稳定 ID（见 `models.py`），本阶段**不**给历史消息
引入随机 UUID——那会让序列化格式、老数据兼容和所有既有测试一起震荡。改用
**append-only 列表序号** 作为 watermark 与 history reference：

- `summarized_through = 7`  表示 `thread.messages[0..7]` 已进摘要
- `indexed_through = 12`   表示 `thread.messages[0..12]` 已进向量索引

序号在 append-only 的 Thread 上单调稳定；删除线程时整份 ContextState 一并删除，
因此不存在「序号指向已消失消息」的中间态。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum

from mini_deerflow.context_errors import (
    TaskNoteLimitExceededError,
    TaskNoteNotFoundError,
    TaskNoteValidationError,
)

CONTEXT_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Task Note 限额
# ---------------------------------------------------------------------------

TASK_NOTE_MAX_CONTENT_CHARS = 2000
TASK_NOTE_MAX_ACTIVE = 100
TASK_NOTE_MAX_TOTAL = 500

# ---------------------------------------------------------------------------
# ConversationSummary 限额
# ---------------------------------------------------------------------------

SUMMARY_MAX_NARRATIVE_CHARS = 2000
SUMMARY_MAX_ITEMS_PER_FIELD = 20
SUMMARY_MAX_TOTAL_ITEMS = 60
SUMMARY_MAX_TOTAL_CHARS = 4000

# 敏感内容形态。摘要会**长期留存**，一旦把密钥或凭据文件写进去，
# 等于把它们复制进了另一个更少被审查的位置——所以必须在持久化前过滤掉，
# 不能只靠 Summary Prompt「请你不要写」：那是概率约束，不是确定性保证。
_SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # --- API Key / 令牌 ---
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"(?i)\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    # --- password / secret / token 的常见写法 ---
    # 覆盖 `key: value`、`key=value`、`key 是 value` 三种常见表述
    re.compile(
        r"(?i)\b(authorization|api[_-]?key|secret[_-]?key|client[_-]?secret|"
        r"access[_-]?token|refresh[_-]?token|password|passwd|secret|token)\b"
        r"\s*(?:[:=]|是|为|=)\s*[\"']?[^\s\"',;，。]{4,}"
    ),
    # --- POSIX 敏感文件 ---
    re.compile(r"(?i)/etc/(?:shadow|passwd|sudoers|gshadow|ssh/sshd_config)\b"),
    # 允许 0~2 段路径前缀：`/.ssh/`、`/root/.ssh/`、`/home/u/.ssh/` 都要命中
    re.compile(r"(?i)/(?:[\w.-]+/){0,2}\.(?:ssh|aws|gnupg|kube|docker)/"),
    re.compile(r"(?i)~/\.(?:ssh|aws|gnupg|kube|docker)/"),
    re.compile(r"(?i)/(?:var/)?run/secrets/"),
    re.compile(r"(?i)/proc/self/environ\b"),
    # --- Windows 敏感路径 ---
    re.compile(
        r"(?i)[a-z]:\\users\\[^\\\s]+\\"
        r"(?:\.ssh|appdata\\(?:local|roaming)\\microsoft\\credentials)\\?"
    ),
    re.compile(r"(?i)[a-z]:\\windows\\system32\\config\\(?:sam|system|security)\b"),
    # --- 私钥 / 证书 ---
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bid_(?:rsa|dsa|ecdsa|ed25519)\b"),
    re.compile(r"(?i)\b[\w.-]+\.(?:pem|pfx|p12|keystore|jks)\b"),
    # --- 环境变量文件 ---
    re.compile(r"(?i)(?:^|[\s/\\])\.env(?:\.[\w-]+)?(?=[\s\"',;]|$)"),
)

# 被过滤处的占位说明。保留它是为了让模型看见「这里被删过东西」，
# 而不是凭空少半句话、误以为上下文本身残缺。
REDACTED_PLACEHOLDER = "[已移除的敏感信息]"


def contains_sensitive(text: str) -> bool:
    """文本中是否含敏感内容（凭据 / 凭据文件路径 / 私钥）。"""
    return any(pattern.search(text) for pattern in _SENSITIVE_PATTERNS)


def scrub_sensitive(text: str) -> tuple[str, bool]:
    """把敏感片段替换为占位符，返回 (清洗后的文本, 是否发生过替换)。

    这是对 `narrative` 的处理方式：叙述是连贯句子，整份丢掉代价太大，
    而逐处替换既保住了「落库内容干净」，也保住了「摘要还在」。
    数组条目走另一条路——整条剔除（见 `_clean_items`），因为条目本就是
    独立事实，删掉一条不影响其余。
    """
    if not text:
        return "", False
    scrubbed = text
    replaced = False
    for pattern in _SENSITIVE_PATTERNS:
        updated, count = pattern.subn(REDACTED_PLACEHOLDER, scrubbed)
        if count:
            scrubbed = updated
            replaced = True
    return scrubbed, replaced


class TaskNoteCategory(str, Enum):
    """Task Note 类别。"""

    CONSTRAINT = "constraint"
    DECISION = "decision"
    FAILURE = "failure"
    NEXT_STEP = "next_step"


class TaskNoteStatus(str, Enum):
    """Task Note 状态。只有 active / resolved，不允许硬删除。"""

    ACTIVE = "active"
    RESOLVED = "resolved"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse_datetime(raw: object, key: str) -> datetime:
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"TaskNote.{key} 必须是 ISO 时间字符串")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"TaskNote.{key} 时间格式非法: {raw!r}") from exc
    if parsed.tzinfo is None:
        # 旧数据可能不带时区；按 UTC 解释，避免 naive/aware 混算
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass(frozen=True)
class TaskNote:
    """一条不可硬删除的任务笔记。

    `source_run_id` 是审计锚点：本轮 Run 后续失败/超时/取消时，已写入的 Note
    仍然保留，靠这个字段回溯「是谁在哪个 Run 里写下的」。
    """

    note_id: str
    category: TaskNoteCategory
    content: str
    status: TaskNoteStatus
    source_run_id: str | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        # 全部抛 TaskNoteValidationError（同时是 ValueError）：
        # 工具层按 TaskNoteError 精确捕获，既有 except ValueError 也仍然有效。
        if not self.note_id or not self.note_id.strip():
            raise TaskNoteValidationError("TaskNote.note_id 不能为空")
        if not isinstance(self.category, TaskNoteCategory):
            raise TaskNoteValidationError(f"TaskNote.category 非法: {self.category!r}")
        if not isinstance(self.status, TaskNoteStatus):
            raise TaskNoteValidationError(f"TaskNote.status 非法: {self.status!r}")
        if not isinstance(self.content, str):
            raise TaskNoteValidationError("TaskNote.content 必须是字符串")
        if len(self.content) > TASK_NOTE_MAX_CONTENT_CHARS:
            raise TaskNoteValidationError(
                f"TaskNote.content 超长: {len(self.content)} > {TASK_NOTE_MAX_CONTENT_CHARS}"
            )

    # -- 构造 / 变更 -------------------------------------------------------

    @classmethod
    def create(
        cls,
        *,
        note_id: str,
        category: TaskNoteCategory,
        content: str,
        source_run_id: str | None,
        now: datetime | None = None,
    ) -> "TaskNote":
        moment = now or _utcnow()
        cleaned = content.strip()
        if not cleaned:
            raise TaskNoteValidationError("TaskNote.content 不能为空")
        return cls(
            note_id=note_id,
            category=category,
            content=cleaned,
            status=TaskNoteStatus.ACTIVE,
            source_run_id=source_run_id,
            created_at=moment,
            updated_at=moment,
        )

    def with_content(self, content: str, *, now: datetime | None = None) -> "TaskNote":
        """更新正文与时间戳。状态不变，note_id 不变。"""
        cleaned = content.strip()
        if not cleaned:
            raise TaskNoteValidationError("TaskNote.content 不能为空")
        return replace(self, content=cleaned, updated_at=now or _utcnow())

    def with_category(
        self, category: TaskNoteCategory, *, now: datetime | None = None
    ) -> "TaskNote":
        return replace(self, category=category, updated_at=now or _utcnow())

    def resolve(self, *, now: datetime | None = None) -> "TaskNote":
        """标记为已解决。幂等：已 resolved 再 resolve 只刷新时间戳。"""
        return replace(self, status=TaskNoteStatus.RESOLVED, updated_at=now or _utcnow())

    # -- 序列化 -----------------------------------------------------------

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "schema_version": CONTEXT_SCHEMA_VERSION,
            "note_id": self.note_id,
            "category": self.category.value,
            "content": self.content,
            "status": self.status.value,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }
        if self.source_run_id is not None:
            data["source_run_id"] = self.source_run_id
        return data

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "TaskNote":
        if not isinstance(data, dict):
            raise ValueError("TaskNote 必须是字典")
        note_id = data.get("note_id")
        if not isinstance(note_id, str) or not note_id:
            raise ValueError("TaskNote 缺少 note_id")
        try:
            category = TaskNoteCategory(data.get("category"))
        except ValueError as exc:
            raise ValueError(f"TaskNote.category 非法: {data.get('category')!r}") from exc
        try:
            status = TaskNoteStatus(data.get("status"))
        except ValueError as exc:
            raise ValueError(f"TaskNote.status 非法: {data.get('status')!r}") from exc
        content = data.get("content")
        if not isinstance(content, str):
            raise ValueError("TaskNote.content 必须是字符串")
        source_run_id = data.get("source_run_id")
        if source_run_id is not None and not isinstance(source_run_id, str):
            raise ValueError("TaskNote.source_run_id 必须是字符串或 null")
        return cls(
            note_id=note_id,
            category=category,
            content=content,
            status=status,
            source_run_id=source_run_id,
            created_at=_parse_datetime(data.get("created_at"), "created_at"),
            updated_at=_parse_datetime(data.get("updated_at"), "updated_at"),
        )


# ---------------------------------------------------------------------------
# ConversationSummary
# ---------------------------------------------------------------------------


def _clean_items(items: object, *, field_name: str) -> tuple[str, ...]:
    """清理条目：去空白、丢空串、去重、去凭据、限量。

    重复条目在长会话里极常见（每轮压缩都会再抄一遍同样的约束），
    不清理会把弹性额度迅速吃光，把最近消息挤出去。
    """
    if items is None:
        return ()
    if not isinstance(items, (list, tuple)):
        raise ValueError(f"ConversationSummary.{field_name} 必须是数组")
    seen: set[str] = set()
    result: list[str] = []
    for raw in items:
        if not isinstance(raw, str):
            raise ValueError(f"ConversationSummary.{field_name} 元素必须是字符串")
        text = raw.strip()
        if not text or text in seen:
            continue
        if contains_sensitive(text):
            # 整条剔除（不是打码）：条目是独立事实，删掉一条不影响其余，
            # 留半条带密钥的反而更危险。
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= SUMMARY_MAX_ITEMS_PER_FIELD:
            break
    return tuple(result)


def _require_int(value: object, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"ConversationSummary.{key} 必须是整数")
    if value < 0:
        raise ValueError(f"ConversationSummary.{key} 不能为负: {value}")
    return value


@dataclass(frozen=True)
class ConversationSummary:
    """结构化摘要 + 连贯概述。

    两者同时保留：`narrative` 给模型读上下文语气和因果，
    `facts/constraints/decisions/...` 给 Task Notes 与后续重建做结构化合并。
    """

    narrative: str
    facts: tuple[str, ...]
    constraints: tuple[str, ...]
    decisions: tuple[str, ...]
    failed_attempts: tuple[str, ...]
    open_items: tuple[str, ...]
    summarized_from: int
    summarized_through: int
    schema_version: int = CONTEXT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CONTEXT_SCHEMA_VERSION:
            raise ValueError(
                f"未知 ConversationSummary.schema_version: {self.schema_version}"
            )
        if isinstance(self.summarized_from, bool) or not isinstance(self.summarized_from, int):
            raise ValueError("summarized_from 必须是整数消息序号")
        if isinstance(self.summarized_through, bool) or not isinstance(self.summarized_through, int):
            raise ValueError("summarized_through 必须是整数消息序号")
        if self.summarized_through < self.summarized_from:
            raise ValueError(
                f"摘要范围倒挂: from={self.summarized_from} through={self.summarized_through}"
            )

    # -- 构造（含全部清理与限额） -----------------------------------------

    @classmethod
    def build(
        cls,
        *,
        narrative: str,
        facts: object = (),
        constraints: object = (),
        decisions: object = (),
        failed_attempts: object = (),
        open_items: object = (),
        summarized_from: int,
        summarized_through: int,
    ) -> "ConversationSummary":
        if not isinstance(narrative, str):
            raise ValueError("ConversationSummary.narrative 必须是字符串")
        # 持久化前的确定性过滤：敏感片段替换为占位符，而不是整份拒绝。
        # 只靠 Summary Prompt 是概率约束；这里在数据落库的必经之路上兜底。
        # 拒绝会把「模型顺带提了一句 /home/u/.ssh」变成整轮压缩失败，
        # 而要求是「落库内容干净」——过滤同时保住内容与干净。
        cleaned_narrative, _was_scrubbed = scrub_sensitive(narrative.strip())
        if not cleaned_narrative.strip():
            raise ValueError("摘要 narrative 不能为空")

        summary = cls(
            narrative=cleaned_narrative,
            facts=_clean_items(facts, field_name="facts"),
            constraints=_clean_items(constraints, field_name="constraints"),
            decisions=_clean_items(decisions, field_name="decisions"),
            failed_attempts=_clean_items(failed_attempts, field_name="failed_attempts"),
            open_items=_clean_items(open_items, field_name="open_items"),
            summarized_from=_require_int(summarized_from, "summarized_from"),
            summarized_through=_require_int(summarized_through, "summarized_through"),
        )
        summary._enforce_limits()
        return summary

    def _enforce_limits(self) -> None:
        if len(self.narrative) > SUMMARY_MAX_NARRATIVE_CHARS:
            raise ValueError(
                f"摘要 narrative 超长: {len(self.narrative)} > {SUMMARY_MAX_NARRATIVE_CHARS}"
            )
        total_items = sum(
            len(getattr(self, name))
            for name in (
                "facts",
                "constraints",
                "decisions",
                "failed_attempts",
                "open_items",
            )
        )
        if total_items > SUMMARY_MAX_TOTAL_ITEMS:
            raise ValueError(
                f"摘要条目总数超限: {total_items} > {SUMMARY_MAX_TOTAL_ITEMS}"
            )
        total_chars = len(self.narrative) + sum(
            len(item)
            for name in (
                "facts",
                "constraints",
                "decisions",
                "failed_attempts",
                "open_items",
            )
            for item in getattr(self, name)
        )
        if total_chars > SUMMARY_MAX_TOTAL_CHARS:
            raise ValueError(
                f"摘要总字符超限: {total_chars} > {SUMMARY_MAX_TOTAL_CHARS}"
            )

    # -- 增量合并 ---------------------------------------------------------

    def merge(self, newer: "ConversationSummary") -> "ConversationSummary":
        """旧摘要 + 新历史块 → 合并摘要。

        范围取并集；结构化条目去重后相接，仍走同一套清理与限额，
        因此合并不会让摘要无限膨胀。
        """
        return ConversationSummary.build(
            narrative=_join_narratives(self.narrative, newer.narrative),
            facts=self.facts + newer.facts,
            constraints=self.constraints + newer.constraints,
            decisions=self.decisions + newer.decisions,
            failed_attempts=self.failed_attempts + newer.failed_attempts,
            open_items=self.open_items + newer.open_items,
            summarized_from=min(self.summarized_from, newer.summarized_from),
            summarized_through=max(self.summarized_through, newer.summarized_through),
        )

    # -- 序列化 -----------------------------------------------------------

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "narrative": self.narrative,
            "facts": list(self.facts),
            "constraints": list(self.constraints),
            "decisions": list(self.decisions),
            "failed_attempts": list(self.failed_attempts),
            "open_items": list(self.open_items),
            "summarized_from": self.summarized_from,
            "summarized_through": self.summarized_through,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "ConversationSummary":
        if not isinstance(data, dict):
            raise ValueError("ConversationSummary 必须是字典")
        version = data.get("schema_version", CONTEXT_SCHEMA_VERSION)
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValueError("schema_version 必须是整数")
        if version != CONTEXT_SCHEMA_VERSION:
            raise ValueError(f"未知 ConversationSummary.schema_version: {version}")
        return cls.build(
            narrative=data.get("narrative", ""),
            facts=data.get("facts", ()),
            constraints=data.get("constraints", ()),
            decisions=data.get("decisions", ()),
            failed_attempts=data.get("failed_attempts", ()),
            open_items=data.get("open_items", ()),
            summarized_from=data.get("summarized_from", 0),  # type: ignore[arg-type]
            summarized_through=data.get("summarized_through", 0),  # type: ignore[arg-type]
        )


def _join_narratives(old: str, new: str) -> str:
    """拼接两段概述并裁到限额，保证合并后仍在硬限制内。"""
    combined = " ".join(part for part in (old.strip(), new.strip()) if part)
    if len(combined) <= SUMMARY_MAX_NARRATIVE_CHARS:
        return combined
    return combined[: SUMMARY_MAX_NARRATIVE_CHARS].rstrip()


# ---------------------------------------------------------------------------
# ContextState
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextState:
    """上下文投影状态：摘要、Task Notes、两道水位、压缩计数。

    `revision` 是乐观并发控制的版本号，由 ContextStore 负责递增；
    领域层只读它、带上它去写，不自己改。
    """

    summary: ConversationSummary | None = None
    task_notes: tuple[TaskNote, ...] = ()
    revision: int = 0
    summarized_through: int | None = None
    indexed_through: int | None = None
    compaction_count: int = 0
    schema_version: int = CONTEXT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CONTEXT_SCHEMA_VERSION:
            raise ValueError(f"未知 ContextState.schema_version: {self.schema_version}")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise ValueError("revision 必须是整数")
        if self.revision < 0:
            raise ValueError(f"revision 不能为负: {self.revision}")
        for key in ("summarized_through", "indexed_through"):
            value = getattr(self, key)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{key} 必须是非负整数或 None")
        if isinstance(self.compaction_count, bool) or not isinstance(self.compaction_count, int):
            raise ValueError("compaction_count 必须是整数")
        if self.compaction_count < 0:
            raise ValueError(f"compaction_count 不能为负: {self.compaction_count}")
        if not isinstance(self.task_notes, tuple):
            raise ValueError("task_notes 必须是 tuple")
        for note in self.task_notes:
            if not isinstance(note, TaskNote):
                raise ValueError(f"task_notes 元素必须是 TaskNote，收到 {type(note).__name__}")

    # -- Note 查询 --------------------------------------------------------

    @property
    def active_notes(self) -> tuple[TaskNote, ...]:
        return tuple(n for n in self.task_notes if n.status is TaskNoteStatus.ACTIVE)

    @property
    def resolved_notes(self) -> tuple[TaskNote, ...]:
        return tuple(n for n in self.task_notes if n.status is TaskNoteStatus.RESOLVED)

    def find_note(self, note_id: str) -> TaskNote | None:
        for note in self.task_notes:
            if note.note_id == note_id:
                return note
        return None

    def _with_notes(self, notes: tuple[TaskNote, ...]) -> "ContextState":
        return replace(self, task_notes=notes)

    def add_note(self, note: TaskNote) -> "ContextState":
        """追加一条 Note；命中容量上限时抛错，绝不静默逐出旧 Note。"""
        if self.find_note(note.note_id) is not None:
            raise TaskNoteValidationError(f"TaskNote 已存在: {note.note_id}")
        active_count = len(self.active_notes)
        if note.status is TaskNoteStatus.ACTIVE and active_count >= TASK_NOTE_MAX_ACTIVE:
            raise TaskNoteLimitExceededError(
                "active_notes", TASK_NOTE_MAX_ACTIVE, active_count + 1
            )
        if len(self.task_notes) >= TASK_NOTE_MAX_TOTAL:
            raise TaskNoteLimitExceededError(
                "total_notes", TASK_NOTE_MAX_TOTAL, len(self.task_notes) + 1
            )
        return self._with_notes(self.task_notes + (note,))

    def update_note(self, note: TaskNote) -> "ContextState":
        """按 note_id 替换；不存在时抛 TaskNoteNotFoundError。"""
        if self.find_note(note.note_id) is None:
            raise TaskNoteNotFoundError(note.note_id)
        return self._with_notes(
            tuple(note if existing.note_id == note.note_id else existing
                  for existing in self.task_notes)
        )

    # -- 序列化 -----------------------------------------------------------

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "summary": self.summary.to_dict() if self.summary else None,
            "task_notes": [note.to_dict() for note in self.task_notes],
            "summarized_through": self.summarized_through,
            "indexed_through": self.indexed_through,
            "compaction_count": self.compaction_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "ContextState":
        if not isinstance(data, dict):
            raise ValueError("ContextState 必须是字典")
        version = data.get("schema_version", CONTEXT_SCHEMA_VERSION)
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValueError("schema_version 必须是整数")
        if version != CONTEXT_SCHEMA_VERSION:
            raise ValueError(f"未知 ContextState.schema_version: {version}")

        raw_summary = data.get("summary")
        summary = None
        if raw_summary is not None:
            if not isinstance(raw_summary, dict):
                raise ValueError("summary 必须是字典或 null")
            summary = ConversationSummary.from_dict(raw_summary)

        raw_notes = data.get("task_notes", ())
        if not isinstance(raw_notes, (list, tuple)):
            raise ValueError("task_notes 必须是数组")
        notes = tuple(TaskNote.from_dict(item) for item in raw_notes)  # type: ignore[arg-type]

        return cls(
            summary=summary,
            task_notes=notes,
            revision=data.get("revision", 0),  # type: ignore[arg-type]
            summarized_through=data.get("summarized_through"),  # type: ignore[arg-type]
            indexed_through=data.get("indexed_through"),  # type: ignore[arg-type]
            compaction_count=data.get("compaction_count", 0),  # type: ignore[arg-type]
            schema_version=version,
        )


def empty_context_state() -> ContextState:
    """新线程的初始 ContextState。"""
    return ContextState()


# ---------------------------------------------------------------------------
# ContextPolicy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextPolicy:
    """Token 预算策略（规格 4.6）。

    公式：
        hard_input_limit = max_context_tokens - reserved_output_tokens - safety_margin_tokens
        soft_compaction_limit = int(hard_input_limit * compaction_threshold)

    比例字段是各**弹性区块**的上限；没用完的额度自动让给最近原始消息，
    不做固定预留（固定预留等于把 Token 白扔）。
    """

    max_context_tokens: int
    reserved_output_tokens: int
    safety_margin_tokens: int
    compaction_threshold: float = 0.8

    summary_max_ratio: float = 0.15
    task_notes_max_ratio: float = 0.10
    memory_max_ratio: float = 0.10
    skills_max_ratio: float = 0.15

    def __post_init__(self) -> None:
        if self.max_context_tokens <= 0:
            raise ValueError("max_context_tokens 必须为正")
        if self.reserved_output_tokens < 0 or self.safety_margin_tokens < 0:
            raise ValueError("reserved_output_tokens / safety_margin_tokens 不能为负")
        if not 0 < self.compaction_threshold <= 1:
            raise ValueError("compaction_threshold 必须在 (0, 1] 内")
        for name in (
            "summary_max_ratio",
            "task_notes_max_ratio",
            "memory_max_ratio",
            "skills_max_ratio",
        ):
            ratio = getattr(self, name)
            if not 0 <= ratio < 1:
                raise ValueError(f"{name} 必须在 [0, 1) 内")
        if self.hard_input_limit <= 0:
            raise ValueError(
                f"硬输入上限非正: {self.hard_input_limit}，"
                "reserved_output_tokens + safety_margin_tokens 过大"
            )

    @property
    def hard_input_limit(self) -> int:
        """单次模型调用允许的输入 Token 硬上限。"""
        return (
            self.max_context_tokens
            - self.reserved_output_tokens
            - self.safety_margin_tokens
        )

    @property
    def soft_compaction_limit(self) -> int:
        """触发压缩的软阈值。"""
        return int(self.hard_input_limit * self.compaction_threshold)

    def cap(self, section: str) -> int:
        """取某弹性区块的 Token 上限。"""
        ratios = {
            "summary": self.summary_max_ratio,
            "task_notes": self.task_notes_max_ratio,
            "memory": self.memory_max_ratio,
            "skills": self.skills_max_ratio,
        }
        try:
            ratio = ratios[section]
        except KeyError as exc:
            raise ValueError(f"未知弹性区块: {section}") from exc
        return int(self.hard_input_limit * ratio)


__all__ = [
    "CONTEXT_SCHEMA_VERSION",
    "REDACTED_PLACEHOLDER",
    "SUMMARY_MAX_ITEMS_PER_FIELD",
    "SUMMARY_MAX_NARRATIVE_CHARS",
    "SUMMARY_MAX_TOTAL_CHARS",
    "SUMMARY_MAX_TOTAL_ITEMS",
    "TASK_NOTE_MAX_ACTIVE",
    "TASK_NOTE_MAX_CONTENT_CHARS",
    "TASK_NOTE_MAX_TOTAL",
    "ConversationSummary",
    "ContextPolicy",
    "ContextState",
    "TaskNote",
    "TaskNoteCategory",
    "TaskNoteStatus",
    "contains_sensitive",
    "empty_context_state",
    "scrub_sensitive",
]
