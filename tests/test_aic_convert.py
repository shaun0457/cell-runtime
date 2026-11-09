"""Pure conversions in the AIC adapter, tested without ROS: units, geometry from ground-truth TF,
core commands to motion-update fields, and CheatCode's pose computation."""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from adapters.aic.convert import (
    POSITION_DAMPING,
    POSITION_STIFFNESS,
    VELOCITY_DAMPING,
    VELOCITY_STIFFNESS,
    WRENCH_FEEDBACK_GAINS,
    Pose,
    core_task,
    diag6,
    motion_command,
    plug_to_port_mm,
    tared_magnitude,
    to_pose_target,
)
from adapters.aic.geometry import (
    MAX_INTEGRATOR_WINDUP,
    XYIntegrator,
    gripper_target,
    quat_multiply,
    quat_slerp,
)
from cellrt.runtime.task import Task
from cellrt.world.base import HOLD, PoseTarget, Velocity

S = math.sqrt(0.5)
Z90 = (S, 0.0, 0.0, S)  # 90 degrees about z, (w, x, y, z)
X180 = (0.0, 1.0, 0.0, 0.0)


# --- units and geometry from TF --------------------------------------------------------------


def test_a_base_frame_pose_becomes_a_pose_target_in_millimetres() -> None:
    target = to_pose_target(Pose(position=(0.5, -0.25, 0.125), orientation=X180))

    assert target == PoseTarget(x_mm=500.0, y_mm=-250.0, z_mm=125.0, qw=0.0, qx=1.0)


def test_an_aic_task_message_maps_to_the_core_task_field_by_field() -> None:
    msg = SimpleNamespace(
        id="trial_1_task_1",
        cable_type="sfp_sc",
        cable_name="cable_1",
        plug_type="sfp",
        plug_name="sfp_tip",
        port_type="sfp",
        port_name="sfp_port_0",
        target_module_name="nic_card_mount_0",
        time_limit=180,  # uint64 seconds in the message
    )

    task = core_task(msg)

    assert task == Task(
        id="trial_1_task_1",
        cable_type="sfp_sc",
        cable_name="cable_1",
        plug_type="sfp",
        plug_name="sfp_tip",
        port_type="sfp",
        port_name="sfp_port_0",
        target_module_name="nic_card_mount_0",
        time_limit=180.0,
    )


def test_height_and_xy_error_are_the_plug_minus_the_port_in_millimetres() -> None:
    port = Pose(position=(0.5, 0.1, 0.05))
    plug = Pose(position=(0.5012, 0.0995, 0.062))

    height, (dx, dy) = plug_to_port_mm(port, plug)

    assert height == pytest.approx(12.0)
    assert (dx, dy) == (pytest.approx(1.2), pytest.approx(-0.5))
    assert plug_to_port_mm(port, Pose(position=(0.5, 0.1, 0.04)))[0] == pytest.approx(-10.0)


def test_force_is_tared_per_axis_before_the_magnitude() -> None:
    # Side contact: magnitudes alone (5 - 5 = 0) would hide 6 N.
    assert tared_magnitude((3.0, 4.0, 0.0), (-3.0, 4.0, 0.0)) == pytest.approx(6.0)


def test_diag6_is_a_row_major_6x6_matrix() -> None:
    m = diag6((1.0, 2.0, 3.0, 4.0, 5.0, 6.0))

    assert len(m) == 36
    assert [m[i * 6 + i] for i in range(6)] == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    assert sum(m) == 21.0
    with pytest.raises(ValueError):
        diag6((1.0,))


# --- commands to motion updates --------------------------------------------------------------


