"""The planner protocol: what setting C asks when the Monitor cannot classify a failure.

The planner is consulted only between skills, never inside the control loop: the failed skill
has been stopped and the arm holds (ADR 006). Its answer is raw and untrusted; the executor
validates it (`planner.schemas.validate_decision`) before anything runs.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

# How much of a model call's prompt and response the event log keeps (`planner_responded`).
MAX_STORED_PROMPT_CHARS = 16_000
MAX_STORED_RESPONSE_CHARS = 4_000


@dataclass(frozen=True)
class FailureContext:
    """Everything the planner is told about one failure."""

    skill: str  # the skill that failed
    attempt: int  # which run of that skill failed
    failure_class: str
    rule: str  # the Monitor rule that fired
    evidence: Mapping[str, Any]
    time_left_s: float  # until the task's time limit
    planner_calls_left: int  # including this one
    recoveries_left: int  # in the task's recovery budget
    catalogue: Mapping[str, str]  # registered skills and the Monitor profile each declares
    options: tuple[str, ...] = ()  # the skills a switch may name for this step, sorted
    descriptions: Mapping[str, str] = field(default_factory=dict)  # what each skill does

    def as_dict(self) -> dict[str, Any]:
        return {
            "skill": self.skill,
            "attempt": self.attempt,
            "failure_class": self.failure_class,
            "rule": self.rule,
            "evidence": dict(self.evidence),
            "time_left_s": round(self.time_left_s, 6),
            "planner_calls_left": self.planner_calls_left,
            "recoveries_left": self.recoveries_left,
            "catalogue": dict(self.catalogue),
            "options": list(self.options),
            "descriptions": dict(self.descriptions),
        }


@dataclass(frozen=True)
class PlannerTrace:
    """One model call, as the audit trail records it (`planner_responded`)."""

    provider: str
    model: str
    prompt: str  # the messages sent, as JSON
    response: str  # what came back, before validation
    prompt_tokens: int | None
    completion_tokens: int | None
    finish_reason: str | None
    latency_s: float  # wall time

    def __post_init__(self) -> None:
        """Types are checked, not only ranges: a client is outside code, and a wrong type here
        would otherwise fail later, while the event is recorded, and crash the run."""
        for text in (self.provider, self.model, self.prompt, self.response):
            if not isinstance(text, str):
                raise TypeError("trace text fields must be strings")
        if self.finish_reason is not None and not isinstance(self.finish_reason, str):
            raise TypeError("finish_reason must be a string or None")
        for tokens in (self.prompt_tokens, self.completion_tokens):
            if tokens is None:
                continue
            if isinstance(tokens, bool) or not isinstance(tokens, int):
                raise TypeError("token counts must be integers or None")
            if tokens < 0:
                raise ValueError("token counts must not be negative")
        if isinstance(self.latency_s, bool) or not isinstance(self.latency_s, int | float):
            raise TypeError("latency_s must be a number")
        if not (math.isfinite(self.latency_s) and self.latency_s >= 0):
            raise ValueError("latency_s must be a finite, non-negative number")


@dataclass(frozen=True)
class Traced:
    """A planner's raw answer together with the trace of the call that produced it."""

    raw: object  # validated like any planner output
    trace: PlannerTrace


class Planner(Protocol):
    """Triage one failure. Returns raw output (a mapping or JSON text), or a `Traced` answer
    that also carries the call's trace; either way it is validated afterwards.

    It runs as a task on the loop, like a skill. In Phase 0 it is given no clock: it must
    answer without awaiting anything (the loop would report it as stuck). Waiting on a model,
    a deadline and late answers come with #47.

    A planner may also have `describe() -> Mapping[str, object]`: its configuration, which then
    counts in the run's config hash (a model planner names its model and prompt there).
    """

    async def triage(self, ctx: FailureContext) -> object: ...
