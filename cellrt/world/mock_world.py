"""A simplified insertion model. Not physically correct: just enough for every failure to happen.

The plug follows the commanded velocity through an impedance spring: the controller integrates
the command into a target height, and contact force is the spring stretch between target and
actual height. Outside the hole tolerance the plug rests on the rim; inside it slides down to the
bottom of the port with friction that grows with speed.

Faults change this model: timed faults (controller stall, sensor dropout) by world time, the
others when the plug reaches them (an obstacle at a depth, a tight section, a wedge from a yaw
mismatch, a port that shifts as the plug descends past a height). See `world/faults.py`.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass

from cellrt.world.base import HOLD, FaultTransition, Signals, Velocity
from cellrt.world.faults import Fault, Obstacle, PoseShift, TightFit, TimedFault, YawMismatch


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
    # In a tight fit the plug moves down at this fraction of the commanded speed.
    tight_fit_speed_ratio: float = 0.5
    xy_noise_mm: float = 0.05
    force_noise_n: float = 0.2
    # Sensor resolution: signals are quantised, so the log holds exactly what the runtime saw.
    length_resolution_mm: float = 1e-4
    force_resolution_n: float = 1e-3
    torque_resolution_nm: float = 1e-4


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
        self._torque = 0.0
        self._steps = 0
        self._wrench_valid = True
        for fault in faults:
            if isinstance(fault, TimedFault) and fault.duration_s < p.dt:
                raise ValueError(f"{fault.fault_id} lasts less than one world step ({p.dt} s)")
        self._faults = tuple(faults)
        self._obstacles = [f for f in faults if isinstance(f, Obstacle)]
        self._tight_fits = [f for f in faults if isinstance(f, TightFit)]
        self._pose_shifts = [f for f in faults if isinstance(f, PoseShift)]
        self._yaw_mismatches = [f for f in faults if isinstance(f, YawMismatch)]
        self._active: set[str] = set()
        self._descents: dict[str, int] = {}  # descents past each pose shift's trigger height
        self._entries = 0  # insertions so far: the plug tip going from above the port to inside
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
        active = self._update_timed_faults(round(self._steps * dt, 9))
        self._wrench_valid = "sensor_dropout" not in active
        cmd = HOLD if "controller_stall" in active else self._cmd
        vx, vy, vz = (_clamp(v, p.max_speed_mm_s) for v in (cmd.vx, cmd.vy, cmd.vz))
        if self._height > 0:  # lateral motion only outside the port
            self._xy = (self._xy[0] + vx * dt, self._xy[1] + vy * dt)
        previous_height = self._height
        self._move_vertically(vz, dt, previous_height)
        if previous_height > 0 >= self._height:
            self._count_entry()
        self._shift_port(previous_height)
        self._measured = self._measure()

    def _move_vertically(self, vz: float, dt: float, previous_height: float) -> None:
        p = self.params
        self._target_height += vz * dt
        self._torque = 0.0
        in_hole = math.hypot(*self._xy) <= p.hole_tolerance_mm
        floor = -p.full_depth_mm if in_hole else 0.0
        if in_hole:
            for obstacle in self._obstacles:
                if self._entries <= obstacle.attempts:
                    floor = max(floor, -obstacle.depth_mm)
            for yaw in self._yaw_mismatches:
                if self._target_height <= -yaw.stall_depth_mm and -yaw.stall_depth_mm > floor:
                    self._wedge(yaw)
                    return
            for tight in self._tight_fits:
                if vz < 0 and self._target_height < -tight.onset_depth_mm:
                    self._creep(tight, previous_height, vz, floor, dt)
                    return
        self._height = max(self._target_height, floor)
        if in_hole:
            for obstacle in self._obstacles:
                if self._entries <= obstacle.attempts and self._height <= -obstacle.depth_mm:
                    self._engage(obstacle)
        stretch = self._height - self._target_height  # > 0 when pushing against rim or bottom
        friction = 0.0
        if self._height < 0 and vz < 0 and stretch == 0:
            friction = p.friction_base_n + p.friction_n_per_mm_s * abs(vz)
        self._force = p.contact_stiffness_n_per_mm * stretch + friction

    def _shift_port(self, previous_height: float) -> None:
        """Pose shift: the port moves sideways when the plug descends past the trigger height."""
        for shift in self._pose_shifts:
            if not previous_height > shift.trigger_height_mm >= self._height:
                continue
            self._descents[shift.fault_id] = descent = self._descents.get(shift.fault_id, 0) + 1
            if descent > shift.attempts:
                self._clear(shift)
            else:
                self._engage(shift)
                self._xy = (
                    self._xy[0] + shift.shift_mm * math.cos(shift.angle_rad),
                    self._xy[1] + shift.shift_mm * math.sin(shift.angle_rad),
                )

    def _creep(
        self, tight: TightFit, previous_height: float, vz: float, floor: float, dt: float
    ) -> None:
        """Tight fit: the plug creeps down from where the tight section starts, and the force
        grows with the commanded speed instead of with spring stretch."""
        self._engage(tight)
        start = min(previous_height, -tight.onset_depth_mm)
        speed = self.params.tight_fit_speed_ratio * abs(vz)
        self._height = self._target_height = max(start - speed * dt, floor)
        self._force = tight.force_n_per_mm_s * abs(vz)

    def _wedge(self, yaw: YawMismatch) -> None:
        """Yaw mismatch: the plug stops at the stall depth and twists; the wrist compliance takes
        up the push, so force stays at the fault's level instead of building up."""
        self._engage(yaw)
        self._height = self._target_height = -yaw.stall_depth_mm
        self._force = yaw.force_n
        self._torque = yaw.torque_nm

    def _update_timed_faults(self, t: float) -> set[str]:
        """Kinds of the timed faults active at world time `t`; records activation and clearing."""
        kinds: set[str] = set()
        for fault in self._faults:
            if not isinstance(fault, TimedFault):
                continue
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

    def _count_entry(self) -> None:
        """A new insertion: faults whose attempts are used up stop affecting the plug."""
        self._entries += 1
        for fault in self._obstacles:
            if self._entries > fault.attempts:
                self._clear(fault)

    def _clear(self, fault: Fault) -> None:
        """A fault triggered by the plug's state stops affecting new attempts."""
        if fault.fault_id in self._active:
            self._active.discard(fault.fault_id)
            self._transitions.append(FaultTransition(fault=fault, active=False))

    def _engage(self, fault: Fault) -> None:
        """A fault triggered by the plug's state takes effect (until its attempts are used up)."""
        if fault.fault_id not in self._active:
            self._active.add(fault.fault_id)
            self._transitions.append(FaultTransition(fault=fault, active=True))

    def _measure(self) -> Signals:
        p = self.params
        offset = (
            self._xy[0] + self._rng.gauss(0.0, p.xy_noise_mm),
            self._xy[1] + self._rng.gauss(0.0, p.xy_noise_mm),
        )
        # Drawn even during a dropout, so a dropout does not shift the noise of later samples.
        force = max(0.0, self._force + self._rng.gauss(0.0, p.force_noise_n))
        mm, n = p.length_resolution_mm, p.force_resolution_n
        return Signals(
            height_mm=_quantise(self._height, mm),
            xy_offset_mm=(_quantise(offset[0], mm), _quantise(offset[1], mm)),
            force_n=_quantise(force, n) if self._wrench_valid else 0.0,
            torque_nm=_quantise(self._torque, p.torque_resolution_nm)
            if self._wrench_valid
            else 0.0,
            wrench_valid=self._wrench_valid,
        )


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


def _quantise(value: float, resolution: float) -> float:
    return round(round(value / resolution) * resolution, 6)
