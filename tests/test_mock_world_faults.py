"""Fault effects in the mock world that the Monitor tests cannot see in a single attempt."""

from __future__ import annotations

import random

from cellrt.world.base import HOLD, Velocity
from cellrt.world.faults import DamagedPort, PoseShift, YawMismatch
from cellrt.world.mock_world import MockParams, MockWorld


def test_a_pose_shift_moves_the_port_on_its_first_attempts_only() -> None:
    shift = PoseShift(
        "pose_shift#0", trigger_height_mm=2.0, shift_mm=3.0, angle_rad=0.0, attempts=2
    )
    params = MockParams(start_height_mm=3.0, initial_xy_range_mm=0.0, xy_noise_mm=0.0)
    world = MockWorld(random.Random(0), params, [shift])

    # Three descents past 2 mm, rising back above it in between.
    for vz, steps in ((-10.0, 2), (10.0, 4), (-10.0, 4), (10.0, 4), (-10.0, 4)):
        world.command(Velocity(vz=vz))
        for _ in range(steps):
            world.step(params.dt)
    world.command(HOLD)

    assert world.signals().xy_offset_mm == (6.0, 0.0)
    assert [t.active for t in world.take_fault_transitions()] == [True, False]


def test_a_yaw_mismatch_wedges_the_plug_until_it_is_twisted_into_line() -> None:
    # Port rotated -10°: the torque must say "twist negative"; twisting the wrong way keeps the
    # plug wedged, twisting the right way frees it.
    yaw = YawMismatch("yaw#0", stall_depth_mm=2.0, force_n=7.0, torque_nm=0.5, yaw_deg=-10.0)
    params = MockParams(start_height_mm=0.5, initial_xy_range_mm=0.0, xy_noise_mm=0.0)
    world = MockWorld(random.Random(0), params, [yaw])

    def run(cmd: Velocity, steps: int) -> None:
        world.command(cmd)
        for _ in range(steps):
            world.step(params.dt)

    run(Velocity(vz=-10.0), 8)
    assert world.signals().height_mm == -2.0
    assert world.signals().torque_nm == -0.5

    run(Velocity(vz=-10.0, wz=30.0), 10)  # wrong way: now 25° off
    assert world.signals().height_mm == -2.0
    assert world.signals().torque_nm == -0.5

    run(Velocity(wz=-30.0), 17)  # back to 0.5° off: within the 1° tolerance
    run(Velocity(vz=-10.0), 4)
    assert world.signals().height_mm < -2.0
    assert world.signals().torque_nm == 0.0
    assert [t.active for t in world.take_fault_transitions()] == [True, False]


def test_a_damaged_port_stops_the_plug_on_every_attempt_whatever_the_skill_does() -> None:
    # Like a yaw wedge, but force and torque stay low and nothing frees the plug: not twisting
    # either way, not pulling out and pushing in again.
    port = DamagedPort("damaged_port#0", stall_depth_mm=1.0, force_n=3.0, torque_nm=0.04)
    params = MockParams(
        start_height_mm=0.5, initial_xy_range_mm=0.0, xy_noise_mm=0.0, force_noise_n=0.0
    )
    world = MockWorld(random.Random(0), params, [port])

    def run(cmd: Velocity, steps: int) -> None:
        world.command(cmd)
        for _ in range(steps):
            world.step(params.dt)

    run(Velocity(vz=-10.0), 8)
    stalled = world.signals()
    assert (stalled.height_mm, stalled.force_n, stalled.torque_nm) == (-1.0, 3.0, 0.04)

    run(Velocity(vz=-10.0, wz=30.0), 20)  # twisting one way
    assert world.signals().height_mm == -1.0
    run(Velocity(vz=-10.0, wz=-30.0), 40)  # and the other
    assert world.signals().height_mm == -1.0

    run(Velocity(vz=10.0), 4)  # out of the port, then a new insertion
    assert world.signals().height_mm > 0
    run(Velocity(vz=-2.0), 40)
    assert (world.signals().height_mm, world.signals().torque_nm) == (-1.0, 0.04)
    assert [t.active for t in world.take_fault_transitions()] == [True]  # never cleared
