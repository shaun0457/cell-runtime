"""Scripted skills for the AIC cell, ported from CheatCode (`aic_example_policies/ros/
CheatCode.py` @ `3ab0653`). They command position targets (ADR 014) computed from ground-truth
poses (`AicWorld.geometry`), which they get at construction: the executor hands skills only
`signals` and `command`.
"""

from __future__ import annotations

from collections.abc import Callable

from adapters.aic.convert import plug_to_port_mm, to_pose_target
from adapters.aic.geometry import gripper_target
from adapters.aic.world import Geometry
from cellrt.monitor.profiles import MonitorProfile
from cellrt.runtime.state import RunState
from cellrt.skills.base import SkillContext, SkillResult

# CheatCode's approach: 100 targets 50 ms apart, interpolating position and orientation from the
# current pose to CheatCode's pose for z_offset 0.2 (CheatCode.py:216-236; where the tip then
# ends, see the quirk in `geometry`).
APPROACH_STEPS = 100
APPROACH_HEIGHT_M = 0.2


class AicApproach:
    """Move to CheatCode's approach pose above the port (z_offset 0.2), aligned with it."""

    name = "approach"
    monitor_profile: MonitorProfile = "motion"
    timeout_s = 10.0  # twice CheatCode's 5 s

    def __init__(self, geometry: Callable[[], Geometry]) -> None:
        self._geometry = geometry

    @classmethod
    def config(cls) -> dict[str, object]:
        """What defines the skill's behaviour, for the run's config hash."""
        return {
            "type": cls.__qualname__,
            "steps": APPROACH_STEPS,
            "height_m": APPROACH_HEIGHT_M,
            "timeout_s": cls.timeout_s,
            "monitor_profile": cls.monitor_profile,
        }

    def preconditions(self, state: RunState) -> bool:
        return True

    async def run(self, ctx: SkillContext) -> SkillResult:
        for t in range(APPROACH_STEPS):
            g = self._geometry()
            fraction = t / APPROACH_STEPS  # 0 .. 0.99, as CheatCode
            target, _ = gripper_target(
                g.port,
                g.plug,
                g.gripper,
                z_offset_m=APPROACH_HEIGHT_M,
                slerp_fraction=fraction,
                position_fraction=fraction,
            )
            ctx.world.command(to_pose_target(target))
            ctx.progress((t + 1) / APPROACH_STEPS)
            await ctx.clock.sleep(ctx.period_s)
        g = self._geometry()
        height_mm, _ = plug_to_port_mm(g.port, g.plug)
        return SkillResult(ok=True, detail={"height_mm": round(height_mm, 3)})
