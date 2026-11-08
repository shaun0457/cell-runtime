"""Phase 0 skills on the mock world. Phase 2 replaces them with skills on the AIC interface."""

from __future__ import annotations

import math
from dataclasses import dataclass

from cellrt.monitor.profiles import MonitorProfile
from cellrt.runtime.state import RunState
from cellrt.skills.base import SkillContext, SkillResult
from cellrt.world.base import HOLD, Velocity


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


@dataclass(frozen=True)
class Approach:
    """Descend to a hover height above the port."""

    name: str = "approach"
    monitor_profile: MonitorProfile = "motion"
    timeout_s: float = 5.0
    hover_mm: float = 5.0
    speed_mm_s: float = 20.0
    gain_per_s: float = 4.0
    tolerance_mm: float = 0.2

    def preconditions(self, state: RunState) -> bool:
        return True

    async def run(self, ctx: SkillContext) -> SkillResult:
        start = ctx.world.signals().height_mm
        while True:
            s = ctx.world.signals()
            error = s.height_mm - self.hover_mm
            if abs(error) < self.tolerance_mm:
                ctx.world.command(HOLD)
                ctx.progress(1.0)
                return SkillResult(ok=True, detail={"height_mm": s.height_mm})
            ctx.world.command(Velocity(vz=_clamp(-self.gain_per_s * error, self.speed_mm_s)))
            span = start - self.hover_mm
            ctx.progress((start - s.height_mm) / span if span > 0 else 1.0)
            await ctx.clock.sleep(ctx.period_s)


@dataclass(frozen=True)
class Align:
    """Center the plug over the port with P control on the measured XY offset, at hover height:
    a plug below it (resting on the rim after a missed insertion) first rises back up."""

    name: str = "align"
    monitor_profile: MonitorProfile = "motion"
    timeout_s: float = 5.0
    gain_per_s: float = 2.0
    speed_mm_s: float = 5.0
    tolerance_mm: float = 0.3
    settle_samples: int = 3
    hover_mm: float = 5.0
    hover_tolerance_mm: float = 0.5
    rise_speed_mm_s: float = 10.0

    def preconditions(self, state: RunState) -> bool:
        return True

    async def run(self, ctx: SkillContext) -> SkillResult:
        while ctx.world.signals().height_mm < self.hover_mm - self.hover_tolerance_mm:
            ctx.world.command(Velocity(vz=self.rise_speed_mm_s))
            await ctx.clock.sleep(ctx.period_s)
        inside = 0
        start = max(ctx.world.signals().xy_error_mm, self.tolerance_mm)
        while True:
            s = ctx.world.signals()
            inside = inside + 1 if s.xy_error_mm < self.tolerance_mm else 0
            if inside >= self.settle_samples:
                ctx.world.command(HOLD)
                ctx.progress(1.0)
                return SkillResult(ok=True, detail={"xy_error_mm": s.xy_error_mm})
            dx, dy = s.xy_offset_mm
            ctx.world.command(
                Velocity(
                    vx=_clamp(-self.gain_per_s * dx, self.speed_mm_s),
                    vy=_clamp(-self.gain_per_s * dy, self.speed_mm_s),
                )
            )
            ctx.progress(min(1.0, 1.0 - (s.xy_error_mm - self.tolerance_mm) / start))
            await ctx.clock.sleep(ctx.period_s)


@dataclass(frozen=True)
class Retract:
    """Pull the plug back out to a height above the port."""

    name: str = "retract"
    monitor_profile: MonitorProfile = "unload"
    timeout_s: float = 4.0
    to_height_mm: float = 5.0
    speed_mm_s: float = 10.0

    def preconditions(self, state: RunState) -> bool:
        return True

    async def run(self, ctx: SkillContext) -> SkillResult:
        to_height = float(ctx.params.get("to_height_mm", self.to_height_mm))
        start = ctx.world.signals().height_mm
        while (s := ctx.world.signals()).height_mm < to_height:
            ctx.world.command(Velocity(vz=self.speed_mm_s))
            span = to_height - start
            ctx.progress((s.height_mm - start) / span if span > 0 else 1.0)
            await ctx.clock.sleep(ctx.period_s)
        ctx.world.command(HOLD)
        ctx.progress(1.0)
        return SkillResult(ok=True, detail={"height_mm": s.height_mm})


