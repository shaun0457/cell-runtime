"""Fault effects in the mock world that the Monitor tests cannot see in a single attempt."""

from __future__ import annotations

import random

from cellrt.world.base import HOLD, Velocity
from cellrt.world.faults import PoseShift
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
