"""The AIC cell as a core `World`, over a `NodePort`.

Time (ADR 011): a step blocks the policy thread until the node's sim time has moved on by the
period, and reports the sim time that passed; the core clock advances by that. A step that
cannot finish within `stall_wall_s` of real time stops the run as `sim_clock_stalled` (the sim
is paused, or runs so slowly it is as good as paused); sim time going backwards (a Gazebo reset)
stops it as `sim_time_reset`. Sensors and poses are read once per step, at its end, so the
Monitor and the log see one consistent sample.

Force (ADR 013): at each step end the world drains every wrench message since the last one. The
newest finite reading is the sample's force; the scorer's rule over the drained readings,
continued from the last reading of the step before, is its `force_over_limit_s`.

Stops (ADR 012): the goal's state is checked when the world is built and on every poll while a
step waits, so a cancel stops the run within one step (`goal_cancel_requested`, `goal_aborted`,
`node_deactivated`). A node call that raises during a step stops it as `node_error`; its text is
dropped. All of these raise `WorldStopped`, which the runner turns into its one stop.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from adapters.aic.convert import Pose, motion_command, plug_to_port_mm
from adapters.aic.force import TaredWrench, time_over_limit
from adapters.aic.node import NodePort
from cellrt.executor.runner import STEP_TOLERANCE_S
from cellrt.runtime.task import Task
from cellrt.world.base import HOLD, Command, Signals, WorldStopped

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
        self._hold_failed = False
        self._last_reading: TaredWrench | None = None  # for the pair across a step boundary
        self._newest: TaredWrench | None = None  # the newest finite reading
        try:
            self._check_goal()  # fail fast, before reading anything else
            self._sim_s = node.sim_now_s()
            self._geometry = self._read_geometry()
            self._signals = self._read_signals()
        except WorldStopped:
            raise
        except Exception:
            raise WorldStopped("node_error") from None

    # World --------------------------------------------------------------------------------------

    def signals(self) -> Signals:
        return self._signals

    def command(self, cmd: Command) -> None:
        try:
            state = self._node.controller_state()
            self._node.send(motion_command(cmd, None if state is None else state.tcp))
        except Exception:
            if cmd is not HOLD:  # the executor's HOLD; a skill's own zero twist still raises
                raise  # the skill raises, and the run aborts with the exception's type
            # HOLD is sent by the executor after a skill ends; nobody can be told here, so the
            # next step stops the run instead.
            self._hold_failed = True

    def step(self, dt: float) -> float:
        if self._hold_failed:
            raise WorldStopped("node_error")
        return self._wait(dt, check_goal=True)

    # for the adapter ----------------------------------------------------------------------------

    def settle(self, dt: float) -> float:
        """A step for the stop confirmation: it ignores the goal's state (the check runs after
        a cancel too) and reads no TF or wrench, so a run stopped for missing ground truth can
        still be confirmed. Bounded by `stall_wall_s` like a step."""
        return self._wait(dt, check_goal=False, read=False)

    def tcp_speed_mm_s(self) -> float | None:
        """The measured linear TCP speed, or None without a finite reading."""
        try:
            state = self._node.controller_state()
        except Exception:
            return None
        if state is None or not math.isfinite(state.tcp_speed_mm_s):
            return None
        return state.tcp_speed_mm_s

    def _wait(self, dt: float, *, check_goal: bool, read: bool = True) -> float:
        try:
            return self._wait_for_sim_time(dt, check_goal, read)
        except WorldStopped:
            raise
        except Exception:
            raise WorldStopped("node_error") from None  # upstream text stays out of the log

    def _wait_for_sim_time(self, dt: float, check_goal: bool, read: bool) -> float:
        node = self._node
        start = last = self._sim_s
        waited_from = node.wall_now_s()
        while True:  # bounded by stall_wall_s of real time
            if check_goal:
                self._check_goal()
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
        if read:
            self._geometry = self._read_geometry()
            self._signals = self._read_signals()
        return now - start

    def _check_goal(self) -> None:
        reason = self._node.goal_stop_reason()
        if reason is not None:
            raise WorldStopped(reason)

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
        drained = self._node.drain_wrenches()
        # After a drop the readings before the first kept one are gone: pairing it with the
        # previous step's last reading would count the whole gap as time over the limit.
        previous = self._last_reading if drained.dropped == 0 else None
        over_s = time_over_limit(previous, drained.readings)
        if drained.readings:
            self._last_reading = drained.readings[-1]
        # A reading with a NaN or an infinity never becomes the sample (the log rejects it);
        # it still counts in the scorer's sum above, as it does in the scorer.
        self._newest = next((r for r in reversed(drained.readings) if r.finite), self._newest)
        newest = self._newest
        valid = newest is not None and abs(self._sim_s - newest.stamp_s) <= WRENCH_FRESH_S + 1e-9
        zero = (0.0, 0.0, 0.0)
        return Signals(
            height_mm=round(height_mm, 6),
            xy_offset_mm=(round(dx, 6), round(dy, 6)),
            raw_force_n=newest.raw_force if newest is not None else zero,
            tare_offset_n=newest.tare_force if newest is not None else zero,
            torque_nm=round(newest.torque_nm, 6) if valid and newest is not None else 0.0,
            wrench_valid=valid,
            force_over_limit_s=round(over_s, 9),
            wrench_dropped=drained.dropped,
        )
