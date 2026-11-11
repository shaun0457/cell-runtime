"""Each verdict of a sim run judged against the referee, at its time (ADR 029; ADR 026's
classes). Pure; imports nothing from ROS or `adapters/`.

The referee of a trial (`RefereeTrial`) is what the scorer saw, built from its files by
`adapters/aic/scoring.py`: the port of its last insertion event, the sim time of the insertion,
the off-limit contacts and the tier scores. The insertion's sim time maps onto the run clock
through the run's `world_ready` (#79): run time = sim time - `sim_origin_s`.

A verdict is a Monitor verdict, or a skill's own failed result in an attempt the Monitor gave no
verdict on (one failure, counted once). Each verdict at run time t is counted:

| The scorer | Counted as |
|---|---|
| Inserted at or before t + τ | a false alarm; within τ of t it is also a tie |
| Inserted after t + τ, a `recovery_started` after the verdict, over τ before it | recovered |
| Inserted after t + τ, no recovery in between | a false alarm, and a late insertion |
| Never inserted, or into another port | a real failure; unconfirmed when the run stopped at it |

The insertion's time is known only to within τ, so a recovery must start more than τ before it
to count: one that starts as the plug goes in did not put it in (against the Monitor, as a tie
is). "Inserted" means into the target port. When the insertion's time cannot be put on the run clock
(no mapping in the bag, or no `world_ready` origin), the trial falls back to ADR 015's rule and
is judged as a whole: every verdict of an inserted trial is a false alarm.

A real failure is unconfirmed when no recovery started after it: the run stopped there (in A,
always), so whether the plug would have gone in was never observed. ADR 026 names A; a B run
whose budget is spent stops the same way, and is marked the same way.

The trial's class is the first row of ADR 026's table that fits: false success, compensated,
adapter abort, then by its first verdict. A real failure is of a known class, UNKNOWN (the
Monitor's own class, the one C-LLM acts on), or unclassified: a skill's failed result with no
class, such as `verify`'s "not seated" or a skill that raised, which no setting recovers.
Infrastructure (no finished run, or no referee data) is the caller's to say: `judge` needs
both.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from cellrt.monitor.classifier import FailureClass
from cellrt.runtime.events import (
    Event,
    MonitorVerdict,
    RecoveryStarted,
    RunFinished,
    RunStarted,
    SkillFinished,
    SkillStarted,
    WorldReady,
)

Counted = Literal["false_alarm", "recovered", "real_failure"]

INFRASTRUCTURE = "infrastructure"
FALSE_SUCCESS = "false_success"
COMPENSATED = "compensated"
ADAPTER_ABORT = "adapter_abort"
FALSE_ALARM = "false_alarm"
RECOVERED = "recovered"
KNOWN_FAILURE = "natural_failure_known"
UNKNOWN_FAILURE = "natural_failure_unknown"
UNCLASSIFIED_FAILURE = "natural_failure_unclassified"
# ADR 026's classes, in its table's order
CLASSES = (
    INFRASTRUCTURE,
    FALSE_SUCCESS,
    COMPENSATED,
    ADAPTER_ABORT,
    FALSE_ALARM,
    RECOVERED,
    KNOWN_FAILURE,
    UNKNOWN_FAILURE,
    UNCLASSIFIED_FAILURE,
)
KNOWN_CLASSES = frozenset(c.value for c in FailureClass if c is not FailureClass.UNKNOWN)
# A skill's failed result without a class is not a known class either.
UNCLASSIFIED = "unclassified"


def port_parts(name: str) -> tuple[str, ...]:
    """An insertion event's port name as the scorer splits it: on `/`, empty parts dropped
    (`ScoringTier2.cc:812-837`), so a leading `/` does not matter."""
    return tuple(part for part in name.split("/") if part)


@dataclass(frozen=True)
class RefereeTrial:
    """What the scorer saw in one trial (ADR 029)."""

    target: tuple[str, str]  # (target module, port)
    last_port: tuple[str, ...] | None  # the last insertion event's port, split; None: no event
    # The sim time of the insertion into the target and its tolerance τ; None when there was no
    # insertion into the target, or when its receive time could not be mapped onto sim time.
    insertion_sim_s: float | None
    tolerance_s: float | None
    contact_messages: int  # off-limit contact messages with at least one contact (the bag)
    contacts_score: float | None  # the scorer's tier-2 contacts score
    force_penalty: float | None  # the scorer's tier-2 insertion-force score
    tier3: float | None
    total: float | None

    @property
    def inserted(self) -> bool:
        """Into the target port, as the scorer scores it: its last event's first two parts."""
        return self.last_port is not None and self.last_port[:2] == self.target

    @property
    def wrong_port(self) -> bool:
        return self.last_port is not None and not self.inserted


