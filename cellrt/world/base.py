"""What the runtime sees of the world, and the one command it can send."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Signals:
    """One sample of the cell's sensors.

    `height_mm` is the plug tip above the port surface (negative once inside the port).
    `xy_offset_mm` is the measured plug-to-port offset in the port plane.
    """

    height_mm: float
    xy_offset_mm: tuple[float, float]
    force_n: float
    torque_nm: float
    wrench_valid: bool

    @property
    def depth_mm(self) -> float:
        return max(0.0, -self.height_mm)

    @property
    def xy_error_mm(self) -> float:
        return math.hypot(*self.xy_offset_mm)


@dataclass(frozen=True)
class Velocity:
    """Cartesian velocity command in mm/s (like the AIC twist / ACT action)."""

    vx: float = 0.0
    vy: float = 0.0
    vz: float = 0.0


HOLD = Velocity()


class World(Protocol):
    def signals(self) -> Signals: ...

    def command(self, cmd: Velocity) -> None: ...

    def step(self, dt: float) -> None: ...
