"""The planner protocol: what setting C asks when the Monitor cannot classify a failure.

The planner is consulted only between skills, never inside the control loop: the failed skill
has been stopped and the arm holds (ADR 006). Its answer is raw and untrusted; the executor
validates it (`planner.schemas.validate_decision`) before anything runs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


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
        }


class Planner(Protocol):
    """Triage one failure. Returns raw output (a mapping or JSON text), validated afterwards.

    It runs as a task on the loop, like a skill, but in Phase 0 it is given no clock: it must
    answer without awaiting anything (the loop would report it as stuck). Waiting, a planner
    timeout and late answers are Phase 3 (#14).
    """

    async def triage(self, ctx: FailureContext) -> object: ...
