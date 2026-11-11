"""Calibration measurements (#83, ADR 030): what the Monitor's thresholds meet in a world, read
from a run's events. Measure only: nothing here changes a threshold (ADR 030, D5).

Every value comes from the run's `signal_sampled` events, each logged at the run clock's time
after a world step (`executor/runner.py`), and from the skill running when it was logged:
- **Sample period**: the time between consecutive samples. In the AIC cell a step waits for an
  observation, which comes at the camera rate, so the period and its jitter are the grain of the
  Monitor's windows (0.2 s, 0.5 s, 1 s).
- **Wrench health**: samples whose wrench was not fresh (`wrench_valid` False), and the readings
  the world dropped (`wrench_dropped`).
- **Free-space force**: the tared force magnitude in every fresh sample of `approach`, which
  carries the plug through free space: noise, the arm's accelerations and the cable's swing,
  against the 10 N of `stuck_force_n`.
- **Tare residual at rest**: the tared force magnitude in fresh samples where the plug has moved
  less than `REST_MM` in each of the last `REST_STEPS` steps (height and both axis offsets) and
  is higher above the port than `free_above_mm` (the caller's entrance height): the plug still
  and clear of the port. Kept apart for `approach` and `align`: align holds near the board
  (ADR 024), so a difference between the two hints at contact or a cable resting on something,
  which the plug's height cannot rule out.
- **Force verdicts**: each INSERT_STUCK and FORCE_LIMIT verdict, timed from the start of its
  rise:
  - INSERT_STUCK: from the first fresh sample above `stuck_n` of the run of such samples the
    verdict ends; a sample whose wrench is not fresh neither starts nor ends a run, as the
    Monitor's window does not.
  - FORCE_LIMIT: from the run's first step over the limit, as the Monitor counts one: a fresh
    sample above `limit_n`, or time over the limit in the F/T stream during the step
    (`force_over_limit_s`, which catches spikes between samples). The Monitor sums that time
    over the whole task, so this is the time the sum took to reach its trigger. The verdict's
    `over_s` (the sum when it fired) is kept, against the 0.5 s trigger and the 1 s budget.

  Only the first verdict of a rise (INSERT_STUCK) or of the run (FORCE_LIMIT) is timed: the
  Monitor repeats a FORCE_LIMIT verdict on every further sample over the limit. A verdict with
  no rise before it is untimed. Both are counted.

`summarize` gives a series' mean, population sd, nearest-rank p95 and max; with fewer than
`MIN_VALUES` values it gives none ("undefined" in a report).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from statistics import fmean, pstdev
from typing import Any

from cellrt.monitor.classifier import FailureClass, MonitorConfig
from cellrt.runtime.events import (
    Event,
    MonitorVerdict,
    SignalSampled,
    SkillCancelled,
    SkillFinished,
    SkillStarted,
)
from cellrt.runtime.state import FORCE_LIMIT_N

MIN_VALUES = 10
REST_MM = 0.05  # a tenth of a descent step: the plug did not move in a step
REST_STEPS = 5  # ... for this many steps in a row: 0.25 s at the camera rate
STUCK_N = MonitorConfig().stuck_force_n
LIMIT_N = FORCE_LIMIT_N
INSERT_STUCK, FORCE_LIMIT = FailureClass.INSERT_STUCK.value, FailureClass.FORCE_LIMIT.value
FORCE_CLASSES = (INSERT_STUCK, FORCE_LIMIT)
FREE_SPACE_SKILL = "approach"
REST_SKILLS = ("approach", "align")


@dataclass(frozen=True)
class ForceVerdict:
    """A force verdict, and how long after the start of its rise it came."""

    failure_class: str
    rise_s: float | None  # None: a repeat, or untimed (no rise before it)
    repeat: bool = False  # not the first verdict of its rise (INSERT_STUCK) or run (FORCE_LIMIT)
    over_s: float | None = None  # FORCE_LIMIT: the Monitor's sum over the limit when it fired


@dataclass(frozen=True)
class Measured:
    """One run's raw series; a report pools them over trials before summarizing."""

    samples: int = 0
    periods_s: tuple[float, ...] = ()
    not_fresh: int = 0
    dropped: int = 0
    free_space_n: tuple[float, ...] = ()
    rest_approach_n: tuple[float, ...] = ()
    rest_align_n: tuple[float, ...] = ()
    force_verdicts: tuple[ForceVerdict, ...] = ()


