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
