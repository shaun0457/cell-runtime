"""The F/T stream as the AIC scorer reads it (ADR 013), ROS-free.

The scorer tares every `/fts_broadcaster/wrench` message, per axis, with the newest tare offset
it has seen, and skips messages that arrive before the first tare (`ScoringTier2.cc:419-430` @
`3ab0653`). For each message after the first it adds the time since the previous message when
the later one's magnitude is over 20 N (`:863-903`). `WrenchFeed` does the first half in the
ROS callbacks; `time_over_limit` does the second over what a step drained.

Callbacks run on the node's executor threads and the policy thread drains at step ends, so the
feed is behind a lock and bounded: when full, the oldest reading is dropped and counted.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass

from adapters.aic.convert import Vec3
from adapters.aic.node import WrenchReading
from cellrt.monitor.classifier import FORCE_LIMIT_N

WRENCH_BUFFER_SIZE = 256  # about 5 s of the 50 Hz broadcaster


@dataclass(frozen=True)
class TaredWrench:
    """One wrench message with the tare that was newest when it arrived."""

    stamp_s: float
    raw_force: Vec3
    tare_force: Vec3
    raw_torque: Vec3
    tare_torque: Vec3

    @property
    def force_n(self) -> float:
        """The tared force magnitude: per axis, then the magnitude (NaN in, NaN out)."""
        return math.hypot(*(r - t for r, t in zip(self.raw_force, self.tare_force, strict=True)))

    @property
    def torque_nm(self) -> float:
        return math.hypot(*(r - t for r, t in zip(self.raw_torque, self.tare_torque, strict=True)))

    @property
    def finite(self) -> bool:
        """Every value is a number: only such a reading may become a sample's force."""
        values = (
            self.stamp_s,
            *self.raw_force,
            *self.tare_force,
            *self.raw_torque,
            *self.tare_torque,
        )
        return all(math.isfinite(v) for v in values)


@dataclass(frozen=True)
class WrenchDrain:
    """What arrived since the last drain, oldest first, and how many readings were dropped."""

    readings: tuple[TaredWrench, ...]
    dropped: int


class WrenchFeed:
    """Tares each wrench with the newest tare and keeps at most `capacity` readings."""

    def __init__(self, capacity: int = WRENCH_BUFFER_SIZE) -> None:
        if capacity < 1:
            raise ValueError("the wrench buffer must hold at least one reading")
        self._lock = threading.Lock()
        self._readings: deque[TaredWrench] = deque(maxlen=capacity)
        self._tare: tuple[Vec3, Vec3] | None = None
        self._dropped = 0

    def on_tare(self, force: Vec3, torque: Vec3) -> None:
        with self._lock:
            self._tare = (force, torque)

    def on_wrench(self, reading: WrenchReading) -> None:
        with self._lock:
            if self._tare is None:
                return  # before the first tare, as the scorer
            force, torque = self._tare
            if len(self._readings) == self._readings.maxlen:
                self._dropped += 1  # the deque drops the oldest
            self._readings.append(
                TaredWrench(
                    stamp_s=reading.stamp_s,
                    raw_force=reading.force,
                    tare_force=force,
                    raw_torque=reading.torque,
                    tare_torque=torque,
                )
            )

    def drain(self) -> WrenchDrain:
        with self._lock:
            readings, dropped = tuple(self._readings), self._dropped
            self._readings.clear()
            self._dropped = 0
        return WrenchDrain(readings=readings, dropped=dropped)


def time_over_limit(
    previous: TaredWrench | None,
    readings: Sequence[TaredWrench],
    limit_n: float = FORCE_LIMIT_N,
) -> float:
    """The scorer's sum over `readings`, continuing from `previous` (the last reading of the
    step before, so the pair across the boundary counts). Compared with `>` as the scorer does,
    so a NaN reading is never over the limit. A gap that is negative or not a number adds
    nothing, so the result is always a finite number of seconds, at least 0."""
    total = 0.0
    before = previous
    for reading in readings:
        if before is not None and reading.force_n > limit_n:
            gap = reading.stamp_s - before.stamp_s
            if math.isfinite(gap) and gap > 0:
                total += gap
        before = reading
    return total
