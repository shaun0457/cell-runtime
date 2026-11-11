"""A fake aic_model node behind the adapter's `NodePort`, for tests on any OS.

Sim time moves only when the policy thread sleeps (wall time), by `sim_per_sleep_s` per sleep;
a test sets it to 0 to stall the sim, or schedules a jump back with `reset_after_sleeps`. The
arm tracks every position target exactly, and the plug hangs `PLUG_BELOW_TCP_M` under the TCP.
Goal events (a cancel, an abort, deactivation) and failures are scripted by sleep count.
`seat_height_m` stops the tip that far above the port frame (by default where #28 measured an
SFP plug on its seat, 0.98 mm under the frame), as a real port stops a plug on its seat; None
lets it go on down through the port.
`plug_below_tcp_m` sets how far the tip hangs under the TCP.
The F/T broadcaster publishes every `wrench_period_s` of sim time (50 Hz by default), from
`wrench_age_s` before the goal's start (or `wrench_delay_s` after it), through a real
`WrenchFeed`; `force_at` scripts the raw force per stamp, and `wrench_until_s` stops the stream.
`frames_from_s` keeps the ground-truth frames away until that much sim time after the goal's
start, as TF that is not up yet when a goal arrives.
`tcp_at_send` keeps the TCP measured just before each motion update, so a test can tell HOLD
(a target at the measured pose) from a skill's last target.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from adapters.aic.convert import MotionCommand, Pose, Vec3
from adapters.aic.force import WrenchDrain, WrenchFeed
from adapters.aic.node import ControllerSnapshot, WrenchReading
from cellrt.runtime.task import Task

PLUG_BELOW_TCP_M = 0.04
SFP_SEAT_M = -0.00098  # an SFP plug on its seat, below the port frame (#28)
SECRET = "upstream text with a key: sk-123"  # must never reach the store
PORT = Pose(position=(0.5, 0.1, 0.05))
START_TCP = Pose(position=(0.3, -0.1, 0.45))
TARE_N = (0.0, 0.0, 1.0)
ZERO3 = (0.0, 0.0, 0.0)
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
    wrench_age_s: float = 0.0  # how far the F/T stream lags sim time
    wrench_delay_s: float = 0.0  # the first message comes this long after the goal's start
    frames_from_s: float | None = None  # no ground-truth frame before this, from the start
    wrench_period_s: float = 0.02  # the broadcaster's 50 Hz
    wrench_until_s: float | None = None  # no message stamped after this
    force_at: Callable[[float], Vec3] | None = None  # raw force per stamp; else raw_force
    wrenches: WrenchFeed = field(default_factory=WrenchFeed)
    published: int = 0  # wrench messages so far
    wrench_start_s: float | None = None  # the first message's stamp, set on the first drain
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
    plug_xy_offset_m: tuple[float, float] = (0.0, 0.0)  # where the plug hangs off the TCP axis
    plug_below_tcp_m: float = PLUG_BELOW_TCP_M
    # The tip never goes below this height above the port; the default task is an SFP one.
    seat_height_m: float | None = SFP_SEAT_M
    # From this much sim time after the goal's start, the connector has sprung back out: the
    # tip reads `plug_pops_out_to_m` above the port, whatever the arm does.
    plug_pops_out_after_s: float | None = None
    plug_pops_out_to_m: float = 0.002
    plug_pops_out_xy_m: tuple[float, float] | None = None  # and this far off the port axis
    fail_feedback: bool = False
    feedback: list[tuple[float, str]] = field(default_factory=list)  # (sim time, message)
    start_sim_s: float = field(init=False)
    sends: int = 0
    sent: list[MotionCommand] = field(default_factory=list)
    tcp_at_send: list[Pose] = field(default_factory=list)
    sleeps: int = 0

    def __post_init__(self) -> None:
        self.start_sim_s = self.sim_s

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
        early = self.frames_from_s
        if early is not None and self.sim_s - self.start_sim_s < early - 1e-9:
            return None
        hidden = self.hide_frames_after_sleeps
        if frame in self.hide_frames or (hidden is not None and self.sleeps >= hidden):
            return None
        frames = {
            self.port_frame: self.port,
            "gripper/tcp": self.tcp,
            self.plug_frame: self.plug,
        }
        return frames.get(frame)

    def drain_wrenches(self) -> WrenchDrain:
        if self.has_controller_state:
            self.wrenches.on_tare(TARE_N, (0.0, 0.0, 0.0))
        if self.wrench_start_s is None:  # the stream starts with the goal
            self.wrench_start_s = self.sim_s - self.wrench_age_s + self.wrench_delay_s
        until = self.sim_s - self.wrench_age_s
        if self.wrench_until_s is not None:
            until = min(until, self.wrench_until_s)
        while True:
            stamp = round(self.wrench_start_s + self.published * self.wrench_period_s, 9)
            if stamp > until + 1e-9:
                break
            force = self.force_at(stamp) if self.force_at is not None else self.raw_force
            self.wrenches.on_wrench(WrenchReading(stamp_s=stamp, force=force, torque=ZERO3))
            self.published += 1
        return self.wrenches.drain()

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

    def send_feedback(self, message: str) -> None:
        if self.fail_feedback:
            raise RuntimeError(SECRET)
        self.feedback.append((self.sim_s, message))

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
        dx, dy = self.plug_xy_offset_m
        tip_z = z - self.plug_below_tcp_m
        if self.seat_height_m is not None:
            tip_z = max(tip_z, self.port.position[2] + self.seat_height_m)
        popped = self.plug_pops_out_after_s
        if popped is not None and self.sim_s - self.start_sim_s >= popped - 1e-9:
            tip_z = max(tip_z, self.port.position[2] + self.plug_pops_out_to_m)
            if self.plug_pops_out_xy_m is not None:
                px, py, _ = self.port.position
                ox, oy = self.plug_pops_out_xy_m
                return Pose(position=(px + ox, py + oy, tip_z), orientation=self.tcp.orientation)
        return Pose(position=(x + dx, y + dy, tip_z), orientation=self.tcp.orientation)
