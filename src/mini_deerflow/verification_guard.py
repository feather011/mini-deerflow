"""验证结论判定（Guard）：把执行证据翻译成主 Agent 可以对外说的结论。

口径（三值，**由代码判定**，不采信模型自述）：

| verdict | label | 含义 |
|---|---|---|
| ``verified`` | 验证通过 | 跑过的验证命令全部退出码 0，且与验收条件对得上 |
| ``unverified`` | 未验证 / 有改动但未验证 | 没有验证命令、退出码无法解析，或验收条件与实际执行的命令无法建立关联 |
| ``failed`` | 验证失败 | 有验证命令退出码非 0 |

为什么需要它
------------
harness 进程正常结束（``status == "completed"``）只说明**执行器跑完了**，
不说明**用户的验收条件已经满足**。此前 ``project_execute`` 把命令和退出码
回灌给模型，但没有任何一处代码判断"这些证据够不够支撑'已完成'"，
结论完全由模型自由文本决定 —— 于是"跑完了但根本没跑测试"和"测试全绿"
都可能写成同一句"已完成"。

本模块只做一件事：**从证据算出结论**。

判定原则（全部朝保守方向倒）：

1. **证据不足往"未验证"降，不往"通过"升。** 没有命令、退出码解析不出来、
   验收条件与命令对不上 —— 一律 ``unverified``。
2. **失败优先。** 只要有一条命令退出码非 0，就是 ``failed``，
   哪怕别的命令都过了。
3. **验收条件做保守关联检查。** 验收条件里提到的命令如果没有对应的
   执行记录（或对应记录没能关联上），就判 ``unverified`` —— 因为
   "跑过 pytest" 不等于 "用户要的那条 pytest 被跑了"。
   关联要求命令**真的执行了**那个工具：``echo pytest`` 提到了 pytest，
   但它没有跑 pytest，不算数。
4. **多条执行取最保守的一个。** 一轮里跑了三次任务，只要有一次失败，
   整体结论就不能是"通过"。

与仓内既有词汇的关系
--------------------
``tool_contracts`` 里已经写着「工具调用成功不等于业务验收通过」，
``lead_agent.models`` 里也有 ``EvidenceReceipt → VerificationResult → VerifiedFact``
这套"证据 → 验证 → 事实"的升格链。本模块是那条链在**运行期**的最小落地：
它只判"能不能升格"，不负责事实本身的建模。
"""

from __future__ import annotations

import json
import re

__all__ = [
    "VERDICT_VERIFIED",
    "VERDICT_UNVERIFIED",
    "VERDICT_FAILED",
    "LABEL_VERIFIED",
    "LABEL_UNVERIFIED",
    "LABEL_UNVERIFIED_WITH_CHANGES",
    "LABEL_FAILED",
    "evaluate_verification",
    "execution_failed_verdict",
    "summarize_verdicts",
    "collect_guard_verdicts",
    "parse_declared_verdict",
    "rewrite_conclusion",
]


VERDICT_VERIFIED = "verified"
VERDICT_UNVERIFIED = "unverified"
VERDICT_FAILED = "failed"

LABEL_VERIFIED = "验证通过"
LABEL_UNVERIFIED = "未验证"
LABEL_UNVERIFIED_WITH_CHANGES = "有改动但未验证"
LABEL_FAILED = "验证失败"

#: 越靠前越保守。多条执行取最保守的一个时用它排序。
_VERDICT_SEVERITY = {
    VERDICT_VERIFIED: 0,
    VERDICT_UNVERIFIED: 1,
    VERDICT_FAILED: 2,
}

_LABEL_BY_VERDICT = {
    VERDICT_VERIFIED: LABEL_VERIFIED,
    VERDICT_UNVERIFIED: LABEL_UNVERIFIED,
    VERDICT_FAILED: LABEL_FAILED,
}

#: 模型自述结论 → verdict（解析模型写的「验证结论：xxx」行时用）。
#: 长的排在前面，避免 "有改动但未验证" 被 "未验证" 抢先匹配。
_LABEL_TO_VERDICT = {
    "有改动但未验证": VERDICT_UNVERIFIED,
    "未验证": VERDICT_UNVERIFIED,
    "验证未通过": VERDICT_FAILED,
    "验证失败": VERDICT_FAILED,
    "验证通过": VERDICT_VERIFIED,
    "验证成功": VERDICT_VERIFIED,
}


