"""What the runtime sees of the world, and the one command it can send."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Signals:
    """One sample of the cell's sensors.

    `height_mm` is the plug tip above the port surface (negative once inside the port).
    `xy_offset_mm` is the measured plug-to-port offset in the port plane.
    `raw_force_n` is the F/T force as the sensor gives it (x, y, z in the sensor frame), not
    tared; `tare_offset_n` is the offset published beside it, in the same frame (AIC:
    `wrist_wrench.force` and the latest `controller_state.fts_tare_offset.force`). Tare per axis,
    then take the magnitude, as the AIC scorer does; the difference of the two magnitudes
    underestimates contact force. Only `force_n`, the tared magnitude, means contact force.
    `torque_nm` is a magnitude an adapter must tare the same way (the mock has no torque offset).
    """

    height_mm: float
    xy_offset_mm: tuple[float, float]
    raw_force_n: tuple[float, float, float]
    tare_offset_n: tuple[float, float, float]
    torque_nm: float
    wrench_valid: bool

    @property
    def force_n(self) -> float:
        """Tared contact force magnitude (0 while the reading is invalid)."""
        if not self.wrench_valid:
            return 0.0
        tared = (r - o for r, o in zip(self.raw_force_n, self.tare_offset_n, strict=True))
        return round(math.hypot(*tared), 6)

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


class SkillWorld(Protocol):
    """The part of the world a skill may use: read sensors, send a command."""

    def signals(self) -> Signals: ...

    def command(self, cmd: Velocity) -> None: ...


class WorldView:
    """Hands skills only `signals` and `command`, so world internals and injected-fault ground
    truth stay out of reach."""

    __slots__ = ("_world",)

    def __init__(self, world: World) -> None:
        self._world = world

    def signals(self) -> Signals:
        return self._world.signals()

    def command(self, cmd: Velocity) -> None:
        self._world.command(cmd)


class InjectedFault(Protocol):
    @property
    def fault_id(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def label(self) -> str: ...

    @property
    def params(self) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class FaultTransition:
    """Ground truth for evaluation: a fault became active (or cleared) during the last step."""

    fault: InjectedFault
    active: bool


class FaultSource(Protocol):
    """A world with injected faults reports their transitions; only the runner reads them."""

    def take_fault_transitions(self) -> list[FaultTransition]: ...
