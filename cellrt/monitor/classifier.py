"""The Monitor is the only component that decides a failure class.

It classifies from what the runtime can observe (signals, elapsed time), never from the
ground-truth label of an injected fault.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from cellrt.world.base import Signals


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


@dataclass(frozen=True)
class MonitorConfig:
    # Skills during which the insertion rules apply.
    insert_skills: frozenset[str] = frozenset({"insert_scripted", "insert_act"})
    dropout_s: float = 0.2
    force_limit_n: float = 20.0
    force_limit_s: float = 1.0  # exceeded only when held for longer than this
    stuck_force_n: float = 10.0
    stuck_progress_mm: float = 0.2
    stuck_window_s: float = 0.5
    pose_tolerance_mm: float = 1.0
    pose_error_s: float = 0.2
    stall_progress_mm: float = 0.2
    stall_window_s: float = 1.0


class Monitor:
    """Watches the signals of the running skill and returns a verdict once a rule matches.

    The window covers only the current skill (`reset` clears it) and holds at most as many samples
    as the longest rule needs.
    """

    def __init__(self, period_s: float, config: MonitorConfig | None = None) -> None:
        self.config = c = config or MonitorConfig()
        self._period_s = period_s
        self._dropout_n = self._held(c.dropout_s)
        self._force_n = self._held(c.force_limit_s, strictly_longer=True)
        self._stuck_n = self._held(c.stuck_window_s)
        self._pose_n = self._held(c.pose_error_s)
        self._stall_n = self._held(c.stall_window_s)
        self._window: deque[Signals] = deque(
            maxlen=max(self._dropout_n, self._force_n, self._stuck_n, self._pose_n, self._stall_n)
        )
        self._skill = ""

    def _held(self, seconds: float, *, strictly_longer: bool = False) -> int:
        """Samples whose span (first to last) is at least `seconds`, or more than it.

        Rounded up, so a rule never fires early when the period does not divide `seconds`.
        """
        steps = seconds / self._period_s
        if strictly_longer:
            return math.floor(steps + 1e-9) + 2
        return math.ceil(steps - 1e-9) + 1

    def _span(self, samples: int) -> float:
        """Time covered by `samples` consecutive samples."""
        return round((samples - 1) * self._period_s, 6)

    def buffered(self) -> int:
        """Samples currently held."""
        return len(self._window)

    def reset(self, skill: str) -> None:
        self._skill = skill
        self._window.clear()

    def observe(self, signals: Signals) -> Verdict | None:
        self._window.append(signals)
        if self._holds(lambda s: not s.wrench_valid, self._dropout_n):
            return Verdict(
                FailureClass.SENSOR_DROPOUT,
                rule="wrench_invalid",
                evidence={"invalid_s": self._span(self._dropout_n)},
            )
        c = self.config
        if self._holds(lambda s: s.wrench_valid and s.force_n > c.force_limit_n, self._force_n):
            return Verdict(
                FailureClass.FORCE_LIMIT,
                rule="force_over_limit",
                evidence={
                    "min_force_n": min(s.force_n for s in list(self._window)[-self._force_n :]),
                    "limit_n": c.force_limit_n,
                    "over_s": self._span(self._force_n),
                },
            )
        if self._skill in c.insert_skills:
            return self._insertion_verdict(signals)
        return None

    def _insertion_verdict(self, s: Signals) -> Verdict | None:
        c = self.config
        progress = self._progress(self._stuck_n)
        if (
            progress is not None
            and progress < c.stuck_progress_mm
            and s.wrench_valid
            and s.force_n > c.stuck_force_n
            and s.xy_error_mm <= c.pose_tolerance_mm
        ):
            return Verdict(
                FailureClass.INSERT_STUCK,
                rule="stuck_under_force",
                evidence={
                    "force_n": s.force_n,
                    "progress_mm": round(progress, 6),
                    "window_s": self._span(self._stuck_n),
                    "xy_error_mm": round(s.xy_error_mm, 6),
                },
            )
        if self._holds(lambda x: x.xy_error_mm > c.pose_tolerance_mm, self._pose_n):
            return Verdict(
                FailureClass.POSE_ERROR,
                rule="xy_off_during_insert",
                evidence={
                    "xy_error_mm": round(s.xy_error_mm, 6),
                    "tolerance_mm": c.pose_tolerance_mm,
                    "held_s": self._span(self._pose_n),
                },
            )
        stalled = self._progress(self._stall_n)
        if stalled is not None and stalled < c.stall_progress_mm:
            # Stalled, but no rule above explains why.
            return Verdict(
                FailureClass.UNKNOWN,
                rule="insert_stalled",
                evidence={
                    "progress_mm": round(stalled, 6),
                    "window_s": self._span(self._stall_n),
                    "force_n": s.force_n,
                    "torque_nm": s.torque_nm,
                    "xy_error_mm": round(s.xy_error_mm, 6),
                },
            )
        return None

    def _progress(self, samples: int) -> float | None:
        """How far the plug went down over the last `samples` samples (None if too few yet)."""
        if len(self._window) < samples:
            return None
        return self._window[-samples].height_mm - self._window[-1].height_mm

    def _holds(self, condition: Callable[[Signals], bool], samples: int) -> bool:
        if len(self._window) < samples:
            return False
        recent = list(self._window)[-samples:]
        return all(condition(s) for s in recent)

    def classify_timeout(self, elapsed_s: float, timeout_s: float) -> Verdict:
        return Verdict(
            FailureClass.TIMEOUT,
            rule="skill_timeout",
            evidence={"elapsed_s": round(elapsed_s, 6), "timeout_s": timeout_s},
        )
