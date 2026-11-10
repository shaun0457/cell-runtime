"""The Monitor is the only component that decides a failure class.

It classifies from what the runtime can observe (signals, elapsed time), never from the
ground-truth label of an injected fault.
"""

from __future__ import annotations

import functools
import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from enum import StrEnum
from typing import Any

from cellrt.monitor.profiles import MonitorProfile
from cellrt.monitor.promoted import PromotedRule
from cellrt.runtime.state import FORCE_LIMIT_N
from cellrt.world.base import Signals


class FailureClass(StrEnum):
    POSE_ERROR = "POSE_ERROR"
    INSERT_STUCK = "INSERT_STUCK"
    FORCE_LIMIT = "FORCE_LIMIT"
    TIMEOUT = "TIMEOUT"
    SENSOR_DROPOUT = "SENSOR_DROPOUT"
    UNKNOWN = "UNKNOWN"


# Verdicts about safety: they override a skill's own "done" on the same tick.
SAFETY_CLASSES = frozenset({FailureClass.SENSOR_DROPOUT, FailureClass.FORCE_LIMIT})


@dataclass(frozen=True)
class Verdict:
    failure_class: FailureClass
    rule: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MonitorConfig:
    dropout_s: float = 0.2
    # The force limit itself is the AIC's (`runtime.state.FORCE_LIMIT_N`), not configurable.
    # AIC penalises once the time over the limit, summed over the task, exceeds this.
    force_budget_s: float = 1.0
    # The Monitor fires once the sum reaches this, leaving the rest of the budget for recovery.
    force_trigger_s: float = 0.5
    stuck_force_n: float = 10.0
    stuck_progress_mm: float = 0.2
    stuck_window_s: float = 0.5
    pose_tolerance_mm: float = 1.0
    pose_error_s: float = 0.2
    stall_progress_mm: float = 0.2
    stall_window_s: float = 1.0
    # Approved promoted rules (ADR 021), checked in this order after the known-class rules and
    # before `insert_stalled`.
    promoted: tuple[PromotedRule, ...] = ()

    def __post_init__(self) -> None:
        names = [r.name for r in self.promoted]
        if len(set(names)) != len(names):
            raise ValueError("promoted rules must have distinct names")

    def as_dict(self) -> dict[str, object]:
        """Plain values, for `config_hash`. Promoted rules count only when there are any, so a
        run without them keeps the hash it had before they existed."""
        out: dict[str, object] = {
            f.name: getattr(self, f.name) for f in fields(self) if f.name != "promoted"
        }
        if self.promoted:
            out["promoted"] = [r.model_dump(mode="json") for r in self.promoted]
        return out


