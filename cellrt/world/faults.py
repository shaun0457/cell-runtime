"""Concrete faults the mock world applies. Parameters are already drawn (see eval/faults.py).

A fault knows when it is active from world time only; the world turns an active fault into an
effect. Nothing here is visible through `Signals`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cellrt.monitor.classifier import FailureClass


@dataclass(frozen=True)
class ControllerStall:
    """The arm controller stops executing commands for a while (the plug holds still)."""

    fault_id: str
    onset_s: float
    duration_s: float
    kind: str = "controller_stall"
    label: FailureClass = FailureClass.TIMEOUT

    def is_active(self, t: float) -> bool:
        return self.onset_s <= t < self.onset_s + self.duration_s

    @property
    def params(self) -> Mapping[str, Any]:
        return {"onset_s": self.onset_s, "duration_s": self.duration_s}


@dataclass(frozen=True)
class SensorDropout:
    """The wrist force/torque sensor stops reporting valid data for a while."""

    fault_id: str
    onset_s: float
    duration_s: float
    kind: str = "sensor_dropout"
    label: FailureClass = FailureClass.SENSOR_DROPOUT

    def is_active(self, t: float) -> bool:
        return self.onset_s <= t < self.onset_s + self.duration_s

    @property
    def params(self) -> Mapping[str, Any]:
        return {"onset_s": self.onset_s, "duration_s": self.duration_s}


@dataclass(frozen=True)
class Obstacle:
    """Something blocks the port at `depth_mm` during the first `attempts` insertions; it takes
    effect when the plug reaches it."""

    fault_id: str
    depth_mm: float
    attempts: int = 1
    kind: str = "obstacle"
    label: FailureClass = FailureClass.INSERT_STUCK

    @property
    def params(self) -> Mapping[str, Any]:
        return {"depth_mm": self.depth_mm, "attempts": self.attempts}


@dataclass(frozen=True)
class TightFit:
    """Below `onset_depth_mm` the port is too tight: the plug creeps down and the contact force
    grows with the commanded speed (so a slower insertion stays under the force limit)."""

    fault_id: str
    onset_depth_mm: float
    force_n_per_mm_s: float
    kind: str = "tight_fit"
    label: FailureClass = FailureClass.FORCE_LIMIT

    @property
    def params(self) -> Mapping[str, Any]:
        return {"onset_depth_mm": self.onset_depth_mm, "force_n_per_mm_s": self.force_n_per_mm_s}


@dataclass(frozen=True)
class PoseShift:
    """The port moves sideways by `shift_mm` when the plug descends past `trigger_height_mm`
    (after alignment, so the insertion starts off target), on each of the first `attempts`
    descents."""

    fault_id: str
    trigger_height_mm: float
    shift_mm: float
    angle_rad: float
    attempts: int = 1
    kind: str = "pose_shift"
    label: FailureClass = FailureClass.POSE_ERROR

    @property
    def params(self) -> Mapping[str, Any]:
        return {
            "trigger_height_mm": self.trigger_height_mm,
            "shift_mm": self.shift_mm,
            "angle_rad": self.angle_rad,
            "attempts": self.attempts,
        }


@dataclass(frozen=True)
class YawMismatch:
    """The plug is rotated `yaw_deg` against the port: it wedges at `stall_depth_mm` and twists
    instead of going deeper. Force stays moderate and torque is high, which no Monitor rule
    describes. Twisting the plug to within the world's yaw tolerance frees it."""

    fault_id: str
    stall_depth_mm: float
    force_n: float
    torque_nm: float
    yaw_deg: float
    kind: str = "yaw_mismatch"
    label: FailureClass = FailureClass.UNKNOWN

    @property
    def params(self) -> Mapping[str, Any]:
        return {
            "stall_depth_mm": self.stall_depth_mm,
            "force_n": self.force_n,
            "torque_nm": self.torque_nm,
            "yaw_deg": self.yaw_deg,
        }


@dataclass(frozen=True)
class DamagedPort:
    """The port is damaged at its entrance: the plug stops at `stall_depth_mm` under low force
    and low torque, whatever the skill does. Like a yaw mismatch it stalls without a known
    signature, but nothing frees it: not twisting, not pulling out and pushing in again."""

    fault_id: str
    stall_depth_mm: float
    force_n: float
    torque_nm: float
    kind: str = "damaged_port"
    label: FailureClass = FailureClass.UNKNOWN

    @property
    def params(self) -> Mapping[str, Any]:
        return {
            "stall_depth_mm": self.stall_depth_mm,
            "force_n": self.force_n,
            "torque_nm": self.torque_nm,
        }


TimedFault = ControllerStall | SensorDropout
Fault = (
    ControllerStall | SensorDropout | Obstacle | TightFit | PoseShift | YawMismatch | DamagedPort
)
