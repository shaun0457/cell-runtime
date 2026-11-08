"""A fake aic_model node behind the adapter's `NodePort`, for tests on any OS.

Sim time moves only when the policy thread sleeps (wall time), by `sim_per_sleep_s` per sleep;
a test sets it to 0 to stall the sim, or schedules a jump back with `reset_after_sleeps`. The
arm tracks every position target exactly, and the plug hangs `PLUG_BELOW_TCP_M` under the TCP.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from adapters.aic.convert import MotionCommand, Pose
from adapters.aic.node import ControllerSnapshot, WrenchReading
from cellrt.runtime.task import Task

PLUG_BELOW_TCP_M = 0.04
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
    sent: list[MotionCommand] = field(default_factory=list)
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
        else:
            self.sim_s = round(self.sim_s + self.sim_per_sleep_s, 9)

    def transform(self, frame: str) -> Pose | None:
        if frame in self.hide_frames:
            return None
        frames = {
            self.port_frame: self.port,
            "gripper/tcp": self.tcp,
            self.plug_frame: self.plug,
        }
        return frames.get(frame)

    def latest_wrench(self) -> WrenchReading | None:
        stamp = self.sim_s - self.wrench_age_s
        return WrenchReading(stamp_s=stamp, force=RAW_N, torque=(0.0, 0.0, 0.0))

    def controller_state(self) -> ControllerSnapshot | None:
        if not self.has_controller_state:
            return None
        return ControllerSnapshot(tcp=self.tcp, tare_force=TARE_N, tare_torque=(0.0, 0.0, 0.0))

    def send(self, motion: MotionCommand) -> None:
        self.sent.append(motion)
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
