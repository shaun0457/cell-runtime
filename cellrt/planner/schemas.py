"""Planner output is untrusted: it becomes a `Decision` only after validation.

Validation checks the shape (a schema) and the meaning against the skill registry: a planner may
switch only to one of the step's options (in Phase 0, the other insert skills for an insert
step), never to an unknown skill or to the failed skill itself (that is a retry). Anything else
is a `Rejection`, recorded as an event; it never raises.
"""

from __future__ import annotations

import json
import reprlib
from collections.abc import Collection
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from cellrt.runtime.scrub import UNSAFE_CHARS, scrub
from cellrt.skills.registry import SkillRegistry

# Raw output is recorded for the audit trail, scrubbed and cut to this length.
MAX_RAW_CHARS = 500
# Text longer than this is rejected before it is parsed.
MAX_INPUT_CHARS = 16_000
# JSON nested deeper than this is rejected before it is parsed (a decision is one level deep).
# Parsing deep input would hit a recursion limit that differs between platforms.
MAX_JSON_DEPTH = 8
# Names an error location may show; anything else (a key the planner made up) is "<extra>".
# With duplicates dropped, this keeps the set of possible error codes small and fixed.
_KNOWN_LOCS = frozenset({"kind", "skill", "reason", "switch_skill", "retry", "abort", "escalate"})
# A printable reason: it ends up in the run's outcome and in replay lines. No C0 or C1 control
# characters (a C1 byte can be a terminal escape) and no direction overrides.
_NO_CONTROL_CHARS = f"^[^{UNSAFE_CHARS}]+$"
MAX_REASON_CHARS = 200


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
    reason: str = Field(min_length=1, max_length=MAX_REASON_CHARS, pattern=_NO_CONTROL_CHARS)


# ADR 018. The docstring below is what the model reads in the tool's schema.
class Escalate(_Decision):
    """Hand the task to a person: nothing else runs, and the run ends aborted."""

    kind: Literal["escalate"] = "escalate"
    reason: str = Field(min_length=1, max_length=MAX_REASON_CHARS, pattern=_NO_CONTROL_CHARS)


Decision = Annotated[SwitchSkill | Retry | Abort | Escalate, Field(discriminator="kind")]
DECISION_ADAPTER: TypeAdapter[Decision] = TypeAdapter(Decision)
_DECISION_TYPES: tuple[type[_Decision], ...] = (SwitchSkill, Retry, Abort, Escalate)


def decision_json_schema() -> dict[str, object]:
    """The decision union as one JSON schema object, for a model's tool or prompt.

    It tells the model the shape; it does not replace validation. Each variant is inlined (no
    `$ref`, which some providers reject), `kind` is required, and the reason's character
    pattern is left out: it is written for Python's regex engine, and `validate_decision`
    enforces it anyway.
    """
    variants: list[dict[str, object]] = []
    for model in _DECISION_TYPES:
        schema = model.model_json_schema()
        properties: dict[str, object] = {
            name: {k: v for k, v in prop.items() if k not in ("title", "default", "pattern")}
            for name, prop in schema["properties"].items()
        }
        properties["kind"] = {"type": "string", "const": model.model_fields["kind"].default}
        required = [r for r in schema.get("required", []) if r != "kind"]
        variants.append(
            {
                "type": "object",
                "description": (model.__doc__ or "").strip(),
                "properties": properties,
                "required": ["kind", *required],
                "additionalProperties": False,
            }
        )
    return {"type": "object", "oneOf": variants}


@dataclass(frozen=True)
class Rejection:
    raw: str  # the output as received, scrubbed and cut to MAX_RAW_CHARS
    errors: tuple[str, ...]  # "field: error type" codes, never the offending values


def validate_decision(
    raw: object, registry: SkillRegistry, failed_skill: str, options: Collection[str]
) -> Decision | Rejection:
    shown = _shown(raw)
    data = raw
    if isinstance(raw, str):
        if len(raw) > MAX_INPUT_CHARS:
            return Rejection(shown, ("<root>: too_long",))
        if _json_depth(raw) > MAX_JSON_DEPTH:
            return Rejection(shown, ("<root>: too_deep",))
        try:
            data = json.loads(raw)
        except ValueError:
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
    if isinstance(decision, Abort | Escalate):
        # The reason is stored and shown: scrub it like any text from outside.
        reason = scrub(decision.reason)[:MAX_REASON_CHARS]
        return decision.model_copy(update={"reason": reason})
    return decision


def audit_text(raw: object) -> str:
    """Planner output as text for the audit trail: scrubbed, bounded, never raises."""
    return _shown(raw)


def _json_depth(text: str) -> int:
    """The deepest bracket nesting in `text`, brackets inside JSON strings not counted."""
    depth = deepest = 0
    in_string = escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
            deepest = max(deepest, depth)
        elif ch in "]}":
            depth -= 1
    return deepest


class _BoundedRepr(reprlib.Repr):
    """Prints a bounded part of a structure: never the whole of a huge or deeply nested output.

    Plain data (what JSON can hold) is printed; any other object by its type name only. Its own
    repr is never called: it may be huge, raise, or show a memory address, which would make the
    log differ between two runs of the same seed.
    """

    def __init__(self) -> None:
        super().__init__()  # keyword arguments only from Python 3.12; we support 3.11
        self.maxlevel = 4
        self.maxtuple = self.maxlist = self.maxdict = 20
        self.maxset = self.maxfrozenset = self.maxdeque = self.maxarray = 20
        self.maxstring = MAX_RAW_CHARS
        self.maxlong = 40

    def repr_instance(self, x: object, level: int) -> str:
        if x is None or isinstance(x, bool | float):
            return repr(x)
        return f"<{type(x).__qualname__}>"


_REPR = _BoundedRepr()


def _shown(raw: object) -> str:
    """The raw output for the audit trail, scrubbed and cut short; never raises."""
    if isinstance(raw, str):
        return scrub(raw)[:MAX_RAW_CHARS]
    try:
        return scrub(_REPR.repr(raw))[:MAX_RAW_CHARS]
    except Exception:  # a hostile __eq__ or __hash__ while sorting a set or dict
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
