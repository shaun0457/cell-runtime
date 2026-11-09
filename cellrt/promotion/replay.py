"""Run the Monitor again over a stored run, as the run judged it (ADR 022).

The live loop logs a sample after each world step and judges it on the next tick, for the skill
running then (or, in a live planner wait, for the hold); a skill's first tick only starts it.
Replay follows the same order over the log: it resets the Monitor on `skill_started` (with the
skill's profile) and, in a live wait only, on `planner_called` (with the hold profile); it feeds
the Monitor every sample logged while a skill or a live planner call is open, and takes the force
time from the fold, as the run did. In a frozen wait the loop never judges the hold, although a
planner that answers in its first step still lets the world step once. The one sample no tick
followed is the one that reached the task's time limit: the loop stops right after logging it.
A run stopped because the world stopped judged its last sample before the failed step.

Gate 0 is that the replay reproduces the record: every verdict the run recorded comes back on
the same sample, with the same class, rule and evidence; a verdict that lost to the skill's own
success and a skill timeout (decided by the clock, not by a sample) are accounted for.

With a candidate rule the replay compares nothing: it returns every verdict the Monitor gives
with the rule added, over the same samples and windows, for the gates in `validate`.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from cellrt.eval.runner import monitor_inputs, part_digest
from cellrt.executor.behaviours import HOLD_PROFILE, PlannerWait
from cellrt.executor.samples import signals_of
from cellrt.monitor.classifier import (
    SAFETY_CLASSES,
    FailureClass,
    Monitor,
    MonitorConfig,
    Verdict,
)
from cellrt.monitor.profiles import MonitorProfile
from cellrt.monitor.promoted import PromotedRule
from cellrt.runtime.events import (
    Event,
    MonitorVerdict,
    PlannerCalled,
    PlannerDecided,
    PlannerOutputRejected,
    PlannerPreempted,
    PlannerTimedOut,
    RunFinished,
    RunStarted,
    SignalSampled,
    SkillCancelled,
    SkillFinished,
    SkillStarted,
)
from cellrt.runtime.state import RunState, apply
from cellrt.skills.mock import mock_registry
from cellrt.skills.registry import SkillRegistry
from cellrt.world.mock_world import MockParams

# The parts of `config_parts` a replay depends on (`eval.runner.monitor_inputs`): the world (its
# period), the Monitor (its thresholds) and the skills (the profile each declares).
REPLAY_PARTS = tuple(monitor_inputs(MockParams(), MonitorConfig(), mock_registry()))

# `replayed`: a verdict of a replay with a candidate, where nothing is compared with the record.
Outcome = Literal["recorded", "lost_to_success", "replayed"]
Status = Literal["ok", "mismatch", "config_mismatch", "unfinished", "unjudgeable"]

_PLANNER_ENDS = (PlannerDecided, PlannerOutputRejected, PlannerTimedOut, PlannerPreempted)


@dataclass(frozen=True)
class ReplayConfig:
    """The configuration to replay with, and the hashes a run must carry to be replayed with
    it: a run stores only hashes, so the values come from the caller and are checked."""

    monitor: MonitorConfig
    period_s: float
    profiles: Mapping[str, MonitorProfile]
    parts: Mapping[str, str]
    # Whether the Monitor watched the hold during a planner call (ADR 020). It sits in the
    # planner part of the hash with the planner's own settings, so it is taken on trust here; a
    # wrong mode shows as a gate 0 mismatch once a verdict falls in a planner wait.
    planner_wait: PlannerWait = "frozen"

    @classmethod
    def mock(
        cls,
        *,
        monitor: MonitorConfig | None = None,
        params: MockParams | None = None,
        registry: SkillRegistry | None = None,
        planner_wait: PlannerWait = "frozen",
    ) -> ReplayConfig:
        """The mock eval's configuration; the defaults are the eval's defaults (the mock always
        waits `frozen`)."""
        monitor = monitor or MonitorConfig()
        params = params or MockParams()
        registry = registry or mock_registry()
        inputs = monitor_inputs(params, monitor, registry)
        return cls(
            monitor=monitor,
            period_s=params.dt,
            profiles=MappingProxyType(
                {n: registry.get(n).monitor_profile for n in sorted(registry.names())}
            ),
            parts=MappingProxyType({name: part_digest(value) for name, value in inputs.items()}),
            planner_wait=planner_wait,
        )


@dataclass(frozen=True)
class Judged:
    """A verdict the replayed Monitor returned, and what became of it in the run."""

    seq: int  # the judged sample's event
    step: int
    skill: str
    attempt: int | None  # None: the hold during a live planner call
    verdict: Verdict
    outcome: Outcome
    window: int  # which watch window of the run, counted from 0 (a skill attempt or a hold)


@dataclass(frozen=True)
class Mismatch:
    """Where the replay and the record first disagree."""

    seq: int
    recorded: str
    replayed: str


@dataclass(frozen=True)
class RunReplay:
    run_id: str
    status: Status
    judged: tuple[Judged, ...] = ()
    mismatch: Mismatch | None = None
    detail: str = ""  # for `config_mismatch`: the first part whose hash differs
    timeouts: int = 0  # skill timeouts the run recorded: decided by the clock, not replayed


def replay_run(
    events: Sequence[Event], config: ReplayConfig, *, candidate: PromotedRule | None = None
) -> RunReplay:
    """Replay the Monitor over one run's events and check the record (gate 0).

    With `candidate`, the Monitor gets the rule after the configured ones and the replay keeps,
    without comparing, the first verdict of each window (`replayed`) and one on the tick the
    watched skill succeeded (`lost_to_success`); the run must still carry `config`'s hashes,
    since its samples are replayed as that configuration judged them."""
    run_id = events[0].run_id if events else ""
    started = events[0].payload if events else None
    if not isinstance(started, RunStarted) or not isinstance(events[-1].payload, RunFinished):
        return RunReplay(run_id, "unfinished")
    for part in REPLAY_PARTS:
        if started.config_parts.get(part) != config.parts[part]:
            return RunReplay(run_id, "config_mismatch", detail=part)
    samples = [e for e in events if isinstance(e.payload, SignalSampled)]
    if any(e.payload.xy_offset_mm is None for e in samples):  # type: ignore[union-attr]
        return RunReplay(run_id, "unjudgeable")
    # The loop checks the time limit right after logging a sample, before the next tick.
    unjudged = samples[-1].seq if samples and samples[-1].ts >= started.task.time_limit else None
    if candidate is None:
        return _Replay(config, unjudged).run(run_id, events)
    monitor = dataclasses.replace(config.monitor, promoted=(*config.monitor.promoted, candidate))
    return _Replay(dataclasses.replace(config, monitor=monitor), unjudged, strict=False).run(
        run_id, events
    )


@dataclass
class _Tick:
    """What one tick did: the sample it judged, the replay's verdict on it, and what the run
    recorded in that tick."""

    sample: Event | None = None
    watching: tuple[str, int | None] | None = None
    window: int = -1
    verdict: Verdict | None = None
    recorded: list[tuple[int, MonitorVerdict]] = field(default_factory=list)
    finished_ok: bool = False


class _Replay:
    def __init__(self, config: ReplayConfig, unjudged: int | None, *, strict: bool = True) -> None:
        self._state = RunState()
        self._monitor = Monitor(
            config.period_s, config.monitor, force_time=lambda: self._state.over_limit_s
        )
        self._profiles = config.profiles
        self._live = config.planner_wait == "live"
        self._unjudged = unjudged
        self._strict = strict
        self._watching: tuple[str, int | None] | None = None
        self._window = -1  # the current or last watch window
        self._tick = _Tick()
        self._judged: list[Judged] = []
        self._replayed_windows: set[int] = set()  # windows with a kept verdict (not strict)
        self._timeouts = 0

    def run(self, run_id: str, events: Sequence[Event]) -> RunReplay:
        for event in events:
            self._state = apply(self._state, event)
            payload = event.payload
            if isinstance(payload, SignalSampled):
                if mismatch := self._settle():
                    return RunReplay(run_id, "mismatch", tuple(self._judged), mismatch)
                self._judge(event, payload)
                continue
            self._note(event)
            self._follow(payload)
        if mismatch := self._settle():
            return RunReplay(run_id, "mismatch", tuple(self._judged), mismatch)
        return RunReplay(run_id, "ok", tuple(self._judged), timeouts=self._timeouts)

    def _judge(self, event: Event, sample: SignalSampled) -> None:
        verdict = None
        if self._watching is not None and event.seq != self._unjudged:
            verdict = self._monitor.observe(signals_of(sample))
        self._tick = _Tick(
            sample=event, watching=self._watching, window=self._window, verdict=verdict
        )

    def _note(self, event: Event) -> None:
        """Record what the run did in the current tick."""
        payload, tick = event.payload, self._tick
        if isinstance(payload, MonitorVerdict):
            tick.recorded.append((event.seq, payload))
        elif (
            isinstance(payload, SkillFinished)
            and payload.ok
            and tick.watching is not None
            and tick.watching[0] == payload.skill
        ):
            tick.finished_ok = True

    def _follow(self, payload: object) -> None:
        """Open and close what the Monitor watches, as the run did."""
        if isinstance(payload, SkillStarted):
            self._watching = (payload.skill, payload.attempt)
            self._window += 1
            self._monitor.reset(self._profiles[payload.skill], skill=payload.skill)
        elif isinstance(payload, PlannerCalled):
            # Only a live wait watches the hold. In a frozen one nothing is judged until the
            # next skill starts, though the world may step once for a planner that answers at
            # once (the rule planner).
            if self._live:
                self._watching = (payload.skill, None)
                self._window += 1
                self._monitor.reset(HOLD_PROFILE)
            else:
                self._watching = None
        elif self._watching is not None:
            in_hold = self._watching[1] is None
            skill_ended = isinstance(payload, (SkillFinished, SkillCancelled))
            if (skill_ended and not in_hold) or (isinstance(payload, _PLANNER_ENDS) and in_hold):
                self._watching = None

    def _settle(self) -> Mismatch | None:
        """Compare the replay's verdict on the last judged sample with what that tick recorded."""
        tick, self._tick = self._tick, _Tick()
        verdict = tick.verdict
        if not self._strict:
            if verdict is not None:
                lost = tick.finished_ok and verdict.failure_class not in SAFETY_CLASSES
                outcome: Outcome = "lost_to_success" if lost else "replayed"
                # Only a window's first verdict counts (validate), so a rule that matches every
                # sample keeps one verdict per window, not one per sample. A lost one ends the
                # window (the skill succeeded), so there is at most one of those.
                if lost or tick.window not in self._replayed_windows:
                    self._replayed_windows.add(tick.window)
                    self._judged.append(self._judged_of(tick, verdict, outcome))
            return None
        if len(tick.recorded) > 1:  # the loop records at most one verdict per tick
            seq, second = tick.recorded[1]
            return Mismatch(seq, recorded=_name(second), replayed="one verdict per tick")
        recorded = tick.recorded[0] if tick.recorded else None
        if verdict is not None and recorded is not None:
            seq, r = recorded
            if _key(verdict) != (r.failure_class, r.rule, _plain(r.evidence)):
                replayed = _named(verdict)
                if replayed == _name(r):  # same class and rule: the evidence differs
                    replayed += " (other evidence)"
                return Mismatch(seq, recorded=_name(r), replayed=replayed)
            self._judged.append(self._judged_of(tick, verdict, "recorded"))
        elif verdict is not None:
            if tick.finished_ok and verdict.failure_class not in SAFETY_CLASSES:
                self._judged.append(self._judged_of(tick, verdict, "lost_to_success"))
            else:
                assert tick.sample is not None
                return Mismatch(tick.sample.seq, recorded="none", replayed=_named(verdict))
        elif recorded is not None:
            if not _timeout_of(recorded[1], tick.watching):
                return Mismatch(recorded[0], recorded=_name(recorded[1]), replayed="none")
            self._timeouts += 1
        return None

    @staticmethod
    def _judged_of(tick: _Tick, verdict: Verdict, outcome: Outcome) -> Judged:
        assert tick.sample is not None and tick.watching is not None
        sample = tick.sample.payload
        assert isinstance(sample, SignalSampled)
        skill, attempt = tick.watching
        return Judged(tick.sample.seq, sample.step, skill, attempt, verdict, outcome, tick.window)


def _timeout_of(recorded: MonitorVerdict, watching: tuple[str, int | None] | None) -> bool:
    """A skill timeout, decided by the clock, for the skill the tick was watching."""
    return (
        recorded.failure_class == FailureClass.TIMEOUT
        and watching is not None
        and watching[1] is not None
        and watching[0] == recorded.skill
    )


def _plain(value: Any) -> Any:
    """As stored: tuples become lists."""
    return json.loads(json.dumps(value))


def _key(v: Verdict) -> tuple[str, str, Any]:
    return (v.failure_class.value, v.rule, _plain(v.evidence))


def _name(r: MonitorVerdict) -> str:
    return f"{r.failure_class}/{r.rule}"


def _named(v: Verdict) -> str:
    return f"{v.failure_class.value}/{v.rule}"