def test_a_pose_target_becomes_a_position_mode_update_with_cheatcode_gains() -> None:
    m = motion_command(PoseTarget(x_mm=500.0, y_mm=100.0, z_mm=250.0, qw=0.0, qx=1.0), None)

    assert m.mode == "position" and m.frame_id == "base_link"
    assert m.position_m == (0.5, 0.1, 0.25)
    assert m.orientation == (0.0, 1.0, 0.0, 0.0)
    assert m.stiffness == diag6((90.0, 90.0, 90.0, 50.0, 50.0, 50.0)) == diag6(POSITION_STIFFNESS)
    assert m.damping == diag6((50.0, 50.0, 50.0, 20.0, 20.0, 20.0)) == diag6(POSITION_DAMPING)
    assert m.wrench_feedback_gains == (0.5, 0.5, 0.5, 0.0, 0.0, 0.0) == WRENCH_FEEDBACK_GAINS


def test_a_velocity_becomes_a_velocity_mode_twist_in_si_units() -> None:
    m = motion_command(Velocity(vx=10.0, vy=-5.0, vz=-2.0, wz=90.0), None)

    assert m.mode == "velocity"
    assert m.linear_m_s == (0.01, -0.005, -0.002)
    assert m.angular_rad_s == (0.0, 0.0, pytest.approx(math.pi / 2))
    assert (
        m.stiffness == diag6((100.0, 100.0, 100.0, 50.0, 50.0, 50.0)) == diag6(VELOCITY_STIFFNESS)
    )
    assert m.damping == diag6((40.0, 40.0, 40.0, 15.0, 15.0, 15.0)) == diag6(VELOCITY_DAMPING)


def test_hold_is_a_position_target_at_the_measured_tcp() -> None:
    tcp = Pose(position=(0.4, 0.0, 0.3), orientation=X180)

    m = motion_command(HOLD, tcp)

    assert m.mode == "position"
    assert (m.position_m, m.orientation) == ((0.4, 0.0, 0.3), X180)
    assert m.linear_m_s == m.angular_rad_s == (0.0, 0.0, 0.0)


def test_hold_before_any_measured_pose_is_a_zero_twist() -> None:
    m = motion_command(HOLD, None)

    assert m.mode == "velocity" and m.linear_m_s == m.angular_rad_s == (0.0, 0.0, 0.0)


def test_something_else_is_not_a_command() -> None:
    with pytest.raises(TypeError):
        motion_command("stop", None)  # type: ignore[arg-type]


# --- quaternions (transforms3d's Gohlke conventions, w first) ---------------------------------


def test_quaternion_multiply_composes_rotations() -> None:
    assert quat_multiply(Z90, Z90) == pytest.approx((0.0, 0.0, 0.0, 1.0))  # 180 about z
    assert quat_multiply((1.0, 0.0, 0.0, 0.0), X180) == X180
    # i * j = k (Hamilton)
    assert quat_multiply((0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0)) == (0.0, 0.0, 0.0, 1.0)


def test_slerp_takes_the_shorter_arc_and_its_endpoints() -> None:
    identity = (1.0, 0.0, 0.0, 0.0)
    z180 = (0.0, 0.0, 0.0, 1.0)

    assert quat_slerp(identity, z180, 0.5) == pytest.approx(Z90)
    assert quat_slerp(identity, Z90, 0.0) == identity
    assert quat_slerp(identity, Z90, 1.0) == pytest.approx(Z90)
    # -Z90 is the same rotation: the short way is still 45 degrees at the midpoint.
    neg = tuple(-c for c in Z90)
    half = quat_slerp(identity, neg, 0.5)  # type: ignore[arg-type]
    assert half == pytest.approx((math.cos(math.pi / 8), 0.0, 0.0, math.sin(math.pi / 8)))


def test_slerp_rejects_a_zero_quaternion() -> None:
    with pytest.raises(ValueError):
        quat_slerp((0.0, 0.0, 0.0, 0.0), Z90, 0.5)


