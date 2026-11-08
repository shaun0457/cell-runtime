"""Planner output is untrusted: it becomes a `Decision` only after validation.

Validation checks the shape (a schema) and the meaning against the skill registry: a planner may
switch only to one of the step's options (in Phase 0, the other insert skills for an insert
step), never to an unknown skill or to the failed skill itself (that is a retry). Anything else
is a `Rejection`, recorded as an event; it never raises.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from cellrt.skills.registry import SkillRegistry

# Raw output is recorded for the audit trail, cut to this length.
MAX_RAW_CHARS = 500
# Text longer than this is rejected before it is parsed.
MAX_INPUT_CHARS = 16_000
# Names an error location may show; anything else (a key the planner made up) is "<extra>".
# With duplicates dropped, this keeps the set of possible error codes small and fixed.
_KNOWN_LOCS = frozenset({"kind", "skill", "reason", "switch_skill", "retry", "abort"})
# A printable reason: it ends up in the run's outcome and in replay lines.
_NO_CONTROL_CHARS = r"^[^\x00-\x1f\x7f]+$"


class _Decision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SwitchSkill(_Decision):
    """Run another skill in place of the failed one, then carry on with the step."""

    kind: Literal["switch_skill"] = "switch_skill"
    skill: str = Field(min_length=1, max_length=64)


class Retry(_Decision):
    """Run the failed skill again."""

    kind: Literal["retry"] = "retry"


class Abort(_Decision):
    """Give up on the task."""

    kind: Literal["abort"] = "abort"
    reason: str = Field(min_length=1, max_length=200, pattern=_NO_CONTROL_CHARS)


Decision = Annotated[SwitchSkill | Retry | Abort, Field(discriminator="kind")]
DECISION_ADAPTER: TypeAdapter[Decision] = TypeAdapter(Decision)


@dataclass(frozen=True)
class Rejection:
    raw: str  # the output as received, cut to MAX_RAW_CHARS
    errors: tuple[str, ...]  # "field: error type" codes, never the offending values


def validate_decision(
    raw: object, registry: SkillRegistry, failed_skill: str, options: Collection[str]
) -> Decision | Rejection:
    shown = _shown(raw)
    data = raw
    if isinstance(raw, str):
        if len(raw) > MAX_INPUT_CHARS:
            return Rejection(shown, ("<root>: too_long",))
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):  # deep nesting raises RecursionError
            return Rejection(shown, ("<root>: not_json",))
    try:
        decision = DECISION_ADAPTER.validate_python(data)
    except ValidationError as exc:
        return Rejection(shown, _codes(exc))
    if isinstance(decision, SwitchSkill):
        if decision.skill not in registry:
            return Rejection(shown, ("skill: unknown_skill",))
        if decision.skill == failed_skill:
            return Rejection(shown, ("skill: same_as_failed_skill",))
        if decision.skill not in options:
            return Rejection(shown, ("skill: not_an_option_for_this_step",))
    return decision


def _shown(raw: object) -> str:
    """The raw output for the audit trail, cut short; never raises."""
    if isinstance(raw, str):
        return raw[:MAX_RAW_CHARS]
    try:
        return repr(raw)[:MAX_RAW_CHARS]
    except Exception:  # a hostile __repr__, or nesting too deep to print
        return "<unrepresentable>"


def _codes(exc: ValidationError) -> tuple[str, ...]:
    """Error codes ("location: type") in order, without duplicates."""
    codes: list[str] = []
    for err in exc.errors(include_url=False, include_input=False, include_context=False):
        loc = ".".join(str(part) if part in _KNOWN_LOCS else "<extra>" for part in err["loc"])
        code = f"{loc or '<root>'}: {err['type']}"
        if code not in codes:
            codes.append(code)
    return tuple(codes)
