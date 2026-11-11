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
z_offset 0.01 if the arm is below that.

Run once at full speed, every skill commands what CheatCode does, with one addition (ADR 024):
align stops its descent once, just above the port's entrance, and corrects XY there until the
plug is on the port axis (at most `ENTRY_HOLD_S`), then goes on with CheatCode's offsets. The
skills judge a seat and place that hold by the port type's measured geometry (`PortProfile`).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from types import MappingProxyType
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
# A skill's timeout is twice its duration at the speed it runs (`_AicSkill.timeout_for`).
SLOWEST_SCALE = 0.5


def speed_scale(ctx: SkillContext) -> float:
    """The `speed_scale` a recovery's slow variant passes (1.0 otherwise)."""
    return _scale_of(ctx.params)


def _scale_of(params: Mapping[str, object]) -> float:
    """The `speed_scale` in `params`, 1.0 without one. Anything but a number from
    SLOWEST_SCALE to 1 raises ValueError: the same bound `run_goal` checks a recovery's slow
    speed against before anything runs."""
    raw = params.get("speed_scale", 1.0)
    scale = float(raw) if isinstance(raw, int | float | str) else math.nan
    if not SLOWEST_SCALE <= scale <= 1.0:
        raise ValueError("speed_scale out of range")
    return scale


class _AicSkill:
    name: str
    monitor_profile: MonitorProfile
    timeout_s: float  # twice the skill's duration at full speed
    # Whether a recovery may slow the skill down (`speed_scale`), so its time grows with it.
    scales_with_speed: ClassVar[bool] = False

    def preconditions(self, state: RunState) -> bool:
        """Every AIC skill can start in any state a run reaches. A precondition that fails ends
        the step with no failure class, so recovery has nothing to act on; a plug off the axis or
        a stale wrench is the Monitor's to classify (POSE_ERROR, SENSOR_DROPOUT, after their
        0.2 s), and recovery has a plan for each. The slow rerun after a FORCE_LIMIT retract, the
        rerun after a sensor wait and a TIMEOUT retry start insertion without align, and its
        descent corrects XY on the way down. (#64 review F2 tried insertion within 1 mm of the
        axis: it would refuse those reruns, and in the mock a like condition, with align's
        0.3 mm, took B's success from 0.619 to 0.571.)"""
        return True

    def timeout_for(self, params: Mapping[str, object]) -> float:
        """The timeout of a run with these params: twice its duration at the speed it runs,
        so `timeout_s` over `speed_scale` for a skill that slows down. A scale the run itself
        refuses (`speed_scale`) keeps `timeout_s`."""
        if not self.scales_with_speed:
            return self.timeout_s
        try:
            return self.timeout_s / _scale_of(params)
        except ValueError:
            return self.timeout_s

    def config(self) -> dict[str, object]:
        """What defines the skill's behaviour, for the run's config hash."""
        return {
            "type": type(self).__qualname__,
            "timeout_s": self.timeout_s,
            "timeout_scales_with_speed": self.scales_with_speed,
            "monitor_profile": self.monitor_profile,
            **self._parameters(),
        }

    def _parameters(self) -> dict[str, object]:
        return {}


class AicApproach(_AicSkill):
    """Move to CheatCode's approach pose above the port (z_offset 0.2), aligned with it."""

    description: ClassVar[str] = (
        "Moves the plug to a point 20 cm above the port, turned to line up with it."
    )
    name = "approach"
    monitor_profile: MonitorProfile = "motion"
    timeout_s = 2 * 5.0  # twice CheatCode's 5 s
    scales_with_speed = True

    def __init__(self, geometry: Callable[[], Geometry]) -> None:
        self._geometry = geometry

    def _parameters(self) -> dict[str, object]:
        return {"steps": APPROACH_STEPS, "height_m": APPROACH_HEIGHT_M}

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
# A plug is on its seat when its tip is in its port type's band around the port frame
# (`PortProfile`) and this close to the port axis: well inside the spacing of neighbouring
# ports. The skills read ground-truth geometry, so this holds only where ground truth is
# available (local development, not the submission).
SEATED_MAX_XY_MM = 2.0
# Insertion stops at the seat: in the band and no longer moving, by less than this either way
# since the previous command (one commanded step is 0.5 mm). A plug still going down, or
# springing back up, is pushed on.
SEATED_STILL_MM = 0.1
# Align's hold above the entrance (ADR 024): the first time the tip is at most this far above
# its port's entrance, the descent stops and corrects XY until the plug has been this close to
# the port axis for this many samples in a row (one dip under the threshold is not enough),
# for at most this long of the run's clock, then goes on. In the 13 SC runs on record, every
# plug that met the entrance 0.5 mm or more off the axis stuck on its rim, and three of the
# four that met it within 0.18 mm went in.
ENTRY_HOLD_ABOVE_MM = 3.0
ENTRY_XY_MM = 0.15
ENTRY_SAMPLES = 3
ENTRY_HOLD_S = 3.0
RETRACT_M = 0.005
RETRACT_STEPS = 10


