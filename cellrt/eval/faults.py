"""Fault specs as written in scenario files: a kind plus parameter ranges.

`materialize` draws the concrete parameters from a RNG seeded by scenario and seed only, so every
setting faces the same fault.
"""

from __future__ import annotations

import math
import random
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, FiniteFloat

from cellrt.world.faults import (
    ControllerStall,
    Fault,
    Obstacle,
    PoseShift,
    SensorDropout,
    TightFit,
    YawMismatch,
)

# A fault shorter than this could fall between two world steps (20 Hz) and never take effect.
MIN_FAULT_DURATION_S = 0.1


def _ordered(r: tuple[float, float]) -> tuple[float, float]:
    if r[0] > r[1]:
        raise ValueError(f"range low {r[0]} is above high {r[1]}")
    return r


def _at_least(minimum: float) -> AfterValidator:
    def check(r: tuple[float, float]) -> tuple[float, float]:
        if r[0] < minimum:
            raise ValueError(f"range low {r[0]} is below the minimum {minimum}")
        return r

    return AfterValidator(check)


# Finite bounds: JSON has no inf or nan, so a drawn value must be one the log can hold.
Range = Annotated[tuple[FiniteFloat, FiniteFloat], AfterValidator(_ordered)]
OnsetRange = Annotated[Range, _at_least(0.0)]
DurationRange = Annotated[Range, _at_least(MIN_FAULT_DURATION_S)]
DepthRange = Annotated[Range, _at_least(0.0)]
MagnitudeRange = Annotated[Range, _at_least(0.0)]
# How many insertion attempts a fault affects (drawn as a whole number).
AttemptsRange = Annotated[tuple[int, int], AfterValidator(_ordered), _at_least(1)]


class ControllerStallSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["controller_stall"] = "controller_stall"
    onset_s: OnsetRange = (0.5, 2.0)
    duration_s: DurationRange


class SensorDropoutSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["sensor_dropout"] = "sensor_dropout"
    onset_s: OnsetRange
    duration_s: DurationRange


class ObstacleSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["obstacle"] = "obstacle"
    depth_mm: DepthRange
    attempts: AttemptsRange = (1, 1)


class TightFitSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["tight_fit"] = "tight_fit"
    onset_depth_mm: DepthRange
    force_n_per_mm_s: MagnitudeRange


class PoseShiftSpec(BaseModel):
    """The shift direction is drawn uniformly; only its size is a range."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["pose_shift"] = "pose_shift"
    trigger_height_mm: DepthRange
    shift_mm: MagnitudeRange
    attempts: AttemptsRange = (1, 1)


class YawMismatchSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["yaw_mismatch"] = "yaw_mismatch"
    stall_depth_mm: DepthRange
    force_n: MagnitudeRange
    torque_nm: MagnitudeRange
    yaw_deg: MagnitudeRange


FaultSpec = Annotated[
    ControllerStallSpec
    | SensorDropoutSpec
    | ObstacleSpec
    | TightFitSpec
    | PoseShiftSpec
    | YawMismatchSpec,
    Field(discriminator="kind"),
]


def materialize(specs: list[FaultSpec], rng: random.Random) -> list[Fault]:
    faults: list[Fault] = []
    for index, spec in enumerate(specs):
        fault_id = f"{spec.kind}#{index}"
        match spec:
            case ControllerStallSpec():
                faults.append(
                    ControllerStall(
                        fault_id=fault_id,
                        onset_s=_draw(rng, spec.onset_s),
                        duration_s=_draw(rng, spec.duration_s),
                    )
                )
            case SensorDropoutSpec():
                faults.append(
                    SensorDropout(
                        fault_id=fault_id,
                        onset_s=_draw(rng, spec.onset_s),
                        duration_s=_draw(rng, spec.duration_s),
                    )
                )
            case ObstacleSpec():
                faults.append(
                    Obstacle(
                        fault_id=fault_id,
                        depth_mm=_draw(rng, spec.depth_mm),
                        attempts=rng.randint(*spec.attempts),
                    )
                )
            case TightFitSpec():
                faults.append(
                    TightFit(
                        fault_id=fault_id,
                        onset_depth_mm=_draw(rng, spec.onset_depth_mm),
                        force_n_per_mm_s=_draw(rng, spec.force_n_per_mm_s),
                    )
                )
            case PoseShiftSpec():
                faults.append(
                    PoseShift(
                        fault_id=fault_id,
                        trigger_height_mm=_draw(rng, spec.trigger_height_mm),
                        shift_mm=_draw(rng, spec.shift_mm),
                        angle_rad=_draw(rng, (0.0, 2 * math.pi)),
                        attempts=rng.randint(*spec.attempts),
                    )
                )
            case YawMismatchSpec():
                faults.append(
                    YawMismatch(
                        fault_id=fault_id,
                        stall_depth_mm=_draw(rng, spec.stall_depth_mm),
                        force_n=_draw(rng, spec.force_n),
                        torque_nm=_draw(rng, spec.torque_nm),
                        yaw_deg=_draw(rng, spec.yaw_deg),  # drawn last: earlier draws unchanged
                    )
                )
    return faults


def _draw(rng: random.Random, r: tuple[float, float]) -> float:
    return round(rng.uniform(*r), 3)