@dataclass(frozen=True)
class WaitSensor:
    """Hold still until the force/torque sensor reports valid data again, for at most a while."""

    name: str = "wait_sensor"
    monitor_profile: MonitorProfile = "sensor_wait"
    timeout_s: float = 12.0  # a backstop: the wait itself is bounded by max_wait_s
    max_wait_s: float = 2.0
    valid_samples: int = 3

    def preconditions(self, state: RunState) -> bool:
        return True

    async def run(self, ctx: SkillContext) -> SkillResult:
        ctx.world.command(HOLD)
        max_wait = float(ctx.params.get("max_wait_s", self.max_wait_s))
        needed = int(ctx.params.get("valid_samples", self.valid_samples))
        valid = waited = 0
        while valid < needed:
            if waited * ctx.period_s >= max_wait:
                return SkillResult(
                    ok=False,
                    detail={
                        "waited_s": round(waited * ctx.period_s, 6),
                        "reason": "sensor still invalid",
                    },
                )
            await ctx.clock.sleep(ctx.period_s)
            waited += 1
            valid = valid + 1 if ctx.world.signals().wrench_valid else 0
        ctx.progress(1.0)
        return SkillResult(ok=True, detail={"waited_s": round(waited * ctx.period_s, 6)})


@dataclass(frozen=True)
class InsertScripted:
    """Descend at a fixed speed until the plug reaches the expected depth (CheatCode-like)."""

    name: str = "insert_scripted"
    monitor_profile: MonitorProfile = "insertion"
    timeout_s: float = 15.0
    speed_mm_s: float = 4.0
    expected_depth_mm: float = 8.0
    depth_tolerance_mm: float = 0.1

    def preconditions(self, state: RunState) -> bool:
        return True

    async def run(self, ctx: SkillContext) -> SkillResult:
        speed = float(ctx.params.get("speed_mm_s", self.speed_mm_s))
        speed *= float(ctx.params.get("speed_scale", 1.0))
        target = self.expected_depth_mm - self.depth_tolerance_mm
        while True:
            s = ctx.world.signals()
            if s.depth_mm >= target:
                ctx.world.command(HOLD)
                ctx.progress(1.0)
                return SkillResult(ok=True, detail={"depth_mm": s.depth_mm})
            ctx.world.command(Velocity(vz=-speed))
            ctx.progress(s.depth_mm / self.expected_depth_mm)
            await ctx.clock.sleep(ctx.period_s)


@dataclass(frozen=True)
class InsertAct:
    """Stand-in for the learned ACT insert policy (Phase 2 runs the real one as this skill).

    Its failure profile differs from `insert_scripted`: it descends at half the speed, so a tight
    fit stays under the force limit, and when the plug wedges it twists with the torque until
    the torque is gone, which frees a yaw mismatch. It does not correct XY.
    """

    name: str = "insert_act"
    monitor_profile: MonitorProfile = "insertion"
    timeout_s: float = 20.0
    speed_mm_s: float = 2.0
    twist_deg_s: float = 30.0
    torque_threshold_nm: float = 0.1
    expected_depth_mm: float = 8.0
    depth_tolerance_mm: float = 0.1

    def preconditions(self, state: RunState) -> bool:
        return True

    async def run(self, ctx: SkillContext) -> SkillResult:
        speed = float(ctx.params.get("speed_mm_s", self.speed_mm_s))
        speed *= float(ctx.params.get("speed_scale", 1.0))
        target = self.expected_depth_mm - self.depth_tolerance_mm
        twisted_deg = 0.0  # as commanded, for the result detail; the world may clamp it
        while True:
            s = ctx.world.signals()
            if s.depth_mm >= target:
                ctx.world.command(HOLD)
                ctx.progress(1.0)
                detail = {"depth_mm": s.depth_mm, "twisted_deg": round(twisted_deg, 6)}
                return SkillResult(ok=True, detail=detail)
            if s.wrench_valid and abs(s.torque_nm) > self.torque_threshold_nm:
                twist = math.copysign(self.twist_deg_s, s.torque_nm)
                ctx.world.command(Velocity(wz=twist))  # stop descending while twisting
                twisted_deg += twist * ctx.period_s
            else:
                ctx.world.command(Velocity(vz=-speed))
            ctx.progress(s.depth_mm / self.expected_depth_mm)
            await ctx.clock.sleep(ctx.period_s)


@dataclass(frozen=True)
class Verify:
    """Hold still briefly, then check the plug is seated at full depth."""

    name: str = "verify"
    monitor_profile: MonitorProfile = "motion"
    timeout_s: float = 2.0
    hold_samples: int = 10
    expected_depth_mm: float = 8.0
    depth_tolerance_mm: float = 0.3

    def preconditions(self, state: RunState) -> bool:
        return True

    async def run(self, ctx: SkillContext) -> SkillResult:
        ctx.world.command(HOLD)
        for i in range(self.hold_samples):
            ctx.progress(i / self.hold_samples)
            await ctx.clock.sleep(ctx.period_s)
        s = ctx.world.signals()
        seated = s.depth_mm >= self.expected_depth_mm - self.depth_tolerance_mm
        ctx.progress(1.0)
        return SkillResult(ok=seated, detail={"depth_mm": s.depth_mm})
