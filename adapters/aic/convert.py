"""Conversions between AIC (ROS) data and core types, as pure functions on plain data.

ROS-free, so they run on Windows CI. The ROS shell (`RuntimePolicy`) only copies message fields
into these types and back. Facts are from `shaun0457/aic` @ `3ab0653`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

from cellrt.runtime.task import Task
from cellrt.world.base import HOLD, Command, PoseTarget, Velocity

M_TO_MM = 1000.0
BASE_FRAME = "base_link"

Vec3 = tuple[float, float, float]
Quat = tuple[float, float, float, float]  # (w, x, y, z), as geometry_msgs orders x, y, z, w

# `Policy.set_pose_target`, which CheatCode uses (aic_model/policy.py:90-130).
POSITION_STIFFNESS = (90.0, 90.0, 90.0, 50.0, 50.0, 50.0)
POSITION_DAMPING = (50.0, 50.0, 50.0, 20.0, 20.0, 20.0)
# RunACT's velocity commands (aic_example_policies/ros/RunACT.py:200-205).
VELOCITY_STIFFNESS = (100.0, 100.0, 100.0, 50.0, 50.0, 50.0)
VELOCITY_DAMPING = (40.0, 40.0, 40.0, 15.0, 15.0, 15.0)
# Both use the same wrench feedback at the tip.
WRENCH_FEEDBACK_GAINS = (0.5, 0.5, 0.5, 0.0, 0.0, 0.0)


@dataclass(frozen=True)
class Pose:
    """A pose in metres with a unit quaternion (w, x, y, z): a TF transform or a
    geometry_msgs/Pose, both expressed in the base frame."""

    position: Vec3
    orientation: Quat = (1.0, 0.0, 0.0, 0.0)


@dataclass(frozen=True)
class MotionCommand:
    """Every field of an AIC `MotionUpdate` (aic_control_interfaces/msg/MotionUpdate.msg) as
    plain data; the ROS shell copies it into the message as is."""

    mode: Literal["position", "velocity"]
    frame_id: str
    position_m: Vec3
    orientation: Quat
    linear_m_s: Vec3
    angular_rad_s: Vec3
    stiffness: tuple[float, ...]  # 6x6 row-major
    damping: tuple[float, ...]  # 6x6 row-major
    wrench_feedback_gains: tuple[float, ...]


def diag6(values: tuple[float, ...]) -> tuple[float, ...]:
    """A 6x6 row-major matrix with `values` on the diagonal (numpy's `diag(...).flatten()`)."""
    if len(values) != 6:
        raise ValueError(f"need 6 diagonal values, got {len(values)}")
    return tuple(values[i] if i == j else 0.0 for i in range(6) for j in range(6))


def core_task(msg: Any) -> Task:
    """The core `Task` from an `aic_task_interfaces/Task` message (or anything with its
    fields). `time_limit` is a whole number of seconds there (uint64)."""
    return Task(
        id=msg.id,
        cable_type=msg.cable_type,
        cable_name=msg.cable_name,
        plug_type=msg.plug_type,
        plug_name=msg.plug_name,
        port_type=msg.port_type,
        port_name=msg.port_name,
        target_module_name=msg.target_module_name,
        time_limit=float(msg.time_limit),
    )


def mm(metres: float) -> float:
    return metres * M_TO_MM


def to_pose_target(pose: Pose) -> PoseTarget:
    """A base-frame pose (metres) as the core's pose target (millimetres)."""
    x, y, z = pose.position
    qw, qx, qy, qz = pose.orientation
    return PoseTarget(x_mm=mm(x), y_mm=mm(y), z_mm=mm(z), qw=qw, qx=qx, qy=qy, qz=qz)


def motion_command(cmd: Command, tcp: Pose | None) -> MotionCommand:
    """The motion update for a core command.

    HOLD (any zero velocity) becomes a position target at the measured TCP pose, so the arm
    stays where it is and any velocity the controller was applying is replaced (ADR 014). With
    no measured pose yet, HOLD is sent as a zero twist.
    """
    if isinstance(cmd, Velocity) and cmd == HOLD and tcp is not None:
        return _position(tcp.position, tcp.orientation)
    if isinstance(cmd, PoseTarget):
        position = (cmd.x_mm / M_TO_MM, cmd.y_mm / M_TO_MM, cmd.z_mm / M_TO_MM)
        return _position(position, (cmd.qw, cmd.qx, cmd.qy, cmd.qz))
    if isinstance(cmd, Velocity):
        return MotionCommand(
            mode="velocity",
            frame_id=BASE_FRAME,
            position_m=(0.0, 0.0, 0.0),
            orientation=(1.0, 0.0, 0.0, 0.0),
            linear_m_s=(cmd.vx / M_TO_MM, cmd.vy / M_TO_MM, cmd.vz / M_TO_MM),
            angular_rad_s=(0.0, 0.0, math.radians(cmd.wz)),
            stiffness=diag6(VELOCITY_STIFFNESS),
            damping=diag6(VELOCITY_DAMPING),
            wrench_feedback_gains=WRENCH_FEEDBACK_GAINS,
        )
    raise TypeError(f"not a command: {type(cmd).__name__}")


def _position(position_m: Vec3, orientation: Quat) -> MotionCommand:
    return MotionCommand(
        mode="position",
        frame_id=BASE_FRAME,
        position_m=position_m,
        orientation=orientation,
        linear_m_s=(0.0, 0.0, 0.0),
        angular_rad_s=(0.0, 0.0, 0.0),
        stiffness=diag6(POSITION_STIFFNESS),
        damping=diag6(POSITION_DAMPING),
        wrench_feedback_gains=WRENCH_FEEDBACK_GAINS,
    )


def plug_to_port_mm(port: Pose, plug: Pose) -> tuple[float, tuple[float, float]]:
    """Height of the plug tip above the port and its XY offset from it, in millimetres, from
    ground-truth TF in the base frame (CheatCode compares the same base-frame coordinates,
    CheatCode.py:133-141). Negative height: the tip is inside the port. Assumes the port's
    insertion axis is the base frame's -z, as on the sample task board."""
    dx, dy, dz = (a - b for a, b in zip(plug.position, port.position, strict=True))
    return mm(dz), (mm(dx), mm(dy))


def magnitude(v: Vec3) -> float:
    return math.hypot(*v)


def tared_magnitude(raw: Vec3, tare: Vec3) -> float:
    """Subtract the tare per axis, then take the magnitude, as the AIC scorer does for force
    (ScoringTier2.cc:419-430); torque is tared the same way."""
    return math.hypot(*(r - t for r, t in zip(raw, tare, strict=True)))
