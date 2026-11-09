# Third-party code, modified for cell-runtime. Notices: THIRD_PARTY_NOTICES.md.
#
# AicApproach, descent_offsets, AicAlign, AicInsertScripted and AicVerify's settle wait: adapted
# from the approach, descent and settle steps of CheatCode's insert_cable in AIC
# (https://github.com/intrinsic-dev/aic), aic_example_policies/aic_example_policies/ros/
# CheatCode.py; modified by splitting them into separate skills.
#   Copyright (C) 2026 Intrinsic Innovation LLC
#   Licensed under the Apache License, Version 2.0 (see LICENSE).

"""Scripted skills for the AIC cell, ported from CheatCode (`aic_example_policies/ros/
CheatCode.py` @ `3ab0653`). They command position targets (ADR 014) computed from ground-truth
poses (`AicWorld.geometry`), which they get at construction: the executor hands skills only
`signals` and `command`.
"""

from __future__ import annotations

from collections.abc import Callable

from adapters.aic.convert import Pose, plug_to_port_mm, to_pose_target
from adapters.aic.geometry import XYIntegrator, gripper_target
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


# CheatCode's descent (CheatCode.py:238-251): from z_offset 0.2, take 0.5 mm off and command,
# 50 ms apart, until z_offset is below -0.015, with the XY integrator carried from step to step.
# It is split at 10 mm: align is the part above, insert_scripted the part below.
STEP_S = 0.05
DESCENT_STEP_M = 0.0005
ALIGN_HEIGHT_M = 0.010
INSERT_END_M = -0.015
ALIGN_XY_MM = 1.0  # align is done once the plug is this close to the port axis
VERIFY_SETTLE_S = 5.0  # CheatCode's wait for the connector to settle
# The plug tip at least this far into the port counts as seated. A placeholder for #28: the
# real seated depth depends on the port and on CheatCode's quirk (geometry.py).
VERIFY_MIN_DEPTH_MM = 10.0
RETRACT_M = 0.005
RETRACT_STEPS = 10


def descent_offsets() -> tuple[list[float], list[float]]:
    """CheatCode's commanded z_offsets, split into align's and insert_scripted's.

    Computed the way CheatCode computes them, by subtracting in floating point: after 430 steps
    z_offset is -0.015000000000000173, already below -0.015, so the loop stops there, with
    430 commands and the last one at about -0.015 (exact arithmetic would give one more).
    """
    offsets: list[float] = []
    z_offset = APPROACH_HEIGHT_M
    while not z_offset < INSERT_END_M:
        z_offset -= DESCENT_STEP_M
        offsets.append(z_offset)
    align = [z for z in offsets if z >= ALIGN_HEIGHT_M - 1e-9]  # a tolerance for the split only
    return align, offsets[len(align) :]


ALIGN_STEPS = len(descent_offsets()[0])
INSERT_STEPS = len(descent_offsets()[1])


class Descent:
    """CheatCode's XY integrator, shared by align and insert_scripted: CheatCode resets it only
    during the approach, so it carries on from one part of the descent to the next."""

    def __init__(self) -> None:
        self.integrator = XYIntegrator()

    def command(self, ctx: SkillContext, g: Geometry, z_offset_m: float) -> None:
        target, self.integrator = gripper_target(
            g.port, g.plug, g.gripper, z_offset_m=z_offset_m, integrator=self.integrator
        )
        ctx.world.command(to_pose_target(target))


class _AicSkill:
    name: str
    monitor_profile: MonitorProfile
    timeout_s: float

    def preconditions(self, state: RunState) -> bool:
        return True

    @classmethod
    def config(cls) -> dict[str, object]:
        """What defines the skill's behaviour, for the run's config hash."""
        return {
            "type": cls.__qualname__,
            "timeout_s": cls.timeout_s,
            "monitor_profile": cls.monitor_profile,
            **cls._parameters(),
        }

    @classmethod
    def _parameters(cls) -> dict[str, object]:
        return {}