@dataclass(frozen=True)
class PortProfile:
    """A port type's geometry as the sim measured it, in mm of plug-tip height above the port
    frame: where its entrance is and the band a seated tip is in."""

    entrance_mm: float
    seated_min_mm: float
    seated_max_mm: float

    @property
    def hold_at_mm(self) -> float:
        """Where align stops to correct XY before the plug meets the entrance."""
        return self.entrance_mm + ENTRY_HOLD_ABOVE_MM

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


# The sample config's two port types. Entrances from the port models (#28). Seats: an SFP tip
# on its seat read -0.97 to -0.98 mm in every run, and one that stopped at -0.24 mm the scorer
# called a partial insertion (#70), so the band is about 0.4 mm either side of the seat; an SC
# tip on its seat read +0.05 to +0.07 mm, against 5.15 mm for one stopped in the bore and
# 13.5 mm on the entrance rim, so the band is 0.5 mm either side of the frame.
PORT_PROFILES: Mapping[str, PortProfile] = MappingProxyType(
    {
        "sfp": PortProfile(entrance_mm=45.8, seated_min_mm=-1.5, seated_max_mm=-0.6),
        "sc": PortProfile(entrance_mm=13.64, seated_min_mm=-0.5, seated_max_mm=0.5),
    }
)


class UnknownPortType(ValueError):
    """The task's port type has no measured profile. The message is fixed and safe to log."""


def port_profile(port_type: str) -> PortProfile:
    """The profile of `port_type`. Raises UnknownPortType, without repeating the value, for a
    type with no measured profile: no seat or entrance to judge it by."""
    profile = PORT_PROFILES.get(port_type)
    if profile is None:
        raise UnknownPortType(f"port_type must be one of: {', '.join(sorted(PORT_PROFILES))}")
    return profile


def ports_config() -> dict[str, dict[str, float]]:
    """Every port type's profile, for a skill's config. A skill records the whole table, not
    the profile it was built with: which one a run uses follows from its task's port type, in
    the scenario part of the config hash, so the goals of one engine run (SFP and SC in the
    sample config) stay one configuration for the agent report."""
    return {name: PORT_PROFILES[name].as_dict() for name in sorted(PORT_PROFILES)}


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
        # The tip's height at the last descent command; None after a retract, which is not one.
        self.last_height_mm: float | None = None
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
        """A retract moved the arm off the descent: the next run goes on from where it is, and
        judges whether the plug is still from its own first step, not from a height measured
        before the retract."""
        self._unbroken = False
        self.last_height_mm = None

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


def seated(g: Geometry, port: PortProfile) -> tuple[bool, float]:
    """Whether the plug tip is in its port type's seated band and near the port axis, and its
    height above the port frame in mm."""
    height_mm, (dx, dy) = plug_to_port_mm(g.port, g.plug)
    in_band = port.seated_min_mm <= height_mm <= port.seated_max_mm
    return in_band and math.hypot(dx, dy) <= SEATED_MAX_XY_MM, height_mm


def _off_axis_mm(g: Geometry) -> float:
    _, (dx, dy) = plug_to_port_mm(g.port, g.plug)
    return math.hypot(dx, dy)


