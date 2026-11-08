"""Fault specs as written in scenario files: a kind plus parameter ranges.

`materialize` draws the concrete parameters from a RNG seeded by scenario and seed only, so every
setting faces the same fault.
"""

from __future__ import annotations

import random
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from cellrt.world.faults import ControllerStall, Fault

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


Range = Annotated[tuple[float, float], AfterValidator(_ordered)]
OnsetRange = Annotated[Range, _at_least(0.0)]
DurationRange = Annotated[Range, _at_least(MIN_FAULT_DURATION_S)]


class ControllerStallSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["controller_stall"] = "controller_stall"
    onset_s: OnsetRange = (0.5, 2.0)
    duration_s: DurationRange


FaultSpec = Annotated[ControllerStallSpec, Field(discriminator="kind")]


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
    return faults


def _draw(rng: random.Random, r: tuple[float, float]) -> float:
    return round(rng.uniform(*r), 3)
