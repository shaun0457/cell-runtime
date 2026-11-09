"""The AIC cell as a core `World`, over a `NodePort`.

Time (ADR 011): a step blocks the policy thread until the node's sim time has moved on by the
period, and reports the sim time that passed; the core clock advances by that. A step that
cannot finish within `stall_wall_s` of real time stops the run as `sim_clock_stalled` (the sim
is paused, or runs so slowly it is as good as paused); sim time going backwards (a Gazebo reset)
stops it as `sim_time_reset`. Sensors and poses are read once per step, at its end, so the
Monitor and the log see one consistent sample.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from adapters.aic.convert import Pose, motion_command, plug_to_port_mm, tared_magnitude
from adapters.aic.node import NodePort
from cellrt.executor.runner import STEP_TOLERANCE_S
from cellrt.runtime.task import Task
from cellrt.world.base import Command, Signals, WorldStopped

# A 50 ms step that takes longer than this in real time means a real-time factor below 0.005.
SIM_STALL_WALL_S = 10.0
POLL_WALL_S = 0.002
# The newest wrench must be at most this old (sim time) to count as valid; F/T runs at 50 Hz.
WRENCH_FRESH_S = 0.1
TCP_FRAME = "gripper/tcp"


@dataclass(frozen=True)
class Frames:
    """Ground-truth TF frames for a task (CheatCode.py:197-198)."""

    port: str
    plug: str
    gripper: str = TCP_FRAME

    @classmethod
    def for_task(cls, task: Task) -> Frames:
        return cls(
            port=f"task_board/{task.target_module_name}/{task.port_name}_link",
            plug=f"{task.cable_name}/{task.plug_name}_link",
        )


@dataclass(frozen=True)
class Geometry:
    """Port, plug tip and gripper TCP in the base frame, from ground truth."""

    port: Pose
    plug: Pose
    gripper: Pose


class AicWorld:
    """Raises `WorldStopped("ground_truth_unavailable")` when built or stepped without the
    ground-truth frames (launch the sim with `ground_truth:=true`)."""

    def __init__(
        self,
        node: NodePort,
        task: Task,
        *,
        stall_wall_s: float = SIM_STALL_WALL_S,
        poll_wall_s: float = POLL_WALL_S,
    ) -> None:
        if not (math.isfinite(stall_wall_s) and stall_wall_s > 0):
            raise ValueError("stall_wall_s must be a positive number of seconds")
        if not (math.isfinite(poll_wall_s) and 0 < poll_wall_s <= stall_wall_s):
            raise ValueError("poll_wall_s must be positive and at most stall_wall_s")
        self._node = node
        self._frames = Frames.for_task(task)
        self._stall_wall_s = stall_wall_s
        self._poll_wall_s = poll_wall_s
        self._sim_s = node.sim_now_s()
        self._geometry = self._read_geometry()
        self._signals = self._read_signals()

    # World --------------------------------------------------------------------------------------

    def signals(self) -> Signals:
        return self._signals

    def command(self, cmd: Command) -> None:
        state = self._node.controller_state()
        self._node.send(motion_command(cmd, None if state is None else state.tcp))

    def step(self, dt: float) -> float:
        node = self._node
        start = last = self._sim_s
        waited_from = node.wall_now_s()
        while True:  # bounded by stall_wall_s of real time
            now = node.sim_now_s()
            if now < last:
                raise WorldStopped("sim_time_reset")
            last = now
            if now - start >= dt - STEP_TOLERANCE_S:
                break
            if node.wall_now_s() - waited_from >= self._stall_wall_s:
                raise WorldStopped("sim_clock_stalled")
            node.wall_sleep(self._poll_wall_s)
        self._sim_s = now
        self._geometry = self._read_geometry()
        self._signals = self._read_signals()
        return now - start

    # for AIC skills -----------------------------------------------------------------------------

    def geometry(self) -> Geometry:
        return self._geometry

    # reading ------------------------------------------------------------------------------------

    def _read_geometry(self) -> Geometry:
        poses = [self._node.transform(f) for f in (self._frames.port, self._frames.plug)]
        gripper = self._node.transform(self._frames.gripper)
        port, plug = poses
        if port is None or plug is None or gripper is None:
            raise WorldStopped("ground_truth_unavailable")
        return Geometry(port=port, plug=plug, gripper=gripper)

    def _read_signals(self) -> Signals:
        g = self._geometry
        height_mm, (dx, dy) = plug_to_port_mm(g.port, g.plug)
        wrench = self._node.latest_wrench()
        state = self._node.controller_state()
        zero = (0.0, 0.0, 0.0)
        valid = False
        torque = 0.0
        if wrench is not None and state is not None:
            valid = abs(self._sim_s - wrench.stamp_s) <= WRENCH_FRESH_S
            torque = tared_magnitude(wrench.torque, state.tare_torque) if valid else 0.0
        return Signals(
            height_mm=round(height_mm, 6),
            xy_offset_mm=(round(dx, 6), round(dy, 6)),
            raw_force_n=wrench.force if wrench is not None else zero,
            tare_offset_n=state.tare_force if state is not None else zero,
            torque_nm=round(torque, 6),
            wrench_valid=valid,
        )