def _same_rotation(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    """q and -q are the same rotation."""
    return abs(abs(sum(x * y for x, y in zip(a, b, strict=True))) - 1.0) < 1e-9


# --- CheatCode's gripper pose ----------------------------------------------------------------


def test_the_target_follows_cheatcodes_formula() -> None:
    port = Pose(position=(0.5, 0.1, 0.05))
    gripper = Pose(position=(0.3, -0.1, 0.45))
    plug = Pose(position=(0.31, -0.1, 0.41))  # 4 cm below the TCP, 1 cm off in x

    target, _ = gripper_target(port, plug, gripper, z_offset_m=0.2)

    # CheatCode.py:165: target_z = port_z + z_offset - (gripper_z - plug_z) = 0.05 + 0.2 - 0.04.
    # The TCP-to-tip offset is subtracted, not added, so with the plug below the TCP the tip ends
    # 2 x 0.04 short of z_offset. Ported as is (CheatCode passes the sample tasks); to be checked
    # against the sim in #28. XY goes to the port.
    assert target.position == pytest.approx((0.5, 0.1, 0.21))
    assert _same_rotation(target.orientation, (1.0, 0.0, 0.0, 0.0))  # plug and port aligned


def test_fractions_blend_from_the_current_gripper_pose() -> None:
    port = Pose(position=(0.5, 0.1, 0.05))
    gripper = Pose(position=(0.3, -0.1, 0.45))
    plug = Pose(position=(0.3, -0.1, 0.41))

    target, _ = gripper_target(port, plug, gripper, z_offset_m=0.2, position_fraction=0.25)

    # A quarter of the way from (0.3, -0.1, 0.45) to CheatCode's target (0.5, 0.1, 0.21).
    assert target.position == pytest.approx((0.35, -0.05, 0.39))


def test_the_target_turns_the_gripper_by_the_plug_to_port_rotation() -> None:
    # The port is turned 90 degrees about z relative to the plug: the gripper turns with it.
    port = Pose(position=(0.5, 0.1, 0.05), orientation=Z90)
    plug = Pose(position=(0.5, 0.1, 0.30))
    gripper = Pose(position=(0.5, 0.1, 0.34))

    full, _ = gripper_target(port, plug, gripper, z_offset_m=0.2)
    half, _ = gripper_target(port, plug, gripper, z_offset_m=0.2, slerp_fraction=0.5)

    assert _same_rotation(full.orientation, Z90)  # CheatCode's negated conjugate: -q is q
    expected_half = (math.cos(math.pi / 8), 0.0, 0.0, math.sin(math.pi / 8))
    assert half.orientation == pytest.approx(expected_half)


def test_the_plug_to_port_rotation_is_applied_after_the_grippers_own() -> None:
    # A gripper pointing down (X180) and a port turned 90 degrees about z: the target is
    # q_diff * q_gripper, here Z90 * X180, which differs from the other order (X180 * Z90).
    port = Pose(position=(0.5, 0.1, 0.05), orientation=Z90)
    plug = Pose(position=(0.5, 0.1, 0.30))
    gripper = Pose(position=(0.5, 0.1, 0.34), orientation=X180)

    target, _ = gripper_target(port, plug, gripper, z_offset_m=0.2)

    assert _same_rotation(target.orientation, quat_multiply(Z90, X180))
    assert not _same_rotation(target.orientation, quat_multiply(X180, Z90))


def test_the_xy_integrator_accumulates_and_is_clipped_at_the_windup_limit() -> None:
    port = Pose(position=(0.5, 0.1, 0.05))
    gripper = Pose(position=(0.5, 0.1, 0.30))
    plug = Pose(position=(0.49, 0.1, 0.26))  # tip 1 cm short of the port in x

    _, reset = gripper_target(port, plug, gripper, z_offset_m=0.0)
    assert reset == XYIntegrator()
    acc = XYIntegrator()
    for _ in range(3):
        target, acc = gripper_target(port, plug, gripper, z_offset_m=0.0, integrator=acc)
    assert acc.x == pytest.approx(0.03)
    assert target.position[0] == pytest.approx(0.5 + 0.15 * 0.03)  # CheatCode's i_gain 0.15
    for _ in range(10):
        _, acc = gripper_target(port, plug, gripper, z_offset_m=0.0, integrator=acc)
    assert acc.x == MAX_INTEGRATOR_WINDUP == 0.05
