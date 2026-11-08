"""A simplified insertion model. Not physically correct: just enough for every failure to happen.

The plug follows the commanded velocity through an impedance spring: the controller integrates
the command into a target height, and contact force is the spring stretch between target and
actual height. Outside the hole tolerance the plug rests on the rim; inside it slides down to the
bottom of the port with friction that grows with speed.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass

from cellrt.world.base import HOLD, FaultTransition, Signals, Velocity
from cellrt.world.faults import Fault


@dataclass(frozen=True)
class MockParams:
    dt: float = 0.05
    start_height_mm: float = 50.0
    initial_xy_range_mm: float = 3.0
    hole_tolerance_mm: float = 1.0
    full_depth_mm: float = 8.0
    max_speed_mm_s: float = 30.0
    contact_stiffness_n_per_mm: float = 5.0
    friction_base_n: float = 2.0
    friction_n_per_mm_s: float = 1.0
    xy_noise_mm: float = 0.05
    force_noise_n: float = 0.2
    # Sensor resolution: signals are quantised, so the log holds exactly what the runtime saw.
    length_resolution_mm: float = 1e-4
    force_resolution_n: float = 1e-3


class MockWorld:
    def __init__(
        self,
        rng: random.Random,
        params: MockParams | None = None,
        faults: Sequence[Fault] = (),
    ) -> None:
        self._rng = rng
        self.params = p = params or MockParams()
        self._height = p.start_height_mm
        self._target_height = p.start_height_mm
        r = p.initial_xy_range_mm
        self._xy = (rng.uniform(-r, r), rng.uniform(-r, r))
        self._cmd = HOLD
        self._force = 0.0
        self._steps = 0
        for fault in faults:
            if fault.duration_s < p.dt:
                raise ValueError(f"{fault.fault_id} lasts less than one world step ({p.dt} s)")
        self._faults = tuple(faults)
        self._active: set[str] = set()
        self._transitions: list[FaultTransition] = []
        self._measured = self._measure()

    def command(self, cmd: Velocity) -> None:
        self._cmd = cmd

    def signals(self) -> Signals:
        return self._measured

    def take_fault_transitions(self) -> list[FaultTransition]:
        transitions, self._transitions = self._transitions, []
        return transitions

    def step(self, dt: float) -> None:
        p = self.params
        self._steps += 1
        active = self._update_faults(round(self._steps * dt, 9))
        cmd = HOLD if "controller_stall" in active else self._cmd
        vx, vy, vz = (_clamp(v, p.max_speed_mm_s) for v in (cmd.vx, cmd.vy, cmd.vz))
        if self._height > 0:  # lateral motion only outside the port
            self._xy = (self._xy[0] + vx * dt, self._xy[1] + vy * dt)
        self._target_height += vz * dt
        in_hole = math.hypot(*self._xy) <= p.hole_tolerance_mm
        floor = -p.full_depth_mm if in_hole else 0.0
        self._height = max(self._target_height, floor)
        stretch = self._height - self._target_height  # > 0 when pushing against rim or bottom
        friction = 0.0
        if self._height < 0 and vz < 0 and stretch == 0:
            friction = p.friction_base_n + p.friction_n_per_mm_s * abs(vz)
        self._force = p.contact_stiffness_n_per_mm * stretch + friction
        self._measured = self._measure()

    def _update_faults(self, t: float) -> set[str]:
        """Kinds of the faults active at world time `t`; records every activation and clearing."""
        kinds: set[str] = set()
        for fault in self._faults:
            now_active = fault.is_active(t)
            if now_active != (fault.fault_id in self._active):
                self._transitions.append(FaultTransition(fault=fault, active=now_active))
                if now_active:
                    self._active.add(fault.fault_id)
                else:
                    self._active.discard(fault.fault_id)
            if now_active:
                kinds.add(fault.kind)
        return kinds

    def _measure(self) -> Signals:
        p = self.params
        offset = (
            self._xy[0] + self._rng.gauss(0.0, p.xy_noise_mm),
            self._xy[1] + self._rng.gauss(0.0, p.xy_noise_mm),
        )
        force = max(0.0, self._force + self._rng.gauss(0.0, p.force_noise_n))
        mm, n = p.length_resolution_mm, p.force_resolution_n
        return Signals(
            height_mm=_quantise(self._height, mm),
            xy_offset_mm=(_quantise(offset[0], mm), _quantise(offset[1], mm)),
            force_n=_quantise(force, n),
            torque_nm=0.0,
            wrench_valid=True,
        )


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


def _quantise(value: float, resolution: float) -> float:
    return round(round(value / resolution) * resolution, 6)