# ---------------------------------------------------------------------------
# 验收条件 ↔ 执行命令的关联
# ---------------------------------------------------------------------------

#: 验收条件里可能被点名的验证工具。命中即视为"一条验收要求"。
_KNOWN_VERIFICATION_PATTERNS = (
    r"pytest",
    r"unittest",
    r"\btox\b",
    r"\bnox\b",
    r"jest",
    r"vitest",
    r"mocha",
    r"rspec",
    r"phpunit",
    r"\bctest\b",
    r"cargo\s+test",
    r"\bgo\s+test\b",
    r"mvn\s+test",
    r"gradle\s+test",
    r"dotnet\s+test",
    r"npm\s+test",
    r"npm\s+run",
    r"yarn\s+test",
    r"yarn\s+run",
    r"pnpm\s+test",
    r"pnpm\s+run",
    r"\bmake\b",
    r"\bruff\b",
    r"\bmypy\b",
    r"\bflake8\b",
    r"\beslint\b",
    r"\btsc\b",
    r"\bclippy\b",
    r"black\s+--check",
)

#: 能**真正执行验证**的可执行程序。命令的首 token 必须落在这些名字里，
#: 才算"跑过验证" —— ``echo pytest`` 提到了 pytest，但它没有跑 pytest。
_VERIFICATION_EXECUTABLES = frozenset({
    "pytest", "unittest", "tox", "nox", "jest", "vitest", "mocha", "rspec",
    "phpunit", "ctest", "cargo", "go", "mvn", "gradle", "dotnet", "make",
    "ruff", "mypy", "flake8", "eslint", "tsc", "clippy", "black",
    "npm", "yarn", "pnpm",
})

#: ``python -m <module>`` 形态下算验证的模块。
_VERIFICATION_MODULES = frozenset({
    "pytest", "unittest", "tox", "nox", "ruff", "mypy", "flake8",
})

#: python 解释器名的前缀（覆盖 python3.12 / python3 之类）。
_PYTHON_NAME_PREFIX = "python"

#: 命令切词（保留引号内的整段，Windows 路径带空格时不能被切开）。
_TOKEN_RE = re.compile(r'"([^"]*)"|\'([^\']*)\'|(\S+)')


def _split_tokens(command: str) -> list[str]:
    """按 shell 习惯切词：引号内的整段保持为一个 token。"""
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(str(command or "")):
        token = match.group(1) or match.group(2) or match.group(3) or ""
        if token:
            tokens.append(token)
    return tokens


def _executable_name(token: str) -> str:
    """取可执行名：去掉目录与 .exe 后缀，转小写。"""
    base = token.replace("\\", "/").rsplit("/", 1)[-1]
    base = base.lower()
    if base.endswith(".exe"):
        base = base[:-4]
    return base

#: 验收条件里的英文词（长度 ≥3），用于没有可识别命令名时的词面兜底关联。
_ACCEPTANCE_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_\-]{2,}")


def _acceptance_references(acceptance: str) -> list[str]:
    """从验收条件里抽出"被点名的验证命令"（按出现顺序去重）。"""
    refs: list[str] = []
    seen: set[str] = set()
    for pattern in _KNOWN_VERIFICATION_PATTERNS:
        match = re.search(pattern, acceptance, re.IGNORECASE)
        if not match:
            continue
        ref = re.sub(r"\s+", " ", match.group(0)).strip().lower()
        if ref and ref not in seen:
            seen.add(ref)
            refs.append(ref)
    return refs