@dataclass(frozen=True)
class JudgedVerdict:
    index: int  # the event's position in the run
    ts: float  # run-clock time
    skill: str
    failure_class: str
    source: Literal["monitor", "skill"]
    counted: Counted
    tie: bool = False  # a false alarm with the insertion within τ of the verdict
    late: bool = False  # a false alarm by a late insertion that nothing acted on
    unconfirmed: bool = False  # a real failure the run stopped at


@dataclass(frozen=True)
class JudgedTrial:
    run_id: str
    scenario_id: str
    setting: str
    seed: int
    outcome: str
    trial_class: str
    verdicts: tuple[JudgedVerdict, ...]
    inserted: bool
    wrong_port: bool
    insertion_t: float | None  # run-clock time of the insertion, when it could be mapped
    fallback: bool  # judged as a whole: an insertion whose time could not be mapped


def judge(events: Sequence[Event], referee: RefereeTrial) -> JudgedTrial:
    """One finished run's verdicts and class against its referee (ValueError if the run did not
    finish: an unfinished run is infrastructure, the caller's case)."""
    if not events or not isinstance(events[0].payload, RunStarted):
        raise ValueError("a judged run starts with run_started")
    end = events[-1].payload
    if not isinstance(end, RunFinished):
        raise ValueError("a judged run ends with run_finished")
    started = events[0].payload
    raw, recoveries, origin = _scan(events)
    insertion_t = None
    if referee.inserted and referee.insertion_sim_s is not None and origin is not None:
        insertion_t = referee.insertion_sim_s - origin
    fallback = referee.inserted and insertion_t is None
    judged = tuple(
        _count(v, referee.inserted, insertion_t, referee.tolerance_s or 0.0, recoveries)
        for v in raw
    )
    return JudgedTrial(
        run_id=events[0].run_id,
        scenario_id=started.scenario_id,
        setting=started.setting,
        seed=started.seed,
        outcome=end.outcome,
        trial_class=_class(end.outcome, referee, judged),
        verdicts=judged,
        inserted=referee.inserted,
        wrong_port=referee.wrong_port,
        insertion_t=insertion_t,
        fallback=fallback,
    )


@dataclass(frozen=True)
class _Verdict:
    index: int
    ts: float
    skill: str
    failure_class: str
    source: Literal["monitor", "skill"]


def _scan(events: Sequence[Event]) -> tuple[list[_Verdict], list[tuple[int, float]], float | None]:
    verdicts: list[_Verdict] = []
    recoveries: list[tuple[int, float]] = []
    origin: float | None = None
    seen_origin = False
    judged_attempt: dict[str, bool] = {}  # skill -> its latest attempt already has a verdict
    for i, event in enumerate(events):
        match event.payload:
            case WorldReady(sim_origin_s=at) if not seen_origin:
                origin, seen_origin = at, True
            case SkillStarted(skill=skill):
                judged_attempt[skill] = False
            case MonitorVerdict(skill=skill, failure_class=cls):
                verdicts.append(_Verdict(i, event.ts, skill, cls, "monitor"))
                judged_attempt[skill] = True
            case SkillFinished(skill=skill, ok=False, failure=cls) if not judged_attempt.get(skill):
                verdicts.append(_Verdict(i, event.ts, skill, cls or UNCLASSIFIED, "skill"))
                judged_attempt[skill] = True
            case RecoveryStarted():
                recoveries.append((i, event.ts))
            case _:
                pass
    return verdicts, recoveries, origin


def _count(
    v: _Verdict,
    inserted: bool,
    insertion_t: float | None,
    tau: float,
    recoveries: Sequence[tuple[int, float]],
) -> JudgedVerdict:
    counted: Counted = "false_alarm"
    tie = late = unconfirmed = False
    if not inserted:
        counted = "real_failure"
        unconfirmed = not any(i > v.index for i, _ in recoveries)
    elif insertion_t is None:  # the fallback: an inserted trial's verdicts are false alarms
        pass
    elif insertion_t <= v.ts + tau:
        tie = abs(insertion_t - v.ts) <= tau
    elif any(i > v.index and ts < insertion_t - tau for i, ts in recoveries):
        counted = "recovered"
    else:
        late = True
    return JudgedVerdict(
        index=v.index,
        ts=v.ts,
        skill=v.skill,
        failure_class=v.failure_class,
        source=v.source,
        counted=counted,
        tie=tie,
        late=late,
        unconfirmed=unconfirmed,
    )


def _class(outcome: str, referee: RefereeTrial, verdicts: Sequence[JudgedVerdict]) -> str:
    if outcome == "success" and not referee.inserted:
        return FALSE_SUCCESS
    if not verdicts:
        return COMPENSATED if outcome == "success" else ADAPTER_ABORT
    first = verdicts[0]
    if first.counted == "false_alarm":
        return FALSE_ALARM
    if first.counted == "recovered":
        return RECOVERED
    if first.failure_class in KNOWN_CLASSES:
        return KNOWN_FAILURE
    if first.failure_class == FailureClass.UNKNOWN.value:
        return UNKNOWN_FAILURE
    return UNCLASSIFIED_FAILURE
