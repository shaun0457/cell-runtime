"""The adapter's test seam: everything the policy body needs from the `aic_model` node.

`ros_port.RosNodePort` implements it over rclpy on the AIC machine; tests use a fake. The
policy body (`run.run_goal`) sees only this protocol, so it runs on any OS.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from adapters.aic.convert import MotionCommand, Pose, Vec3

if TYPE_CHECKING:
    from adapters.aic.force import WrenchDrain


@dataclass(frozen=True)
class WrenchReading:
    """One `/fts_broadcaster/wrench` message: raw (not tared), in the sensor frame."""

    stamp_s: float  # sim time of the reading
    force: Vec3
    torque: Vec3


@dataclass(frozen=True)
class ControllerSnapshot:
    """The parts of the latest `/aic_controller/controller_state` the adapter uses."""

    tcp: Pose  # `tcp_pose`, the measured TCP in the base frame
    tare_force: Vec3  # `fts_tare_offset.wrench.force`, same frame as the raw reading
    tare_torque: Vec3
    tcp_speed_mm_s: float  # |`tcp_velocity.linear`|, to confirm the arm stopped


class NodePort(Protocol):
    def sim_now_s(self) -> float:
        """The node's clock (sim time with `use_sim_time`), in seconds."""
        ...

    def wall_now_s(self) -> float:
        """Real time, for the watchdog only; never recorded as clock time."""
        ...

    def wall_sleep(self, seconds: float) -> None:
        """Block the policy thread for `seconds` of real time while sim time moves on."""
        ...

    def goal_stop_reason(self) -> str | None:
        """Why the goal must stop now, as a code, or None while it may go on:
        `goal_cancel_requested`, `goal_aborted` (cancel_task), `node_deactivated`,
        `aic_goal_handle_unavailable` when the goal handle could not be read at the start, or
        `goal_handle_unreadable` when it could not be read later."""
        ...

    def transform(self, frame: str) -> Pose | None:
        """`frame` in the base frame from TF, or None while it is not available."""
        ...

    def drain_wrenches(self) -> WrenchDrain:
        """Every wrench message since the last drain, tared with the tare newest at its arrival
        (`force.WrenchFeed`), oldest first, and how many the bounded buffer dropped."""
        ...

    def controller_state(self) -> ControllerSnapshot | None: ...

    def send(self, motion: MotionCommand) -> None:
        """Hand a motion update to `move_robot`."""
        ...

    def send_feedback(self, message: str) -> None:
        """Send `InsertCable` feedback (aic_model's `send_feedback`)."""
        ...
