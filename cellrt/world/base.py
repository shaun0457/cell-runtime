"""What the runtime sees of the world, and the one command it can send."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
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


class CommandLease:
    """The right of one skill run to command the world, held by the executor.

    Once revoked (the skill finished or was stopped), the run's commands no longer reach the
    world: a stopped skill cannot keep the arm moving. The first dropped command is reported
    through `on_dropped`; later ones are dropped silently, so a runaway skill cannot flood the
    log.
    """

    __slots__ = ("_active", "_on_dropped", "_reported")

    def __init__(self, on_dropped: Callable[[], None] | None = None) -> None:
        self._active = True
        self._on_dropped = on_dropped
        self._reported = False

    @property
    def active(self) -> bool:
        return self._active

    def revoke(self) -> None:
        self._active = False

    def dropped(self) -> None:
        if self._on_dropped is not None and not self._reported:
            self._reported = True
            self._on_dropped()


class WorldView:
    """Hands skills only `signals` and `command`, so world internals and injected-fault ground
    truth stay out of reach. Commands pass only while the run's lease is active."""

    __slots__ = ("_lease", "_world")

    def __init__(self, world: World, lease: CommandLease) -> None:
        self._world = world
        self._lease = lease

    def signals(self) -> Signals:
        return self._world.signals()

    def command(self, cmd: Velocity) -> None:
        if self._lease.active:
            self._world.command(cmd)
        else:
            self._lease.dropped()


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
