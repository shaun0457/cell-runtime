"""Phase 0 skills on the mock world. Phase 2 replaces them with skills on the AIC interface."""

from __future__ import annotations

from dataclasses import dataclass

from cellrt.runtime.state import RunState
from cellrt.skills.base import SkillContext, SkillResult
from cellrt.world.base import HOLD, Velocity


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


@dataclass(frozen=True)
class Approach:
    """Descend to a hover height above the port."""

    name: str = "approach"
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
    """Center the plug over the port with P control on the measured XY offset."""

    name: str = "align"
    timeout_s: float = 5.0
    gain_per_s: float = 2.0
    speed_mm_s: float = 5.0
    tolerance_mm: float = 0.3
    settle_samples: int = 3

    def preconditions(self, state: RunState) -> bool:
        return True

    async def run(self, ctx: SkillContext) -> SkillResult:
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
class InsertScripted:
    """Descend at a fixed speed until the plug reaches the expected depth (CheatCode-like)."""

    name: str = "insert_scripted"
    timeout_s: float = 10.0
    speed_mm_s: float = 4.0
    expected_depth_mm: float = 8.0
    depth_tolerance_mm: float = 0.1

    def preconditions(self, state: RunState) -> bool:
        return True

    async def run(self, ctx: SkillContext) -> SkillResult:
        speed = float(ctx.params.get("speed_mm_s", self.speed_mm_s))
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
class Verify:
    """Hold still briefly, then check the plug is seated at full depth."""

    name: str = "verify"
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
