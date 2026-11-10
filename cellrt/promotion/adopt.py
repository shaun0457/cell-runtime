"""Approve a validated candidate, and load approved rules into an eval (ADR 021, ticket T3).

`approve` reads a report `cellrt promote validate --out` wrote. It refuses a report unless all
four gates passed with no failing window, the report was made under the Monitor configuration
in use now, and the candidate in it still has the hash the report names. The approved file
`eval/promoted/<NAME>.json` holds the rule, its plan, both hashes, that configuration and who
approved it. An eval loads only these files: a promoted plan runs in settings B and C without
the planner, and counts against the UNKNOWN class budget and the task budget.

Trust: the hashes catch a candidate edited after its report, or a configuration changed since,
by accident. They are not signatures: anyone who can write the report or the approval directory
can forge both. Write access to the approval directory is the trust root, as it is for the code.

A refusal is a fixed code, never text from the report: the candidate in it came from a model.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cellrt.executor.recovery import RecoveryConfig
from cellrt.monitor.classifier import FailureClass, MonitorConfig
from cellrt.monitor.promoted import (
    Candidate,
    CandidateRefused,
    PromotedPlan,
    PromotedRule,
    rules_overlap,
)
from cellrt.promotion.replay import ReplayConfig
from cellrt.promotion.validate import REPORT_VERSION, check_registry
from cellrt.skills.registry import SkillRegistry

APPROVER = re.compile(r"[A-Za-z][A-Za-z0-9 ._-]{0,63}")
MAX_REPORT_CHARS = 3_000_000  # a report lists every run it replayed (20,000 at most)
MAX_ADOPTED_CHARS = 20_000
MAX_GROUP_CHARS = 200
MAX_PROMOTED_RULES = 16
MAX_CONFIG_PARTS = 16
# The UNKNOWN class budget a promoted plan gets when the recovery config sets none (by default
# UNKNOWN has no budget: only the planner handled it). The same as the planner's per task.
PROMOTED_UNKNOWN_BUDGET = 2
_HASH = r"^[0-9a-f]{16}$"
_GATES = (0, 1, 2, 3)
# Device names on Windows, with or without an extension: `NUL.json` is not a file there. A rule
# name matches the language's pattern and may still be one of them.
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"{d}{n}" for d in ("COM", "LPT") for n in range(1, 10))}
)


class ApprovalRefused(ValueError):
    """A report, an approver or an approved file that cannot be used. The message is a code."""

    def __init__(self, code: str) -> None:
        super().__init__(f"approval refused: {code}")
        self.code = code


class Adopted(BaseModel):
    """An approved rule, as written to `eval/promoted/<NAME>.json`."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    rule: PromotedRule
    plan: PromotedPlan
    candidate_hash: str = Field(pattern=_HASH)
    report_hash: str = Field(pattern=_HASH)
    group: str = Field(min_length=1, max_length=MAX_GROUP_CHARS)
    # The replay configuration (Monitor inputs) the gates were checked under.
    config_parts: dict[str, str] = Field(max_length=MAX_CONFIG_PARTS)
    approved_by: str = Field(pattern=f"^{APPROVER.pattern}$")

    def candidate(self) -> Candidate:
        return Candidate(rule=self.rule, plan=self.plan)


@dataclass(frozen=True)
class Promoted:
    """The approved rules an eval runs with, in name order, and their plans by rule name."""

    rules: tuple[PromotedRule, ...] = ()
    plans: Mapping[str, PromotedPlan] = dataclasses.field(
        default_factory=lambda: MappingProxyType({})
    )


def approve(
    report: str,
    by: str,
    registry: SkillRegistry,
    parts: Mapping[str, str] | None = None,
    *,
    approved: Iterable[PromotedRule] = (),
) -> Adopted:
    """The approved rule for a report whose gates all passed, approved by `by`. `parts` is the
    replay configuration in use now (the mock's by default). A candidate that can fire on the
    same sample as one of the `approved` rules is refused; one with the same name is left to
    `write_adopted`, which never replaces it."""
    expected = dict(ReplayConfig.mock().parts if parts is None else parts)
    if not APPROVER.fullmatch(by):
        raise ApprovalRefused("approver_invalid")
    if len(report) > MAX_REPORT_CHARS:
        raise ApprovalRefused("report_too_long")
    try:
        data = json.loads(report)
    except (ValueError, RecursionError):
        raise ApprovalRefused("report_not_json") from None
    if not isinstance(data, dict):
        raise ApprovalRefused("report_not_an_object")
    if type(data.get("report_version")) is not int or data["report_version"] != REPORT_VERSION:
        raise ApprovalRefused("report_version")
    if data.get("passed") is not True:
        raise ApprovalRefused("not_passed")
    _check_gates(data.get("gates"))
    if data.get("config_parts") != expected:
        raise ApprovalRefused("config_stale")
    candidate = _checked(json.dumps(data.get("candidate")), registry)
    if candidate.digest() != data.get("candidate_hash"):
        raise ApprovalRefused("candidate_hash_mismatch")
    group = data.get("group")
    if not isinstance(group, str) or not 0 < len(group) <= MAX_GROUP_CHARS:
        raise ApprovalRefused("group_invalid")
    rule = candidate.rule
    if any(r.name != rule.name and rules_overlap(r, rule) for r in approved):
        raise ApprovalRefused("rules_overlap")
    return Adopted(
        rule=candidate.rule,
        plan=candidate.plan,
        candidate_hash=candidate.digest(),
        report_hash=hashlib.sha256(report.encode("utf-8")).hexdigest()[:16],
        group=group,
        config_parts=expected,
        approved_by=by,
    )


