"""A fake aic_model node behind the adapter's `NodePort`, for tests on any OS.

Sim time moves only when the policy thread sleeps (wall time), by `sim_per_sleep_s` per sleep;
a test sets it to 0 to stall the sim, or schedules a jump back with `reset_after_sleeps`. The
arm tracks every position target exactly, and the plug hangs `PLUG_BELOW_TCP_M` under the TCP.
Goal events (a cancel, an abort, deactivation) and failures are scripted by sleep count.
`tcp_at_send` keeps the TCP measured just before each motion update, so a test can tell HOLD
(a target at the measured pose) from a skill's last target.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from adapters.aic.convert import MotionCommand, Pose
from adapters.aic.node import ControllerSnapshot, WrenchReading
from cellrt.runtime.task import Task

PLUG_BELOW_TCP_M = 0.04
SECRET = "upstream text with a key: sk-123"  # must never reach the store
PORT = Pose(position=(0.5, 0.1, 0.05))
START_TCP = Pose(position=(0.3, -0.1, 0.45))
TARE_N = (0.0, 0.0, 1.0)
RAW_N = (0.0, 0.0, 1.5)  # 0.5 N of contact force after the tare


@dataclass
class FakeNode:
    task: Task = field(default_factory=Task)
    sim_s: float = 100.0  # the node's sim time when the goal arrives; never 0 on purpose
    wall_s: float = 0.0
    sim_per_sleep_s: float = 0.025
    reset_after_sleeps: int | None = None
    port: Pose = PORT
    tcp: Pose = START_TCP
    hide_frames: frozenset[str] = frozenset()
    wrench_age_s: float = 0.0  # how old the newest wrench is, in sim time
    has_controller_state: bool = True
    raw_force: tuple[float, float, float] = RAW_N
    # (reason code, sleeps): the goal is cancelled / aborted / the node deactivated from then on
    goal_stop: tuple[str, int] | None = None
    goal_handle_available: bool = True
    stop_sim_s: float | None = None  # sim time at which the scripted goal stop became visible
    tcp_speed_mm_s: float = 0.0
    slow_after_sleeps: int | None = None  # from then on the TCP speed reads 0
    fail_send_at: int | None = None  # the n-th send (1-based) raises, once
    fail_transform_after_sleeps: int | None = None
    hide_frames_after_sleeps: int | None = None  # from then on no ground-truth frame is found
    stall_after_sleeps: int | None = None  # from then on sim time stops
    sends: int = 0
    sent: list[MotionCommand] = field(default_factory=list)
    tcp_at_send: list[Pose] = field(default_factory=list)
    sleeps: int = 0

    # NodePort ---------------------------------------------------------------------------------

    def sim_now_s(self) -> float:
        return self.sim_s

    def wall_now_s(self) -> float:
        return self.wall_s

    def wall_sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.wall_s += seconds
        if self.reset_after_sleeps is not None and self.sleeps >= self.reset_after_sleeps:
            self.sim_s = 0.0  # Gazebo reset
            self.reset_after_sleeps = None
        elif self.stall_after_sleeps is None or self.sleeps < self.stall_after_sleeps:
            self.sim_s = round(self.sim_s + self.sim_per_sleep_s, 9)
        if self.goal_stop is not None and self.sleeps == self.goal_stop[1]:
            self.stop_sim_s = self.sim_s  # when the stop happens, not when it is seen

    def goal_stop_reason(self) -> str | None:
        if not self.goal_handle_available:
            return "aic_goal_handle_unavailable"
        if self.goal_stop is not None and self.sleeps >= self.goal_stop[1]:
            return self.goal_stop[0]
        return None

    def transform(self, frame: str) -> Pose | None:
        after = self.fail_transform_after_sleeps
        if after is not None and self.sleeps >= after:
            raise RuntimeError(SECRET)
        hidden = self.hide_frames_after_sleeps
        if frame in self.hide_frames or (hidden is not None and self.sleeps >= hidden):
            return None
        frames = {
            self.port_frame: self.port,
            "gripper/tcp": self.tcp,
            self.plug_frame: self.plug,
        }
        return frames.get(frame)

    def latest_wrench(self) -> WrenchReading | None:
        stamp = self.sim_s - self.wrench_age_s
        return WrenchReading(stamp_s=stamp, force=self.raw_force, torque=(0.0, 0.0, 0.0))

    def controller_state(self) -> ControllerSnapshot | None:
        if not self.has_controller_state:
            return None
        slow = self.slow_after_sleeps is not None and self.sleeps >= self.slow_after_sleeps
        return ControllerSnapshot(
            tcp=self.tcp,
            tare_force=TARE_N,
            tare_torque=(0.0, 0.0, 0.0),
            tcp_speed_mm_s=0.0 if slow else self.tcp_speed_mm_s,
        )

    def send(self, motion: MotionCommand) -> None:
        self.sends += 1
        if self.fail_send_at is not None and self.sends == self.fail_send_at:
            raise RuntimeError(SECRET)
        self.sent.append(motion)
        self.tcp_at_send.append(self.tcp)
        if motion.mode == "position":
            self.tcp = Pose(position=motion.position_m, orientation=motion.orientation)

    # helpers ------------------------------------------------------------------------------------

    @property
    def port_frame(self) -> str:
        return f"task_board/{self.task.target_module_name}/{self.task.port_name}_link"

    @property
    def plug_frame(self) -> str:
        return f"{self.task.cable_name}/{self.task.plug_name}_link"

    @property
    def plug(self) -> Pose:
        x, y, z = self.tcp.position
        return Pose(position=(x, y, z - PLUG_BELOW_TCP_M), orientation=self.tcp.orientation)