def _verification_signature(command: str) -> set[str]:
    """命令的"验证签名"：它**真正执行**了哪些验证工具。

    只有首 token 本身就是验证工具（或 ``python -m <验证模块>``）时才有签名。
    ``echo pytest`` / ``ls -la`` / ``cat pytest.log`` 的签名是**空集** ——
    它们提到了 pytest，但没有跑 pytest，不能用来支撑"验证通过"。

    签名 = {可执行名} ∪ {子命令}（`npm test` → {npm, test}）。
    """
    tokens = _split_tokens(command)
    if not tokens:
        return set()

    base = _executable_name(tokens[0])

    if base.startswith(_PYTHON_NAME_PREFIX) or base == "py":
        # python -m <module>：模块必须是验证模块才算数
        for index, token in enumerate(tokens):
            if token == "-m" and index + 1 < len(tokens):
                module = tokens[index + 1].lower()
                if module in _VERIFICATION_MODULES:
                    return {module}
                return set()
        return set()

    if base not in _VERIFICATION_EXECUTABLES:
        return set()

    signature = {base}
    if len(tokens) > 1:
        sub = tokens[1].lower()
        if sub and not sub.startswith("-"):
            signature.add(sub)
    return signature


def _reference_satisfied(reference: str, commands: list[dict]) -> "str | None":
    """验收条件点名的某条命令，是否被已执行的命令覆盖。

    覆盖 = 已执行命令的**验证签名**包含该要求的全部词。
    返回命中的命令原文，找不到返回 None。
    """
    wanted = {word for word in reference.split() if word}
    if not wanted:
        return None
    for command in commands:
        if wanted <= _verification_signature(command.get("command", "")):
            return command.get("command") or ""
    return None


def _check_acceptance_coverage(
    acceptance: str,
    commands: list[dict],
) -> dict:
    """保守关联检查：验收条件是否真的被已执行的命令覆盖。

    返回 ``{"covered": True|False|None, "reason": str, "checked": [str]}``。
    ``covered is None`` 表示**没有验收条件可比对**（不是"通过"）。
    """
    acceptance = (acceptance or "").strip()
    if not acceptance:
        return {
            "covered": None,
            "reason": "未提供验收条件；只能确认已执行的命令全部退出码为 0",
            "checked": [],
        }

    references = _acceptance_references(acceptance)
    if references:
        checked: list[str] = []
        missing: list[str] = []
        for reference in references:
            hit = _reference_satisfied(reference, commands)
            if hit is None:
                missing.append(reference)
            else:
                checked.append(hit)
        if missing:
            return {
                "covered": False,
                "reason": (
                    "验收条件要求的命令没有对应的执行记录："
                    + "、".join(missing)
                ),
                "checked": checked,
            }
        return {
            "covered": True,
            "reason": "验收条件要求的命令都有退出码为 0 的执行记录",
            "checked": checked,
        }

    # 验收条件里没有可识别的命令名 → 退化成词面关联（仍然保守）
    words = {word.lower() for word in _ACCEPTANCE_WORD_RE.findall(acceptance)}
    checked = []
    for command in commands:
        if words & _verification_signature(command.get("command", "")):
            checked.append(command.get("command") or "")
    if checked:
        return {
            "covered": True,
            "reason": "验收条件与已执行的命令存在词面关联",
            "checked": checked,
        }
    return {
        "covered": False,
        "reason": "验收条件里没有可识别的命令，且已执行的命令与验收条件无法建立关联",
        "checked": [],
    }


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------


#: 命令原文进入判定依据时的长度上限（判定依据会回灌给模型与用户）。
_COMMAND_TEXT_MAX = 120


def _short_command(command: str) -> str:
    """命令原文的受控短形式（判定依据会回灌给模型，不放长命令）。"""
    text = " ".join(str(command or "").split())
    if not text:
        return "(未记录命令)"
    if len(text) > _COMMAND_TEXT_MAX:
        text = text[:_COMMAND_TEXT_MAX] + "…"
    return text


