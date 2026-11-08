"""The Monitor is the only component that decides a failure class.

It classifies from what the runtime can observe (signals, elapsed time), never from the
ground-truth label of an injected fault.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class FailureClass(StrEnum):
    POSE_ERROR = "POSE_ERROR"
    INSERT_STUCK = "INSERT_STUCK"
    FORCE_LIMIT = "FORCE_LIMIT"
    TIMEOUT = "TIMEOUT"
    SENSOR_DROPOUT = "SENSOR_DROPOUT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Verdict:
    failure_class: FailureClass
    rule: str
    evidence: dict[str, Any] = field(default_factory=dict)


class Monitor:
    def classify_timeout(self, elapsed_s: float, timeout_s: float) -> Verdict:
        return Verdict(
            FailureClass.TIMEOUT,
            rule="skill_timeout",
            evidence={"elapsed_s": round(elapsed_s, 6), "timeout_s": timeout_s},
        )
