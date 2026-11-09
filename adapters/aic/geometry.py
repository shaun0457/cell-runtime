# Third-party code, modified for cell-runtime (rewritten as pure functions on tuples, without
# numpy, ROS or transforms3d). Notices and full license texts: THIRD_PARTY_NOTICES.md.
#
# gripper_target, XYIntegrator, I_GAIN, MAX_INTEGRATOR_WINDUP: adapted from CheatCode's
# calc_gripper_pose in AIC (https://github.com/intrinsic-dev/aic),
# aic_example_policies/aic_example_policies/ros/CheatCode.py.
#   Copyright (C) 2026 Intrinsic Innovation LLC
#   Licensed under the Apache License, Version 2.0 (see LICENSE).
#
# quat_multiply, quat_slerp, _EPS: adapted from quaternion_multiply, quaternion_slerp and _EPS in
# transforms3d (https://github.com/matthew-brett/transforms3d), transforms3d/_gohlketransforms.py.
#   Copyright (c) 2006-2017, Christoph Gohlke
#   Copyright (c) 2006-2017, The Regents of the University of California
#   BSD license (conditions and disclaimer in THIRD_PARTY_NOTICES.md). Redistributing this file
#   alone requires keeping this header and that BSD text with it.

"""CheatCode's pose computation as pure functions, without numpy or transforms3d.

Ported from `aic_example_policies/ros/CheatCode.py:72-186` @ `3ab0653` (`calc_gripper_pose`)
and transforms3d's `quaternion_multiply` / `quaternion_slerp` (Gohlke). Quaternions are
(w, x, y, z). Positions are metres in the base frame.

Ported as is, including one quirk: CheatCode subtracts the TCP-to-tip offset when it places the
TCP (`target_z = port_z + z_offset - (gripper_z - plug_z)`, CheatCode.py:165), so the tip ends
twice that offset away from `z_offset`. CheatCode passes the sample tasks this way; check it
against the sim (#28) before changing it.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass

from adapters.aic.convert import Pose, Quat

# CheatCode's XY integral term (CheatCode.py:41, :161).
I_GAIN = 0.15
MAX_INTEGRATOR_WINDUP = 0.05
# transforms3d's tolerance for slerp's special cases (`_EPS = numpy.finfo(float).eps * 4.0`).
_EPS = sys.float_info.epsilon * 4.0


def quat_multiply(q1: Quat, q0: Quat) -> Quat:
    """The Hamilton product q1 * q0 (apply q0, then q1)."""
    w1, x1, y1, z1 = q1
    w0, x0, y0, z0 = q0
    return (
        -x1 * x0 - y1 * y0 - z1 * z0 + w1 * w0,
        x1 * w0 + y1 * z0 - z1 * y0 + w1 * x0,
        -x1 * z0 + y1 * w0 + z1 * x0 + w1 * y0,
        x1 * y0 - y1 * x0 + z1 * w0 + w1 * z0,
    )


def quat_slerp(q0: Quat, q1: Quat, fraction: float) -> Quat:
    """Spherical interpolation from q0 to q1 along the shorter arc (transforms3d's default)."""
    a, b = _unit(q0), _unit(q1)
    if fraction == 0.0:
        return a
    if fraction == 1.0:
        return b
    d = sum(x * y for x, y in zip(a, b, strict=True))
    if abs(abs(d) - 1.0) < _EPS:
        return a
    if d < 0.0:
        d = -d
        b = (-b[0], -b[1], -b[2], -b[3])
    angle = math.acos(max(-1.0, min(1.0, d)))
    if abs(angle) < _EPS:
        return a
    s = math.sin(angle)
    wa, wb = math.sin((1.0 - fraction) * angle) / s, math.sin(fraction * angle) / s
    return (
        wa * a[0] + wb * b[0],
        wa * a[1] + wb * b[1],
        wa * a[2] + wb * b[2],
        wa * a[3] + wb * b[3],
    )


def _unit(q: Quat) -> Quat:
    n = math.sqrt(sum(c * c for c in q))
    if n == 0.0 or not math.isfinite(n):
        raise ValueError("not a rotation: zero or non-finite quaternion")
    return (q[0] / n, q[1] / n, q[2] / n, q[3] / n)


@dataclass(frozen=True)
class XYIntegrator:
    """CheatCode's accumulated XY tip error (metres), clipped to the windup limit."""

    x: float = 0.0
    y: float = 0.0

    def add(self, ex: float, ey: float) -> XYIntegrator:
        clip = MAX_INTEGRATOR_WINDUP
        return XYIntegrator(
            x=max(-clip, min(clip, self.x + ex)), y=max(-clip, min(clip, self.y + ey))
        )


def gripper_target(
    port: Pose,
    plug: Pose,
    gripper: Pose,
    *,
    z_offset_m: float,
    slerp_fraction: float = 1.0,
    position_fraction: float = 1.0,
    integrator: XYIntegrator | None = None,
) -> tuple[Pose, XYIntegrator]:
    """The gripper (TCP) pose that puts the plug tip `z_offset_m` above the port, aligned with
    it, and the updated integrator; `integrator=None` resets it (CheatCode's
    `reset_xy_integrator=True`). Fractions blend from the current gripper pose towards the
    target, as CheatCode's approach interpolation does."""
    # CheatCode builds the plug inverse as (-w, x, y, z): the negated conjugate, the same
    # rotation (CheatCode.py:98-103).
    pw, px_, py_, pz_ = plug.orientation
    q_diff = quat_multiply(port.orientation, (-pw, px_, py_, pz_))
    q_target = quat_multiply(q_diff, gripper.orientation)
    q_slerp = quat_slerp(gripper.orientation, q_target, slerp_fraction)

    gx, gy, gz = gripper.position
    px, py, pz = plug.position
    tip_offset_z = gz - pz
    ex, ey = port.position[0] - px, port.position[1] - py
    acc = XYIntegrator() if integrator is None else integrator.add(ex, ey)

    target = (
        port.position[0] + I_GAIN * acc.x,
        port.position[1] + I_GAIN * acc.y,
        port.position[2] + z_offset_m - tip_offset_z,
    )
    f = position_fraction
    blend = (
        f * target[0] + (1 - f) * gx,
        f * target[1] + (1 - f) * gy,
        f * target[2] + (1 - f) * gz,
    )
    return Pose(position=blend, orientation=q_slerp), acc