def write_adopted(adopted: Adopted, directory: Path) -> Path:
    """Write the approved rule as `<directory>/<NAME>.json`; an approved rule is never
    replaced (remove its file to withdraw it, then approve again). A write that fails leaves
    no file behind."""
    if adopted.rule.name in _RESERVED_NAMES:
        raise ApprovalRefused("rule_name_reserved")
    text = adopted.model_dump_json(indent=2) + "\n"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{adopted.rule.name}.json"
    try:
        f = path.open("x", encoding="utf-8", newline="\n")
    except FileExistsError:
        raise ApprovalRefused("already_adopted") from None
    try:
        with f:
            f.write(text)
    except BaseException:
        path.unlink(missing_ok=True)  # half an approval is none: the name stays free
        raise
    return path


def load_promoted(
    directory: Path, registry: SkillRegistry, parts: Mapping[str, str] | None = None
) -> Promoted:
    """Every approved rule in `directory`, checked again: the file is named for its rule, the
    rule and plan still have the validated candidate's hash, the gates were checked under
    `parts` (the mock's replay configuration by default), and the skills are registered."""
    expected = dict(ReplayConfig.mock().parts if parts is None else parts)
    if not directory.is_dir():
        raise ApprovalRefused("no_directory")
    files: list[Path] = []
    try:
        for path in directory.glob("*.json"):
            if path.suffix != ".json":  # Windows matches `X.JSON` too; Linux would not
                continue
            files.append(path)
            if len(files) > MAX_PROMOTED_RULES:
                raise ApprovalRefused("too_many_rules")
    except OSError:
        raise ApprovalRefused("adopted_unreadable") from None
    rules: list[PromotedRule] = []
    plans: dict[str, PromotedPlan] = {}
    for path in files:
        adopted = _read_adopted(path)
        if adopted.rule.name != path.stem:
            raise ApprovalRefused("file_name")
        if adopted.config_parts != expected:
            raise ApprovalRefused("config_stale")
        candidate = adopted.candidate()
        if candidate.digest() != adopted.candidate_hash:
            raise ApprovalRefused("candidate_hash_mismatch")
        _checked(candidate.canonical(), registry)
        rules.append(adopted.rule)
        plans[adopted.rule.name] = adopted.plan
    # Approval checks a candidate against the rules approved before it; a file copied in, or one
    # approved before that check existed, meets the check only here.
    if any(rules_overlap(a, b) for i, a in enumerate(rules) for b in rules[i + 1 :]):
        raise ApprovalRefused("rules_overlap")
    # By name as a string, the same on every platform: the order is part of the Monitor's hash.
    rules.sort(key=lambda r: r.name)
    return Promoted(tuple(rules), MappingProxyType(plans))


def with_promoted(
    promoted: Promoted, monitor: MonitorConfig, recovery: RecoveryConfig
) -> tuple[MonitorConfig, RecoveryConfig]:
    """The Monitor with the approved rules after its own, and recovery with their plans. With
    no rules both come back unchanged, so their hashes do too."""
    if not promoted.rules:
        return monitor, recovery
    monitor = dataclasses.replace(monitor, promoted=(*monitor.promoted, *promoted.rules))
    budgets = dict(recovery.budgets)
    budgets.setdefault(FailureClass.UNKNOWN.value, PROMOTED_UNKNOWN_BUDGET)
    recovery = dataclasses.replace(
        recovery, promoted={**recovery.promoted, **promoted.plans}, budgets=budgets
    )
    return monitor, recovery


def _check_gates(gates: object) -> None:
    """Gates 0-3, each once, each passed with no failing window."""
    if not isinstance(gates, list) or not all(isinstance(g, dict) for g in gates):
        raise ApprovalRefused("gates_invalid")
    # Exact types: `true` or `1.0` must not stand in for gate 1, nor 1 for a pass.
    if any(type(g.get("gate")) is not int or type(g.get("passed")) is not bool for g in gates):
        raise ApprovalRefused("gates_invalid")
    if sorted(g["gate"] for g in gates) != list(_GATES):
        raise ApprovalRefused("gate_missing")
    if any(not g["passed"] or g.get("failed") != 0 or g.get("cases") != [] for g in gates):
        raise ApprovalRefused("gate_failed")


def _read_adopted(path: Path) -> Adopted:
    try:
        with path.open(encoding="utf-8") as f:
            raw = f.read(MAX_ADOPTED_CHARS + 1)
    except (OSError, UnicodeDecodeError):
        raise ApprovalRefused("adopted_unreadable") from None
    if len(raw) > MAX_ADOPTED_CHARS:
        raise ApprovalRefused("adopted_too_long")
    try:
        return Adopted.model_validate_json(raw)
    except (ValidationError, ValueError, RecursionError):
        raise ApprovalRefused("adopted_invalid") from None


def _checked(raw: str, registry: SkillRegistry) -> Candidate:
    try:
        candidate = Candidate.parse(raw)
        check_registry(candidate, registry)
    except CandidateRefused:
        raise ApprovalRefused("candidate_refused") from None
    return candidate