class AicAlign(_AicSkill):
    """Descend to 10 mm above the port (z_offset 0.01) with CheatCode's XY correction, then
    keep correcting there until the plug is within 1 mm of the port axis."""

    name = "align"
    monitor_profile: MonitorProfile = "motion"
    timeout_s = 2 * ALIGN_STEPS * STEP_S

    def __init__(self, geometry: Callable[[], Geometry], descent: Descent) -> None:
        self._geometry = geometry
        self._descent = descent

    @classmethod
    def _parameters(cls) -> dict[str, object]:
        return {"steps": ALIGN_STEPS, "height_m": ALIGN_HEIGHT_M, "xy_mm": ALIGN_XY_MM}

    async def run(self, ctx: SkillContext) -> SkillResult:
        align, _ = descent_offsets()
        for i, z in enumerate(align):
            self._descent.command(ctx, self._geometry(), z)
            ctx.progress((i + 1) / len(align))
            await ctx.clock.sleep(ctx.period_s)
        while ctx.world.signals().xy_error_mm >= ALIGN_XY_MM:  # bounded by the timeout
            self._descent.command(ctx, self._geometry(), ALIGN_HEIGHT_M)
            await ctx.clock.sleep(ctx.period_s)
        return SkillResult(ok=True, detail={"xy_error_mm": ctx.world.signals().xy_error_mm})


class AicInsertScripted(_AicSkill):
    """The rest of CheatCode's descent: to z_offset -0.015, 0.5 mm per step."""

    name = "insert_scripted"
    monitor_profile: MonitorProfile = "insertion"
    timeout_s = 2 * INSERT_STEPS * STEP_S

    def __init__(self, geometry: Callable[[], Geometry], descent: Descent) -> None:
        self._geometry = geometry
        self._descent = descent

    @classmethod
    def _parameters(cls) -> dict[str, object]:
        return {"steps": INSERT_STEPS, "end_m": INSERT_END_M}

    async def run(self, ctx: SkillContext) -> SkillResult:
        _, insert = descent_offsets()
        for i, z in enumerate(insert):
            self._descent.command(ctx, self._geometry(), z)
            ctx.progress((i + 1) / len(insert))
            await ctx.clock.sleep(ctx.period_s)
        return SkillResult(ok=True, detail={"depth_mm": ctx.world.signals().depth_mm})


class AicVerify(_AicSkill):
    """Wait for the connector to settle (the arm holds: the executor sent HOLD when insertion
    ended), then check from ground truth that the plug tip is seated in the port."""

    name = "verify"
    monitor_profile: MonitorProfile = "motion"
    timeout_s = 2 * VERIFY_SETTLE_S

    def __init__(self, geometry: Callable[[], Geometry]) -> None:
        self._geometry = geometry

    @classmethod
    def _parameters(cls) -> dict[str, object]:
        return {"settle_s": VERIFY_SETTLE_S, "min_depth_mm": VERIFY_MIN_DEPTH_MM}

    async def run(self, ctx: SkillContext) -> SkillResult:
        steps = round(VERIFY_SETTLE_S / ctx.period_s)
        for i in range(steps):
            ctx.progress(i / steps)
            await ctx.clock.sleep(ctx.period_s)
        g = self._geometry()
        height_mm, _ = plug_to_port_mm(g.port, g.plug)
        depth_mm = round(max(0.0, -height_mm), 3)
        ctx.progress(1.0)
        if depth_mm < VERIFY_MIN_DEPTH_MM:
            return SkillResult(ok=False, detail={"depth_mm": depth_mm, "reason": "not seated"})
        return SkillResult(ok=True, detail={"depth_mm": depth_mm})


class AicRetract(_AicSkill):
    """Raise the TCP 5 mm straight up from where it is, 0.5 mm per step (for recovery)."""

    name = "retract"
    monitor_profile: MonitorProfile = "unload"
    timeout_s = 2 * RETRACT_STEPS * STEP_S

    def __init__(self, geometry: Callable[[], Geometry]) -> None:
        self._geometry = geometry

    @classmethod
    def _parameters(cls) -> dict[str, object]:
        return {"steps": RETRACT_STEPS, "distance_m": RETRACT_M}

    async def run(self, ctx: SkillContext) -> SkillResult:
        start = self._geometry().gripper
        x, y, z = start.position
        for i in range(1, RETRACT_STEPS + 1):
            up = Pose(
                position=(x, y, z + RETRACT_M * i / RETRACT_STEPS), orientation=start.orientation
            )
            ctx.world.command(to_pose_target(up))
            ctx.progress(i / RETRACT_STEPS)
            await ctx.clock.sleep(ctx.period_s)
        return SkillResult(ok=True, detail={})