class AicAlign(_AicSkill):
    """Descend to 10 mm above the port (z_offset 0.01) with CheatCode's XY correction, then
    keep correcting there until the plug is within 1 mm of the port axis. On the way down it
    stops once just above the port's entrance (ADR 024): there it corrects XY until the plug
    has been within ENTRY_XY_MM of the axis for ENTRY_SAMPLES samples in a row, for at most
    ENTRY_HOLD_S, then goes on. An align that starts under that point (in the port, after a
    retract) does not stop."""

    description: ClassVar[str] = (
        "Descends to 10 mm above the port while correcting XY, stopping just above the port's "
        "entrance until the plug is on the port axis, then holds at 10 mm until the plug is "
        "within 1 mm of the axis."
    )
    name = "align"
    monitor_profile: MonitorProfile = "motion"
    # Twice the descent's 19 s at full speed; the hold above the entrance, at most 3 s, fits in.
    timeout_s = 2 * ALIGN_STEPS * STEP_S
    scales_with_speed = True

    def __init__(
        self, geometry: Callable[[], Geometry], descent: Descent, port: PortProfile
    ) -> None:
        self._geometry = geometry
        self._descent = descent
        self._port = port

    def _parameters(self) -> dict[str, object]:
        return {
            "steps": ALIGN_STEPS,
            "height_m": ALIGN_HEIGHT_M,
            "xy_mm": ALIGN_XY_MM,
            "entry": {
                "above_entrance_mm": ENTRY_HOLD_ABOVE_MM,
                "xy_mm": ENTRY_XY_MM,
                "samples": ENTRY_SAMPLES,
                "max_hold_s": ENTRY_HOLD_S,
            },
            "ports": ports_config(),
        }

    async def run(self, ctx: SkillContext) -> SkillResult:
        scale = speed_scale(ctx)
        g = self._geometry()
        self._descent.begin(g)
        align = descent_from(descent_offsets()[0], self._descent.z_offset_m, scale)
        above_entry = plug_to_port_mm(g.port, g.plug)[0] > self._port.hold_at_mm
        entry: dict[str, object] = {}
        for i, z in enumerate(align):
            g = self._geometry()
            if above_entry and plug_to_port_mm(g.port, g.plug)[0] <= self._port.hold_at_mm:
                above_entry = False
                entry = await self._hold_at_entry(ctx)
                g = self._geometry()
            self._descent.command(ctx, g, z)
            ctx.progress((i + 1) / len(align))
            await ctx.clock.sleep(ctx.period_s)
        while ctx.world.signals().xy_error_mm >= ALIGN_XY_MM:  # bounded by the timeout
            self._descent.command(ctx, self._geometry(), ALIGN_HEIGHT_M)
            await ctx.clock.sleep(ctx.period_s)
        self._descent.finished()
        detail = {"xy_error_mm": ctx.world.signals().xy_error_mm, **entry}
        return SkillResult(ok=True, detail=detail)

    async def _hold_at_entry(self, ctx: SkillContext) -> dict[str, object]:
        """Command the z_offset reached again, correcting XY with the integrator, until the
        plug has been within ENTRY_XY_MM of the axis for ENTRY_SAMPLES samples in a row
        (counting the one at the hold point) or ENTRY_HOLD_S of the run's clock has passed;
        what it reached. The steps are capped too, at what the time allows at full rate."""
        z = self._descent.z_offset_m
        start_s = ctx.clock.now()
        max_steps = round(ENTRY_HOLD_S / ctx.period_s)
        steps = 0
        g = self._geometry()
        off_mm = _off_axis_mm(g)
        on_axis = 1 if off_mm < ENTRY_XY_MM else 0
        while (
            z is not None
            and on_axis < ENTRY_SAMPLES
            and steps < max_steps
            and ctx.clock.now() - start_s < ENTRY_HOLD_S - 1e-9
        ):
            self._descent.command(ctx, g, z)
            await ctx.clock.sleep(ctx.period_s)
            steps += 1
            g = self._geometry()
            off_mm = _off_axis_mm(g)
            on_axis = on_axis + 1 if off_mm < ENTRY_XY_MM else 0
        return {
            "entry_xy_mm": round(off_mm, 3),
            "entry_height_mm": round(plug_to_port_mm(g.port, g.plug)[0], 3),
            "entry_held_s": round(ctx.clock.now() - start_s, 3),
            "entry_converged": on_axis >= ENTRY_SAMPLES,
        }


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
    timeout_s = 2 * INSERT_STEPS * STEP_S
    scales_with_speed = True

    def __init__(
        self, geometry: Callable[[], Geometry], descent: Descent, port: PortProfile
    ) -> None:
        self._geometry = geometry
        self._descent = descent
        self._port = port

    def _parameters(self) -> dict[str, object]:
        return {
            "steps": INSERT_STEPS,
            "end_m": INSERT_END_M,
            "seated_max_xy_mm": SEATED_MAX_XY_MM,
            "still_mm": SEATED_STILL_MM,
            "ports": ports_config(),
        }

    async def run(self, ctx: SkillContext) -> SkillResult:
        scale = speed_scale(ctx)
        self._descent.begin(self._geometry())
        insert = descent_from(descent_offsets()[1], self._descent.z_offset_m, scale)
        for i, z in enumerate(insert):
            g = self._geometry()
            on_seat, height_mm = seated(g, self._port)
            before = self._descent.last_height_mm
            if on_seat and before is not None and abs(before - height_mm) < SEATED_STILL_MM:
                self._descent.finished()
                detail = {"height_mm": round(height_mm, 3), "seated": True}
                return SkillResult(ok=True, detail=detail)
            self._descent.command(ctx, g, z)
            ctx.progress((i + 1) / len(insert))
            await ctx.clock.sleep(ctx.period_s)
        self._descent.finished()
        on_seat, height_mm = seated(self._geometry(), self._port)
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

    def __init__(self, geometry: Callable[[], Geometry], port: PortProfile) -> None:
        self._geometry = geometry
        self._port = port

    def _parameters(self) -> dict[str, object]:
        return {
            "settle_s": VERIFY_SETTLE_S,
            "seated_max_xy_mm": SEATED_MAX_XY_MM,
            "ports": ports_config(),
        }

    async def run(self, ctx: SkillContext) -> SkillResult:
        steps = round(VERIFY_SETTLE_S / ctx.period_s)
        for i in range(steps):
            ctx.progress(i / steps)
            await ctx.clock.sleep(ctx.period_s)
        on_seat, height_mm = seated(self._geometry(), self._port)
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

    def _parameters(self) -> dict[str, object]:
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