@dataclass(frozen=True)
class Summary:
    n: int
    mean: float
    sd: float
    p95: float
    max: float


@dataclass
class _Rise:
    since: float | None = None
    timed: bool = False  # a verdict was timed against this rise

    def start(self, ts: float) -> None:
        if self.since is None:
            self.since, self.timed = ts, False

    def end(self) -> None:
        self.since, self.timed = None, False

    def verdict(self, failure_class: str, ts: float, over_s: float | None) -> ForceVerdict:
        if self.since is None:
            return ForceVerdict(failure_class, None, over_s=over_s)
        if self.timed:
            return ForceVerdict(failure_class, None, repeat=True, over_s=over_s)
        self.timed = True
        return ForceVerdict(failure_class, ts - self.since, over_s=over_s)


def measure(
    events: Sequence[Event],
    *,
    free_above_mm: float | None,
    stuck_n: float = STUCK_N,
    limit_n: float = LIMIT_N,
) -> Measured:
    """The run's series, from its events in log order (see the module docstring)."""
    samples = not_fresh = dropped = still = 0
    periods: list[float] = []
    free: list[float] = []
    rest: dict[str, list[float]] = {skill: [] for skill in REST_SKILLS}
    verdicts: list[ForceVerdict] = []
    skill: str | None = None
    last: tuple[float, SignalSampled] | None = None
    rises = {INSERT_STUCK: _Rise(), FORCE_LIMIT: _Rise()}
    for event in events:
        match event.payload:
            case SkillStarted(skill=name):
                skill = name
            case SkillFinished(skill=name) | SkillCancelled(skill=name) if name == skill:
                skill = None
            case MonitorVerdict(failure_class=failure_class, evidence=evidence) if (
                failure_class in FORCE_CLASSES
            ):
                over_s = _finite(evidence.get("over_s")) if failure_class == FORCE_LIMIT else None
                verdicts.append(rises[failure_class].verdict(failure_class, event.ts, over_s))
            case SignalSampled() as s:
                samples += 1
                not_fresh += not s.wrench_valid
                dropped += s.wrench_dropped or 0
                still = still + 1 if last is not None and _still(last[1], s) else 0
                if last is not None:
                    periods.append(event.ts - last[0])
                if s.wrench_valid and skill == FREE_SPACE_SKILL:
                    free.append(s.force_n)
                if (
                    s.wrench_valid
                    and skill in rest
                    and still >= REST_STEPS
                    and (free_above_mm is None or s.height_mm > free_above_mm)
                ):
                    rest[skill].append(s.force_n)
                if s.wrench_valid:  # a stale sample neither starts nor ends a run above it
                    if s.force_n > stuck_n:
                        rises[INSERT_STUCK].start(event.ts)
                    else:
                        rises[INSERT_STUCK].end()
                if (s.wrench_valid and s.force_n > limit_n) or bool(s.force_over_limit_s):
                    rises[FORCE_LIMIT].start(event.ts)  # summed over the run: it never ends
                last = (event.ts, s)
    return Measured(
        samples=samples,
        periods_s=tuple(periods),
        not_fresh=not_fresh,
        dropped=dropped,
        free_space_n=tuple(free),
        rest_approach_n=tuple(rest["approach"]),
        rest_align_n=tuple(rest["align"]),
        force_verdicts=tuple(verdicts),
    )


def summarize(values: Iterable[float], *, min_values: int = MIN_VALUES) -> Summary | None:
    """Mean, population sd, nearest-rank p95 and max; None with fewer than `min_values`."""
    if min_values < 1:
        raise ValueError("min_values must be at least 1")
    ordered = sorted(values)
    n = len(ordered)
    if n < min_values:
        return None
    return Summary(
        n=n,
        mean=fmean(ordered),
        sd=pstdev(ordered),
        p95=ordered[math.ceil(0.95 * n) - 1],
        max=ordered[-1],
    )


def _still(before: SignalSampled, s: SignalSampled) -> bool:
    """Whether the plug moved under `REST_MM` between two samples; False when either lacks an
    offset (a log from before `xy_offset_mm`)."""
    if before.xy_offset_mm is None or s.xy_offset_mm is None:
        return False
    moved = math.dist((before.height_mm, *before.xy_offset_mm), (s.height_mm, *s.xy_offset_mm))
    return moved < REST_MM


def _finite(value: Any) -> float | None:
    """A verdict's evidence is the log's: a number only if it is one."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None
