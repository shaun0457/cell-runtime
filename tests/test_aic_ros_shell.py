"""The thin ROS shell around the policy body. Runs only where ROS 2 and aic_model are importable
(the AIC VM); skipped everywhere else, including CI."""

from __future__ import annotations

import pytest

pytest.importorskip("rclpy")
pytest.importorskip("aic_model")


def test_runtime_policy_is_an_aic_policy_loadable_by_its_module_name() -> None:
    import importlib

    from aic_model.policy import Policy

    module = importlib.import_module("adapters.aic.RuntimePolicy")
    # aic_model loads `policy:=adapters.aic.RuntimePolicy`: the class has the module's last name.
    assert issubclass(module.RuntimePolicy, Policy)


def test_the_ros_port_maps_a_motion_command_to_a_motion_update() -> None:
    from aic_control_interfaces.msg import TrajectoryGenerationMode

    from adapters.aic.convert import motion_command
    from adapters.aic.ros_port import to_motion_update
    from cellrt.world.base import PoseTarget

    cmd = motion_command(PoseTarget(x_mm=500.0, y_mm=100.0, z_mm=250.0), None)
    msg = to_motion_update(cmd, stamp=None)

    assert msg.header.frame_id == "base_link"
    assert (msg.pose.position.x, msg.pose.position.y, msg.pose.position.z) == (0.5, 0.1, 0.25)
    assert msg.trajectory_generation_mode.mode == TrajectoryGenerationMode.MODE_POSITION
    assert list(msg.target_stiffness) == list(cmd.stiffness)