class Monitor:
    """Watches the signals of the running skill and returns a verdict once a rule matches.

    Force over the limit is a running sum over the whole task, as in the AIC penalty. The
    Monitor does not keep it: `force_time` reads it from the folded run state, which counts
    every logged sample, also those taken while no skill runs (the planner step). A skill that
    unloads the plug (retract) is not preempted for force. Once the sum reaches the trigger,
    every further sample over the limit is a verdict.

    Two bounded windows, each as long as its longest rule:
    - sensor validity runs across skill boundaries; it restarts after a dropout verdict, so the
      skill that handles it gets the full duration again;
    - insertion (progress, XY) covers only the current skill (`reset` clears it).
    """

    def __init__(
        self,
        period_s: float,
        config: MonitorConfig | None = None,
        *,
        force_time: Callable[[], float],
    ) -> None:
        self.config = c = config or MonitorConfig()
        self._period_s = period_s
        if not 0 < c.force_trigger_s <= c.force_budget_s:
            raise ValueError("force_trigger_s must be positive and at most force_budget_s")
        self._dropout_n = self._held(c.dropout_s)
        self._force_time = force_time  # seconds over the limit so far, this task
        self._stuck_n = self._held(c.stuck_window_s)
        self._pose_n = self._held(c.pose_error_s)
        self._stall_n = self._held(c.stall_window_s)
        promoted = [(rule, self._held(rule.window_s)) for rule in c.promoted]
        self._promoted: list[Callable[[Signals], Verdict | None]] = [
            functools.partial(self._promoted_rule, rule, n) for rule, n in promoted
        ]
        self._safety: deque[Signals] = deque(maxlen=self._dropout_n)
        self._window: deque[Signals] = deque(
            maxlen=max(self._stuck_n, self._pose_n, self._stall_n, *(n for _, n in promoted))
        )
        self._profile: MonitorProfile = "motion"
        self._skill: str | None = None

    def _held(self, seconds: float) -> int:
        """Samples whose span (first to last) is at least `seconds`.

        Rounded up, so a rule never fires early when the period does not divide `seconds`.
        """
        return math.ceil(seconds / self._period_s - 1e-9) + 1

    def _span(self, samples: int) -> float:
        """Time covered by `samples` consecutive samples."""
        return round((samples - 1) * self._period_s, 6)

    def buffered(self) -> int:
        """Samples currently held, in both windows."""
        return len(self._safety) + len(self._window)

    def reset(self, profile: MonitorProfile, *, skill: str | None = None) -> None:
        """A new skill starts, with the profile it declares (`skill` is None for the hold during
        a live planner call): promoted rules that name a skill watch only that one."""
        self._profile = profile
        self._skill = skill
        self._window.clear()

    def observe(self, signals: Signals) -> Verdict | None:
        """The verdict for this sample, or None.

        Every rule is checked, in priority order; the first match is the verdict and the others
        are named in its evidence (`also_matched`).
        """
        self._safety.append(signals)
        self._window.append(signals)
        rules: list[Callable[[Signals], Verdict | None]] = [self._dropout, self._force_limit]
        if self._profile == "insertion":
            rules += [self._stuck, self._pose_error, *self._promoted, self._stalled]
        matched = [v for rule in rules if (v := rule(signals))]
        if not matched:
            return None
        verdict, *others = matched
        if verdict.failure_class is FailureClass.SENSOR_DROPOUT:
            self._safety.clear()  # the skill that handles it gets the full duration again
        if others:
            evidence = {**verdict.evidence, "also_matched": [v.rule for v in others]}
            verdict = Verdict(verdict.failure_class, verdict.rule, evidence)
        return verdict

    def _dropout(self, s: Signals) -> Verdict | None:
        if self._profile == "sensor_wait":
            return None
        if not self._holds(lambda x: not x.wrench_valid, self._dropout_n, self._safety):
            return None
        return Verdict(
            FailureClass.SENSOR_DROPOUT,
            rule="wrench_invalid",
            evidence={"invalid_s": self._span(self._dropout_n)},
        )

    def _force_limit(self, s: Signals) -> Verdict | None:
        c = self.config
        over_s = round(self._force_time(), 6)
        # Over the limit now: the sample reads over it, or the F/T stream was over it during
        # the step (`force_over_limit_s`, a world that reads the stream faster than it samples:
        # stick-slip contact can spike between samples).
        over_now = (s.wrench_valid and s.force_n > FORCE_LIMIT_N) or bool(s.force_over_limit_s)
        if self._profile == "unload" or not over_now or over_s < c.force_trigger_s - 1e-9:
            return None
        evidence: dict[str, Any] = {
            "force_n": s.force_n,
            "limit_n": FORCE_LIMIT_N,
            "over_s": over_s,
            "trigger_s": c.force_trigger_s,
            "budget_s": c.force_budget_s,
        }
        if s.force_over_limit_s is not None:  # what this step added, from the F/T stream
            evidence["step_over_s"] = s.force_over_limit_s
        return Verdict(FailureClass.FORCE_LIMIT, rule="force_over_limit", evidence=evidence)

    def _stuck(self, s: Signals) -> Verdict | None:
        c = self.config
        progress = self._progress(self._stuck_n)
        if not (
            progress is not None
            and progress < c.stuck_progress_mm
            and s.wrench_valid
            and s.force_n > c.stuck_force_n
            and s.xy_error_mm <= c.pose_tolerance_mm
        ):
            return None
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

    def _pose_error(self, s: Signals) -> Verdict | None:
        tolerance = self.config.pose_tolerance_mm
        if not self._holds(lambda x: x.xy_error_mm > tolerance, self._pose_n):
            return None
        return Verdict(
            FailureClass.POSE_ERROR,
            rule="xy_off_during_insert",
            evidence={
                "xy_error_mm": round(s.xy_error_mm, 6),
                "tolerance_mm": tolerance,
                "held_s": self._span(self._pose_n),
            },
        )

    def _stalled(self, s: Signals) -> Verdict | None:
        """Stalled during insertion; the verdict only when no rule before it explains why."""
        stalled = self._progress(self._stall_n)
        if stalled is None or stalled >= self.config.stall_progress_mm:
            return None
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

    def _promoted_rule(self, rule: PromotedRule, samples: int, s: Signals) -> Verdict | None:
        """An approved promoted rule: once its window is full, every condition holds on this
        sample (force, torque magnitude, XY error) and on the progress over the window. A sample
        with an invalid wrench is never read: the dropout rule owns it."""
        if rule.skill != self._skill or not s.wrench_valid:
            return None
        progress = self._progress(samples)
        if progress is None:
            return None
        readings = {
            "force_n": s.force_n,
            "torque_nm": abs(s.torque_nm),
            "xy_error_mm": s.xy_error_mm,
            "progress_mm": progress,
        }
        if not rule.holds(readings):
            return None
        return Verdict(
            FailureClass.UNKNOWN,
            rule=rule.verdict_rule,
            evidence={
                "progress_mm": round(progress, 6),
                "window_s": self._span(samples),
                "force_n": s.force_n,
                "torque_nm": s.torque_nm,
                "xy_error_mm": round(s.xy_error_mm, 6),
            },
        )

    def _progress(self, samples: int) -> float | None:
        """How far the plug went down over the last `samples` samples (None if too few yet)."""
        if len(self._window) < samples:
            return None
        return self._window[-samples].height_mm - self._window[-1].height_mm

    def _holds(
        self,
        condition: Callable[[Signals], bool],
        samples: int,
        window: deque[Signals] | None = None,
    ) -> bool:
        window = self._window if window is None else window
        if len(window) < samples:
            return False
        recent = list(window)[-samples:]
        return all(condition(s) for s in recent)

    def classify_timeout(self, elapsed_s: float, timeout_s: float) -> Verdict:
        return Verdict(
            FailureClass.TIMEOUT,
            rule="skill_timeout",
            evidence={"elapsed_s": round(elapsed_s, 6), "timeout_s": timeout_s},
        )
