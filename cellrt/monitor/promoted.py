"""Promoted rules: the language a candidate Monitor rule is written in (ADR 021).

A promoted rule is a conjunction of one to four threshold conditions over what the Monitor
already reads: the force and the torque magnitude and the XY error of the current sample, and
the height the plug went down over the rule's window. It fires once its window is full and every
condition holds, only in the insertion profile and only for the skill it names. Its
verdict is UNKNOWN with the rule `promoted:<NAME>`; the Monitor checks promoted rules after the
known-class rules and before `insert_stalled`.

A candidate comes from a model (principle 4), so every field is bounded and parsing is strict:
no coercion, no extra fields, no NaN, a size limit before the parser sees the text. A refusal
says where and why, never the value.
"""

from __future__ import annotations

import hashlib
import json
import math
import operator
from collections.abc import Callable, Iterable, Mapping
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError

SignalField = Literal["force_n", "torque_nm", "xy_error_mm", "progress_mm"]
Op = Literal["<", "<=", ">", ">="]

# The range a threshold may take, per field: wide enough for any real reading, narrow enough
# that a model cannot write a rule that never or always holds by accident of scale.
FIELD_BOUNDS: Mapping[str, tuple[float, float]] = MappingProxyType(
    {
        "force_n": (0.0, 100.0),
        "torque_nm": (0.0, 10.0),  # a magnitude
        "xy_error_mm": (0.0, 50.0),
        "progress_mm": (-50.0, 50.0),  # down is positive; negative means the plug went up
    }
)
MAX_CONDITIONS = 4
MIN_WINDOW_S = 0.2
MAX_WINDOW_S = 2.0
MAX_CANDIDATE_CHARS = 10_000
MAX_SHOWN_ERRORS = 5
NAME_PATTERN = r"^[A-Z][A-Z0-9_]{2,39}$"
SKILL_PATTERN = r"^[a-z][a-z0-9_]{0,39}$"
PROMOTED_PREFIX = "promoted:"

_OPS: Mapping[str, Callable[[float, float], bool]] = MappingProxyType(
    {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}
)
_STRICT = ConfigDict(frozen=True, extra="forbid", strict=True)


class CandidateRefused(ValueError):
    """A candidate outside the rule language. The message names locations and error types."""


class Condition(BaseModel):
    model_config = _STRICT

    field: SignalField
    op: Op
    value: float = Field(allow_inf_nan=False)

    @model_validator(mode="after")
    def _in_bounds(self) -> Condition:
        low, high = FIELD_BOUNDS[self.field]
        if not low <= self.value <= high:
            raise PydanticCustomError("out_of_bounds", "the threshold is outside the field's range")
        return self

    def holds(self, reading: float) -> bool:
        return _OPS[self.op](reading, self.value)


class PromotedRule(BaseModel):
    model_config = _STRICT

    name: str = Field(pattern=NAME_PATTERN)
    # The one skill the rule watches: the skill its group came from (ADR 021, 5A).
    skill: str = Field(pattern=SKILL_PATTERN)
    window_s: float = Field(ge=MIN_WINDOW_S, le=MAX_WINDOW_S, allow_inf_nan=False)
    conditions: tuple[Condition, ...] = Field(min_length=1, max_length=MAX_CONDITIONS)

    @property
    def verdict_rule(self) -> str:
        return f"{PROMOTED_PREFIX}{self.name}"

    def holds(self, readings: Mapping[str, float]) -> bool:
        """Every condition holds for these readings (one per field)."""
        return all(c.holds(readings[c.field]) for c in self.conditions)


class PromotedPlan(BaseModel):
    """What recovery does when the rule fires: switch to another insert skill, retry the same
    skill, stop, or hand over to a human."""

    model_config = _STRICT

    action: Literal["switch_skill", "retry", "abort", "escalate"]
    skill: str | None = Field(default=None, pattern=SKILL_PATTERN)

    @model_validator(mode="after")
    def _skill_only_for_a_switch(self) -> PromotedPlan:
        if (self.action == "switch_skill") != (self.skill is not None):
            raise PydanticCustomError(
                "skill_for_switch_only", "a switch names its skill; other actions name none"
            )
        return self


class Candidate(BaseModel):
    """A proposed rule and the plan for the failures it describes."""

    model_config = _STRICT

    rule: PromotedRule
    plan: PromotedPlan

    @classmethod
    def parse(cls, raw: str) -> Candidate:
        if len(raw) > MAX_CANDIDATE_CHARS:
            raise CandidateRefused(
                f"candidate refused: too long (more than {MAX_CANDIDATE_CHARS} characters)"
            )
        try:
            return cls.model_validate_json(raw)
        except ValidationError as exc:
            raise CandidateRefused(f"candidate refused: {_where(exc)}") from None
        except (ValueError, RecursionError) as exc:
            raise CandidateRefused(f"candidate refused: {type(exc).__name__}") from None

    def canonical(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        """Identifies the candidate across files and whitespace: approval names this hash."""
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()[:16]


def rules_overlap(a: PromotedRule, b: PromotedRule) -> bool:
    """Whether one sample can meet both rules: they watch the same skill and, field by field,
    some reading meets every condition of both. The Monitor would then pick between them by
    name, which says nothing about which plan fits (ADR 021). `progress_mm` is the descent over
    a rule's own window, so it tells two rules apart only when their windows are the same.

    Readings are taken as unbounded, so a pair is called overlapping also where a reading that
    meets both cannot occur (a force below 0 N): a false refusal, never a missed overlap. The
    windows are compared in seconds, while the Monitor counts a window in samples: two windows
    that round to the same number of samples (0.46 s and 0.5 s at 50 ms) are taken as different,
    so `progress_mm` is left out and the pair may be refused. Also a false refusal, never a
    missed overlap (#66 review F3)."""
    if a.skill != b.skill:
        return False
    fields = set(FIELD_BOUNDS) - (set() if a.window_s == b.window_s else {"progress_mm"})
    both = (*a.conditions, *b.conditions)
    return all(_satisfiable(c for c in both if c.field == f) for f in fields)


def _satisfiable(conditions: Iterable[Condition]) -> bool:
    """Whether some reading meets every one of these conditions on one field."""
    low, low_closed = -math.inf, False
    high, high_closed = math.inf, False
    for c in conditions:
        closed = c.op in ("<=", ">=")
        if c.op in (">", ">="):
            if c.value > low or (c.value == low and not closed):
                low, low_closed = c.value, closed
        elif c.value < high or (c.value == high and not closed):
            high, high_closed = c.value, closed
    return low < high or (low == high and low_closed and high_closed)


def _where(exc: ValidationError) -> str:
    """Each error's location and type. A location names only the language's own fields and list
    indexes: an unknown key is the candidate's text, so it shows as `<key>`."""
    where = [
        f"{'.'.join(_part(p) for p in e['loc']) or '<root>'}: {e['type']}"
        for e in exc.errors(include_url=False, include_input=False, include_context=False)
    ]
    more = len(where) - MAX_SHOWN_ERRORS
    return "; ".join(where[:MAX_SHOWN_ERRORS] + ([f"... and {more} more"] if more > 0 else []))


_FIELD_NAMES = frozenset().union(
    *(m.model_fields for m in (Candidate, PromotedRule, PromotedPlan, Condition))
)


def _part(p: str | int) -> str:
    return str(p) if isinstance(p, int) or p in _FIELD_NAMES else "<key>"
