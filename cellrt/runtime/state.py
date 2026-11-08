"""Runtime state is a pure fold over the event log."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Literal

from cellrt.runtime.events import (
    Event,
    PlannerCalled,
    RecoveryStarted,
    RunFinished,
    RunStarted,
    SignalSampled,
    SkillCancelled,
    SkillFinished,
    SkillStarted,
)

# AIC: tared force above this counts toward the force penalty (`ScoringTier2.cc`). A fact of
# the scorer, not a tuning knob: the Monitor and the metrics both read the folded sum.
FORCE_LIMIT_N = 20.0

Status = Literal["pending", "running", "succeeded", "failed", "aborted"]
_OUTCOME_STATUS: Mapping[str, Status] = {
    "success": "succeeded",
    "failed": "failed",
    "aborted": "aborted",
}


@dataclass(frozen=True)
class RunState:
    run_id: str | None = None
    status: Status = "pending"
    current_skill: str | None = None
    skill_attempts: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    steps: int = 0
    # Recoveries started so far: the budgets of settings B and C are checked against these.
    recoveries_by_class: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    recoveries_total: int = 0
    # Planner calls so far: setting C's planner budget is checked against this.
    planner_calls: int = 0
    last_signal: SignalSampled | None = None
    last_sample_ts: float | None = None
    # Time with a valid tared force over FORCE_LIMIT_N, summed over the task as the AIC penalty
    # sums it: each sample stands for the interval since the one before (or the run start).
    over_limit_s: float = 0.0
    started_ts: float | None = None
    finished_ts: float | None = None


def apply(state: RunState, event: Event) -> RunState:
    """The next state after `event`. Pure: no I/O, no clock, `state` is not modified."""
    match event.payload:
        case RunStarted():
            return replace(state, run_id=event.run_id, status="running", started_ts=event.ts)
        case SkillStarted(skill=skill):
            attempts = dict(state.skill_attempts)
            attempts[skill] = attempts.get(skill, 0) + 1
            return replace(
                state,
                current_skill=skill,
                skill_attempts=MappingProxyType(attempts),
                steps=state.steps + 1,
            )
        case RecoveryStarted(failure_class=failure_class):
            counts = dict(state.recoveries_by_class)
            counts[failure_class] = counts.get(failure_class, 0) + 1
            return replace(
                state,
                recoveries_by_class=MappingProxyType(counts),
                recoveries_total=state.recoveries_total + 1,
            )
        case PlannerCalled():
            return replace(state, planner_calls=state.planner_calls + 1)
        case SkillFinished() | SkillCancelled():
            return replace(state, current_skill=None)
        case SignalSampled() as signal:
            over_limit_s = state.over_limit_s
            if signal.wrench_valid and signal.force_n > FORCE_LIMIT_N:
                since = state.last_sample_ts
                if since is None:
                    since = state.started_ts if state.started_ts is not None else event.ts
                over_limit_s = round(over_limit_s + event.ts - since, 9)
            return replace(
                state, last_signal=signal, last_sample_ts=event.ts, over_limit_s=over_limit_s
            )
        case RunFinished(outcome=outcome):
            return replace(
                state,
                status=_OUTCOME_STATUS[outcome],
                current_skill=None,
                finished_ts=event.ts,
            )
        case _:
            return state


def fold(events: Iterable[Event]) -> RunState:
    state = RunState()
    for event in events:
        state = apply(state, event)
    return state


def state_at(events: Sequence[Event], seq: int) -> RunState:
    """State right after the event numbered `seq` (the basis for a future fork-from-step-N)."""
    return fold(e for e in events if e.seq <= seq)