def evaluate_verification(
    verification: "list[dict] | None",
    acceptance: str = "",
    *,
    has_changes: bool = False,
) -> dict:
    """从执行证据算出权威结论。

    :param verification: ``project_execute`` 返回的验证记录列表，
        每条至少含 ``command`` / ``exit_code``。
    :param acceptance: 本轮交接的验收条件原文（用户确认过的）。
    :param has_changes: 本轮是否有文件改动（只影响"未验证"时的措辞）。
    :returns: ``{"verdict", "label", "reason", "acceptance_covered",
        "failed_commands", "checked_commands"}``
    """
    records = [item for item in (verification or []) if isinstance(item, dict)]

    commands: list[dict] = []
    for item in records:
        exit_code = item.get("exit_code")
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            exit_code = None
        commands.append({
            "command": str(item.get("command") or ""),
            "exit_code": exit_code,
            "passed": None if exit_code is None else (exit_code == 0),
        })

    failed = [cmd for cmd in commands if cmd["passed"] is False]
    unknown = [cmd for cmd in commands if cmd["passed"] is None]
    coverage = _check_acceptance_coverage(acceptance, commands)

    if not commands:
        verdict = VERDICT_UNVERIFIED
        reason = "本轮没有执行任何验证命令，无法确认验收条件已满足"
    elif failed:
        verdict = VERDICT_FAILED
        failed_names = "、".join(
            _short_command(cmd["command"]) for cmd in failed
        )
        reason = f"{len(failed)} 条验证命令退出码非 0：{failed_names}"
    elif unknown:
        verdict = VERDICT_UNVERIFIED
        reason = f"{len(unknown)} 条验证命令的退出码无法解析，不能判定为通过"
    elif coverage["covered"] is False:
        verdict = VERDICT_UNVERIFIED
        reason = coverage["reason"]
    else:
        verdict = VERDICT_VERIFIED
        reason = coverage["reason"]

    if verdict == VERDICT_UNVERIFIED:
        label = LABEL_UNVERIFIED_WITH_CHANGES if has_changes else LABEL_UNVERIFIED
    else:
        label = _LABEL_BY_VERDICT[verdict]

    return {
        "verdict": verdict,
        "label": label,
        "reason": reason,
        "acceptance_covered": coverage["covered"],
        "failed_commands": [cmd["command"] for cmd in failed],
        "checked_commands": coverage["checked"],
    }


def execution_failed_verdict(reason: str) -> dict:
    """执行器根本没跑起来时的判定：没有任何验证证据 → 验证失败。

    用于 ``launch_failed`` / ``protocol_error`` 这两条**拿不到验证记录**的路径 ——
    它们同样不能让模型自由写"已完成"。
    """
    return {
        "verdict": VERDICT_FAILED,
        "label": LABEL_FAILED,
        "reason": reason,
        "acceptance_covered": False,
        "failed_commands": [],
        "checked_commands": [],
    }


def _worse(left: "dict | None", right: "dict | None") -> "dict | None":
    """取两个判定中更保守的一个。"""
    if left is None:
        return right
    if right is None:
        return left
    if _VERDICT_SEVERITY.get(right.get("verdict"), 1) > _VERDICT_SEVERITY.get(
        left.get("verdict"), 1
    ):
        return right
    return left


def summarize_verdicts(verdicts: "list[dict] | None") -> "dict | None":
    """多条执行证据取最保守的一个（failed > unverified > verified）。

    一轮里跑了三次任务，只要有一次失败，整体结论就不能是"通过"。
    """
    result: "dict | None" = None
    for verdict in verdicts or []:
        if not isinstance(verdict, dict):
            continue
        if verdict.get("verdict") not in _VERDICT_SEVERITY:
            continue
        result = _worse(result, verdict)
    return result


def collect_guard_verdicts(tool_result: str) -> list[dict]:
    """从工具结果里抽出验证判定。

    **不只信工具给的 ``guard`` 字段**：只要结果里带 ``verification``，
    就在这里用同一套规则**重新算一遍**；工具给的判定更保守时以更保守的为准。
    这样"判定由代码完成"这件事不依赖某一个工具的输出形状。
    """
    try:
        payload = json.loads(tool_result)
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []

    verdict: "dict | None" = None
    verification = payload.get("verification")
    if isinstance(verification, list):
        acceptance = payload.get("acceptance")
        if not isinstance(acceptance, str):
            acceptance = ""
        changes = payload.get("changes")
        has_changes = False
        if isinstance(changes, dict):
            files = changes.get("files")
            has_changes = isinstance(files, list) and bool(files)
        verdict = evaluate_verification(
            verification, acceptance, has_changes=has_changes
        )

    provided = payload.get("guard")
    if isinstance(provided, dict) and provided.get("verdict") in _VERDICT_SEVERITY:
        verdict = _worse(verdict, provided)

    return [verdict] if verdict is not None else []


