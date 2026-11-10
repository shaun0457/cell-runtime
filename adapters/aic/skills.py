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

For recovery (settings B and C) approach, align and insert_scripted take `speed_scale` (a
recovery's slow variant), and align and insert_scripted carry on from wherever the descent
is. An align or insert_scripted that starts after a broken descent (a run that did not finish,
or a retract) goes on from the measured pose (`Descent`), not from targets the arm may never
have reached: after a retract, from the retracted pose, not from below it (a pressed plug
holds the TCP far above its target) or from the approach pose; align first comes back up to
z_offset 0.01 if the arm is below that. Run once at full speed, every skill commands exactly
what CheatCode does.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import ClassVar

from adapters.aic.convert import Pose, plug_to_port_mm, to_pose_target
from adapters.aic.geometry import XYIntegrator, gripper_target, xy_integrator_at, z_offset_at
from adapters.aic.world import Geometry
from cellrt.monitor.profiles import MonitorProfile
from cellrt.runtime.state import RunState
from cellrt.skills.base import SkillContext, SkillResult

# CheatCode's approach: 100 targets 50 ms apart, interpolating position and orientation from the
# current pose to CheatCode's pose for z_offset 0.2 (CheatCode.py:216-236; where the tip then
# ends, see the quirk in `geometry`).
APPROACH_STEPS = 100
APPROACH_HEIGHT_M = 0.2
# The slowest a recovery may run a skill (`RecoveryConfig.slow_speed_scale`, 0.5 by default).
# A skill that moves gets twice its duration at this speed as its timeout.
SLOWEST_SCALE = 0.5


def speed_scale(ctx: SkillContext) -> float:
    """The `speed_scale` a recovery's slow variant passes (1.0 otherwise). Slower than
    SLOWEST_SCALE the skill would run into its timeout, so that is refused."""
    scale = float(ctx.params.get("speed_scale", 1.0))
    if not SLOWEST_SCALE <= scale <= 1.0:
        raise ValueError("speed_scale out of range")
    return scale


class AicApproach:
    """Move to CheatCode's approach pose above the port (z_offset 0.2), aligned with it."""

    description: ClassVar[str] = (
        "Moves the plug to a point 20 cm above the port, turned to line up with it."
    )
    name = "approach"
    monitor_profile: MonitorProfile = "motion"
    timeout_s = 2 * 5.0 / SLOWEST_SCALE  # twice CheatCode's 5 s, at the slowest speed

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
        steps = round(APPROACH_STEPS / speed_scale(ctx))
        for t in range(steps):
            g = self._geometry()
            fraction = t / steps  # 0 .. 0.99, as CheatCode
            target, _ = gripper_target(
                g.port,
                g.plug,
                g.gripper,
                z_offset_m=APPROACH_HEIGHT_M,
                slerp_fraction=fraction,
                position_fraction=fraction,
            )
            ctx.world.command(to_pose_target(target))
            ctx.progress((t + 1) / steps)
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
# A plug is on its seat when its tip is within this band around the port frame and this close
# to the port axis. #28 measured one seat per port type: -0.98 mm (SFP, three runs) and
# +0.07 mm (SC, one run), 0.37-0.62 mm off the axis; the SC plug jammed at its entrance read
# 13.46 mm. One band serves both types: 1 mm over the higher seat, 2 mm under the lower one
# (a tip further down is not in a port). 2 mm off the axis is well inside the spacing of
# neighbouring ports. It reads ground-truth geometry, so it holds only where ground truth is
# available (local development, not the submission).
SEATED_MAX_HEIGHT_MM = 1.0
SEATED_MIN_HEIGHT_MM = -3.0
SEATED_MAX_XY_MM = 2.0
# Insertion stops at the seat: in the band and no longer going down, by less than this since
# the previous command (one commanded step is 0.5 mm). A plug still going down is pushed on.
SEATED_STILL_MM = 0.1
SEATED = {
    "max_height_mm": SEATED_MAX_HEIGHT_MM,
    "min_height_mm": SEATED_MIN_HEIGHT_MM,
    "max_xy_mm": SEATED_MAX_XY_MM,
}
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


def descent_from(offsets: list[float], start_m: float | None, scale: float = 1.0) -> list[float]:
    """The z_offsets still to command of `offsets` (one part of CheatCode's descent), from
    `start_m`, the last one commanded (None: the descent has not begun, so from the approach
    height). Going on at full speed from where CheatCode's descent left off, they are its own
    offsets. From anywhere else (after a retract) or slower, they go down from `start_m` in
    steps of `scale` x 0.5 mm and end on the same offset; from below that end, the end alone."""
    start = APPROACH_HEIGHT_M if start_m is None else start_m
    if scale == 1.0 and math.isclose(start - DESCENT_STEP_M, offsets[0], abs_tol=1e-9):
        return list(offsets)
    step = DESCENT_STEP_M * scale
    end = offsets[-1]
    count = max(0, math.ceil((start - end) / step - 1e-9) - 1)  # the steps above the end
    return [*(start - k * step for k in range(1, count + 1)), end]


class Descent:
    """CheatCode's descent as align and insert_scripted share it: the last z_offset commanded
    and the XY integrator, which CheatCode resets only during the approach, so both carry on
    from one part of the descent to the next.

    Both are bookkeeping of what was sent, which is where the arm is only while every command
    arrives and the arm follows it. A descent run that did not finish (the Monitor stopped it,
    or its timeout) or a retract breaks that: commands may have been lost in a dropout, or the
    arm held back by a pressed plug. So after one, the next run goes on from where the arm is
    measured to be (`begin`). In #61 a retry after a dropout went on from targets the arm never
    reached and threw the plug 12.6 mm down into the port in 0.15 s. A run that follows a
    finished one goes on from its last command, as CheatCode's does."""

    def __init__(self) -> None:
        self.integrator = XYIntegrator()
        self.last_height_mm: float | None = None  # the tip's height at the last command
        self.z_offset_m: float | None = None  # the last z_offset commanded
        self._unbroken = True  # every descent run so far ran to its end, and no retract

    def begin(self, g: Geometry) -> None:
        """At the start of align or insert_scripted."""
        if not self._unbroken:
            self._follow(g)
        self._unbroken = False  # until the run ends by itself

    def finished(self) -> None:
        """At the end of a descent run that ran to its end."""
        self._unbroken = True

    def interrupt(self) -> None:
        """A retract moved the arm off the descent: the next run goes on from where it is."""
        self._unbroken = False

    def command(self, ctx: SkillContext, g: Geometry, z_offset_m: float) -> None:
        self.last_height_mm, _ = plug_to_port_mm(g.port, g.plug)
        self.z_offset_m = z_offset_m
        target, self.integrator = gripper_target(
            g.port, g.plug, g.gripper, z_offset_m=z_offset_m, integrator=self.integrator
        )
        ctx.world.command(to_pose_target(target))

    def _follow(self, g: Geometry) -> None:
        """Take the descent up from where the arm is, not where it was sent: a pressed plug
        holds the TCP above its target by the force over the controller's stiffness (90 N/m
        in position mode, so 10 N is 111 mm), and commands lost in a dropout leave the
        z_offset last commanded below the arm. The XY integrator starts again from the arm's
        XY (`xy_integrator_at`): while commands were lost it went on adding the tip's error
        and holds up to 7.5 mm of correction the arm never got. Before the descent has begun
        there is nothing to take up."""
        if self.z_offset_m is not None:
            self.z_offset_m = z_offset_at(g.port, g.plug, g.gripper)
            self.integrator = xy_integrator_at(g.port, g.plug, g.gripper)


def seated(g: Geometry) -> tuple[bool, float]:
    """Whether the plug tip is in the seated band and near the port axis, and its height
    above the port frame in mm."""
    height_mm, (dx, dy) = plug_to_port_mm(g.port, g.plug)
    in_band = SEATED_MIN_HEIGHT_MM <= height_mm <= SEATED_MAX_HEIGHT_MM
    return in_band and math.hypot(dx, dy) <= SEATED_MAX_XY_MM, height_mm


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

    description: ClassVar[str] = (
        "Descends to 10 mm above the port while correcting XY, then holds there until the "
        "plug is within 1 mm of the port axis."
    )
    name = "align"
    monitor_profile: MonitorProfile = "motion"
    timeout_s = 2 * ALIGN_STEPS * STEP_S / SLOWEST_SCALE

    def __init__(self, geometry: Callable[[], Geometry], descent: Descent) -> None:
        self._geometry = geometry
        self._descent = descent

    @classmethod
    def _parameters(cls) -> dict[str, object]:
        return {"steps": ALIGN_STEPS, "height_m": ALIGN_HEIGHT_M, "xy_mm": ALIGN_XY_MM}

    async def run(self, ctx: SkillContext) -> SkillResult:
        scale = speed_scale(ctx)
        self._descent.begin(self._geometry())
        align = descent_from(descent_offsets()[0], self._descent.z_offset_m, scale)
        for i, z in enumerate(align):
            self._descent.command(ctx, self._geometry(), z)
            ctx.progress((i + 1) / len(align))
            await ctx.clock.sleep(ctx.period_s)
        while ctx.world.signals().xy_error_mm >= ALIGN_XY_MM:  # bounded by the timeout
            self._descent.command(ctx, self._geometry(), ALIGN_HEIGHT_M)
            await ctx.clock.sleep(ctx.period_s)
        self._descent.finished()
        return SkillResult(ok=True, detail={"xy_error_mm": ctx.world.signals().xy_error_mm})


class AicInsertScripted(_AicSkill):
    """The rest of CheatCode's descent: to z_offset -0.015, 0.5 mm per step, ending as soon
    as the plug rests on its seat (seated, and no longer going down). Pushing on would only
    press it into the seat, which the Monitor cannot tell from a jam (#28)."""

    description: ClassVar[str] = (
        "Descends into the port in 0.5 mm steps, still correcting XY, and stops once the plug "
        "rests on its seat. It does not twist the plug."
    )
    name = "insert_scripted"
    monitor_profile: MonitorProfile = "insertion"
    timeout_s = 2 * INSERT_STEPS * STEP_S / SLOWEST_SCALE

    def __init__(self, geometry: Callable[[], Geometry], descent: Descent) -> None:
        self._geometry = geometry
        self._descent = descent

    @classmethod
    def _parameters(cls) -> dict[str, object]:
        return {
            "steps": INSERT_STEPS,
            "end_m": INSERT_END_M,
            "seated": SEATED,
            "still_mm": SEATED_STILL_MM,
        }

    async def run(self, ctx: SkillContext) -> SkillResult:
        scale = speed_scale(ctx)
        self._descent.begin(self._geometry())
        insert = descent_from(descent_offsets()[1], self._descent.z_offset_m, scale)
        for i, z in enumerate(insert):
            g = self._geometry()
            on_seat, height_mm = seated(g)
            before = self._descent.last_height_mm
            if on_seat and before is not None and before - height_mm < SEATED_STILL_MM:
                self._descent.finished()
                detail = {"height_mm": round(height_mm, 3), "seated": True}
                return SkillResult(ok=True, detail=detail)
            self._descent.command(ctx, g, z)
            ctx.progress((i + 1) / len(insert))
            await ctx.clock.sleep(ctx.period_s)
        self._descent.finished()
        on_seat, height_mm = seated(self._geometry())
        return SkillResult(ok=True, detail={"height_mm": round(height_mm, 3), "seated": on_seat})


class AicVerify(_AicSkill):
    """Wait for the connector to settle (the arm holds: the executor sent HOLD when insertion
    ended), then check from ground truth that the plug tip is seated in the port."""

    description: ClassVar[str] = (
        "Holds for 5 s while the connector settles, then checks that the plug is seated."
    )
    name = "verify"
    monitor_profile: MonitorProfile = "motion"
    timeout_s = 2 * VERIFY_SETTLE_S

    def __init__(self, geometry: Callable[[], Geometry]) -> None:
        self._geometry = geometry

    @classmethod
    def _parameters(cls) -> dict[str, object]:
        return {"settle_s": VERIFY_SETTLE_S, "seated": SEATED}

    async def run(self, ctx: SkillContext) -> SkillResult:
        steps = round(VERIFY_SETTLE_S / ctx.period_s)
        for i in range(steps):
            ctx.progress(i / steps)
            await ctx.clock.sleep(ctx.period_s)
        on_seat, height_mm = seated(self._geometry())
        ctx.progress(1.0)
        detail: dict[str, object] = {"height_mm": round(height_mm, 3)}
        if not on_seat:
            return SkillResult(ok=False, detail={**detail, "reason": "not seated"})
        return SkillResult(ok=True, detail=detail)


class AicRetract(_AicSkill):
    """Raise the TCP 5 mm straight up from where it is, 0.5 mm per step (for recovery).

    It ignores `to_height_mm`, the mock's height above the port to retract to: here the port
    frame lies at a different depth for each port type (#28), so a height above it means
    different things. Given the descent, it marks it interrupted, so the next align or insert
    goes on down from where the arm is measured after the retract."""

    description: ClassVar[str] = "Lifts the plug 5 mm straight up from where it is."
    name = "retract"
    monitor_profile: MonitorProfile = "unload"
    timeout_s = 2 * RETRACT_STEPS * STEP_S

    def __init__(self, geometry: Callable[[], Geometry], descent: Descent | None = None) -> None:
        self._geometry = geometry
        self._descent = descent

    @classmethod
    def _parameters(cls) -> dict[str, object]:
        return {"steps": RETRACT_STEPS, "distance_m": RETRACT_M}

    async def run(self, ctx: SkillContext) -> SkillResult:
        if self._descent is not None:
            self._descent.interrupt()
        start = self._geometry().gripper
        x, y, z = start.position
        for i in range(1, RETRACT_STEPS + 1):
            up = Pose(
                position=(x, y, z + RETRACT_M * i / RETRACT_STEPS), orientation=start.orientation
            )
            ctx.world.command(to_pose_target(up))
            ctx.progress(i / RETRACT_STEPS)
            await ctx.clock.sleep(ctx.period_s)
        return SkillResult(ok=True, detail={"raised_mm": round(RETRACT_M * 1000, 3)})
