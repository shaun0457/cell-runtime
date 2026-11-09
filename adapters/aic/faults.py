"""Faults injected into the AIC cell (#60), as ground truth for evaluation.

`command_dropout`: the link to the arm controller drops when the plug tip first comes within
`onset_height_mm` of the port, for `duration_s` of sim time. Every motion command in between is
lost (the executor's HOLD too), so the arm stays at its last target and the plug hangs where it
was. Nothing reaches the Monitor but what the cell measures: a plug that stops going down at low
force, which no rule names, so it is UNKNOWN (`insert_stalled`) if the dropout outlasts the
Monitor's stall window during insertion.

The onset is keyed to height, not to a skill: in the sim the plug seats during align (#28,
CheatCode's tip-offset quirk), so a fault keyed to insert_scripted would never fire. With the
default 20 mm the link drops half way down align; align runs its remaining targets into the
void and insert_scripted starts with the plug hanging.

`CELLRT_FAULT` names the fault as JSON, for example
`{"kind": "command_dropout", "onset_height_mm": 20, "duration_s": 15, "goals": [2]}`;
without `goals` every goal gets it. A bad value is refused with one fixed message
(`SPEC_REFUSED`) that never repeats what it refuses.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, ValidationError

from cellrt.executor.runner import STEP_TOLERANCE_S
from cellrt.monitor.classifier import FailureClass
from cellrt.world.base import FaultTransition

FAULT_ENV = "CELLRT_FAULT"
MAX_SPEC_CHARS = 1000
MAX_GOALS = 100
SPEC_REFUSED = (
    f"{FAULT_ENV} must be a JSON object: kind command_dropout, duration_s 0.05-120, optional"
    f" onset_height_mm 1-200 and goals (1-{MAX_GOALS} goal numbers from 1)"
)


class FaultSpecError(ValueError):
    """`CELLRT_FAULT` cannot be used. The message is always `SPEC_REFUSED`."""

    def __init__(self) -> None:
        super().__init__(SPEC_REFUSED)


@dataclass(frozen=True)
class CommandDropout:
    """The fault one goal gets (see the module docstring)."""

    fault_id: str
    onset_height_mm: float
    duration_s: float
    kind: str = "command_dropout"
    label: FailureClass = FailureClass.UNKNOWN

    @property
    def params(self) -> Mapping[str, Any]:
        return {"onset_height_mm": self.onset_height_mm, "duration_s": self.duration_s}

    def config(self) -> dict[str, Any]:
        """What the run's configuration hash covers."""
        return {"kind": self.kind, "label": str(self.label), **self.params}


class FaultSpec(BaseModel):
    """`CELLRT_FAULT`, validated: which fault, and the goals that get it (all if None).
    Strict: a number must be a JSON number and a goal a JSON integer, so `true`, `"15"` or
    `2.0` are refused rather than read as some other fault."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    kind: Literal["command_dropout"]
    onset_height_mm: float = Field(default=20.0, ge=1.0, le=200.0, allow_inf_nan=False)
    duration_s: float = Field(ge=0.05, le=120.0, allow_inf_nan=False)
    goals: tuple[PositiveInt, ...] | None = Field(default=None, min_length=1, max_length=MAX_GOALS)

    def for_goal(self, goal: int) -> CommandDropout | None:
        if self.goals is not None and goal not in self.goals:
            return None
        return CommandDropout(
            fault_id=self.kind, onset_height_mm=self.onset_height_mm, duration_s=self.duration_s
        )

    @classmethod
    def parse(cls, raw: str) -> FaultSpec:
        """Raises FaultSpecError for anything but a valid spec of at most MAX_SPEC_CHARS."""
        if len(raw) > MAX_SPEC_CHARS:
            raise FaultSpecError
        try:
            return cls.model_validate_json(raw)
        except (ValidationError, ValueError, RecursionError):
            raise FaultSpecError from None  # the input's text stays out of every message


class Dropout:
    """Runs one CommandDropout in the world. `observe` is called with every sample; a change
    decided at one sample applies to the commands sent until the next, and is reported then,
    as a transition during that step (FaultTransition's contract). So `fault_injected` follows
    the first sample within the onset height, and `fault_cleared` comes `duration_s` later. A
    change decided at a run's last sample is reported by no step: `flush` hands it over at the
    end of the run."""

    def __init__(self, fault: CommandDropout) -> None:
        self.fault = fault
        self._started_s: float | None = None
        self._active = False
        self._decided: list[FaultTransition] = []  # at the last sample
        self._ready: list[FaultTransition] = []  # for the runner

    @property
    def active(self) -> bool:
        return self._active

    def observe(self, sim_s: float, height_mm: float) -> None:
        self._ready.extend(self._decided)
        self._decided = []
        if self._started_s is None:
            if height_mm <= self.fault.onset_height_mm:
                self._started_s = sim_s
                self._active = True
                self._decided.append(FaultTransition(fault=self.fault, active=True))
        elif self._active and sim_s - self._started_s >= self.fault.duration_s - STEP_TOLERANCE_S:
            self._active = False
            self._decided.append(FaultTransition(fault=self.fault, active=False))

    def flush(self) -> None:
        self._ready.extend(self._decided)
        self._decided = []

    def take_transitions(self) -> list[FaultTransition]:
        ready, self._ready = self._ready, []
        return ready