# ---------------------------------------------------------------------------
# 结论一致性
# ---------------------------------------------------------------------------

#: 模型自述结论的那一行（允许 markdown 修饰，如 "**验证结论**：xxx"）。
_DECLARATION_RE = re.compile(r"验证结论[^\n]{0,8}?[：:]")

#: 叙述里出现的"完成/通过"断言 → 它隐含的判定。
#: 完成类断言只在**验证失败**时才算冲突（"已完成改动但未验证"是诚实的说法）。
_VERIFICATION_CLAIMS = {
    "验证通过": VERDICT_VERIFIED,
    "验证成功": VERDICT_VERIFIED,
    "全部通过": VERDICT_VERIFIED,
    "测试通过": VERDICT_VERIFIED,
    "验收通过": VERDICT_VERIFIED,
    "验证失败": VERDICT_FAILED,
    "验证未通过": VERDICT_FAILED,
    "测试失败": VERDICT_FAILED,
    "有改动但未验证": VERDICT_UNVERIFIED,
    "未验证": VERDICT_UNVERIFIED,
}

_COMPLETION_CLAIMS = (
    "已完成",
    "已经完成",
    "任务完成",
    "全部完成",
    "完成了",
)


def parse_declared_verdict(text: str) -> "str | None":
    """解析模型自述的结论标签（``验证结论：xxx``），取不到返回 None。"""
    if not text:
        return None
    match = re.search(r"验证结论[^\n]{0,8}?[：:]\s*([^\n]*)", text)
    if not match:
        return None
    declared = match.group(1).strip()
    if not declared:
        return None
    # 先按长的标签匹配，避免 "有改动但未验证" 被 "未验证" 抢先
    for label in sorted(_LABEL_TO_VERDICT, key=len, reverse=True):
        if declared.startswith(label) or label in declared:
            return _LABEL_TO_VERDICT[label]
    return None


def _find_conflicting_claims(text: str, verdict: str) -> list[str]:
    """找出与权威判定冲突的断言短语。"""
    conflicts: list[str] = []
    for phrase, claimed in _VERIFICATION_CLAIMS.items():
        if phrase in text and claimed != verdict:
            conflicts.append(phrase)
    # 完成类断言只在"验证失败"时构成冲突
    if verdict == VERDICT_FAILED:
        for phrase in _COMPLETION_CLAIMS:
            if phrase in text:
                conflicts.append(phrase)
    return conflicts


def rewrite_conclusion(content: str, verdict: dict) -> "tuple[str, bool]":
    """把模型自述的结论纠正成代码判定。

    规则（**代码判定为准**）：

    - 模型声明的结论与权威判定一致、且叙述里没有冲突断言 → 原样返回。
    - 否则：摘掉模型自己写的结论行（那是机器槽位，由系统接管），
      末尾附上**权威判定与依据**；叙述里有冲突断言时，开头加纠正标注。

    返回 ``(新内容, 是否纠正过)``。

    边界：模型**叙述正文**里的措辞不做改写（正则改写正文容易把话说坏），
    系统的保证是"最终回答里的权威判定与证据一致，且冲突被显式标注"。
    """
    authoritative = verdict.get("verdict")
    if authoritative not in _VERDICT_SEVERITY:
        return content, False

    label = verdict.get("label") or _LABEL_BY_VERDICT[authoritative]
    reason = verdict.get("reason") or ""
    declared = parse_declared_verdict(content)
    conflicts = _find_conflicting_claims(content, authoritative)
    wrong_declaration = declared is not None and declared != authoritative

    if declared == authoritative and not conflicts:
        return content, False

    kept = [
        line for line in content.splitlines()
        if not _DECLARATION_RE.search(line)
    ]
    body = "\n".join(kept).strip()

    parts: list[str] = []
    if wrong_declaration or conflicts:
        parts.append("⚠️ 结论纠正：上文结论与执行证据不一致，以执行证据为准。")
    if body:
        parts.append(body)
    block = f"验证结论：{label}（以执行证据为准）"
    if reason:
        block += f"\n判定依据：{reason}"
    parts.append(block)
    return "\n\n".join(parts), True
