"""Failure paths of the skill contract, seen through the event log: timeouts, faults,
preconditions and the drain bound."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, fields

import pytest

from cellrt.eval.runner import run_unit
from cellrt.eval.scenarios import Scenario
from cellrt.executor.runner import DrainError
from cellrt.runtime.state import RunState, fold
from cellrt.runtime.store import EventStore
from cellrt.skills.base import SkillContext, SkillResult
from cellrt.skills.mock import Align, Approach, InsertScripted, Retract, Verify, WaitSensor
from cellrt.skills.registry import SkillRegistry
from cellrt.world.base import HOLD, Signals

STALL_FOREVER = {"kind": "controller_stall", "onset_s": [0.5, 0.5], "duration_s": [100, 100]}


SHORT_STALL = {"kind": "controller_stall", "onset_s": [0.5, 1.5], "duration_s": [1.0, 2.0]}


def _fault_events(store: EventStore, run_id: str) -> list[tuple[str, str, str, dict]]:
    return [
        (e.type, e.payload.fault_id, e.payload.label, dict(e.payload.params))
        for e in store.events(run_id)
        if e.type in ("fault_injected", "fault_cleared")
    ]


def test_fault_activation_and_clearing_are_recorded_with_the_ground_truth_label(
    store: EventStore,
) -> None:
    scenario = Scenario.model_validate({"id": "short_stall", "faults": [SHORT_STALL]})

    events = store.events(run_unit(scenario, "A", seed=3, store=store).run_id)

    injected = next(e for e in events if e.type == "fault_injected")
    cleared = next(e for e in events if e.type == "fault_cleared")
    params = injected.payload.params
    assert (injected.payload.fault_id, injected.payload.label) == ("controller_stall#0", "TIMEOUT")
    assert 0.5 <= params["onset_s"] <= 1.5
    assert 1.0 <= params["duration_s"] <= 2.0
    # Activation is seen at the first world step at or after the onset; clearing likewise.
    assert params["onset_s"] <= injected.ts < params["onset_s"] + 0.05
    end = params["onset_s"] + params["duration_s"]
    assert end <= cleared.ts < end + 0.05


def test_fault_parameters_depend_on_scenario_and_seed_only(tmp_path) -> None:
    scenario = Scenario.model_validate({"id": "short_stall", "faults": [SHORT_STALL]})
    draws = []
    for name, seed in (("a", 3), ("b", 3), ("c", 4)):
        with EventStore(tmp_path / f"{name}.sqlite") as store:
            draws.append(
                _fault_events(store, run_unit(scenario, "A", seed=seed, store=store).run_id)
            )

    assert draws[0] == draws[1]
    assert draws[0][0][3] != draws[2][0][3]


@dataclass
class _Probe:
    """Records what a skill can reach through its context, then succeeds."""

    seen: list[set[str]] = field(default_factory=list)
    name: str = "approach"
    timeout_s: float = 5.0

    def preconditions(self, state: RunState) -> bool:
        return True

    async def run(self, ctx: SkillContext) -> SkillResult:
        self.seen.append({a for a in dir(ctx.world) if not a.startswith("_")})
        return SkillResult(ok=True)


def test_skills_reach_the_world_only_through_signals_and_commands(store: EventStore) -> None:
    probe = _Probe()
    scenario = Scenario.model_validate({"id": "short_stall", "faults": [SHORT_STALL]})
    registry = SkillRegistry([probe, Align(), InsertScripted(), Verify()])

    run_unit(scenario, "A", seed=0, store=store, registry=registry)

    assert probe.seen == [{"signals", "command"}]
    assert {f.name for f in fields(Signals)} == {
        "height_mm",
        "xy_offset_mm",
        "force_n",
        "torque_nm",
        "wrench_valid",
    }


@dataclass(frozen=True)
class _NeverReady:
    name: str = "align"
    timeout_s: float = 5.0

    def preconditions(self, state: RunState) -> bool:
        return False

    async def run(self, ctx: SkillContext) -> SkillResult:  # pragma: no cover - never started
        raise AssertionError("a skill whose preconditions fail must not run")


def test_a_skill_whose_preconditions_fail_does_not_start(
    store: EventStore, baseline: Scenario
) -> None:
    registry = SkillRegistry([Approach(), _NeverReady(), InsertScripted(), Verify()])

    result = run_unit(baseline, "A", seed=0, store=store, registry=registry)

    events = store.events(result.run_id)
    started = [e.payload.skill for e in events if e.type == "skill_started"]
    refused = [e.payload for e in events if e.type == "precondition_failed"]
    assert started == ["approach"]
    assert [(p.skill, p.attempt) for p in refused] == [("align", 1)]
    assert result.outcome == "failed"
    assert events[-1].payload.reason == "align failed: preconditions not met"


@dataclass(frozen=True)
class _WaitsOnSomethingElse:
    name: str = "approach"
    timeout_s: float = 5.0

    def preconditions(self, state: RunState) -> bool:
        return True

    async def run(self, ctx: SkillContext) -> SkillResult:
        await (
            asyncio.Event().wait()
        )  # breaks the contract: blocks on something other than the clock
        return SkillResult(ok=True)


def test_a_skill_that_never_waits_on_the_clock_fails_the_run_loudly(
    store: EventStore, baseline: Scenario
) -> None:
    registry = SkillRegistry([_WaitsOnSomethingElse(), Align(), InsertScripted(), Verify()])

    with pytest.raises(DrainError, match="approach"):
        run_unit(baseline, "A", seed=0, store=store, registry=registry)

    run_id = "baseline/A/0/a1"
    types = [e.type for e in store.events(run_id)]
    assert "skill_started" in types
    assert "run_finished" not in types  # left incomplete: a resumed eval reruns it


def test_a_stalled_controller_times_out_the_running_skill(store: EventStore) -> None:
    stalled = Scenario.model_validate({"id": "stall", "faults": [STALL_FOREVER]})

    result = run_unit(stalled, "A", seed=0, store=store)

    events = store.events(result.run_id)
    cancelled = [(e.payload.skill, e.payload.reason) for e in events if e.type == "skill_cancelled"]
    verdicts = [
        (e.payload.skill, e.payload.failure_class) for e in events if e.type == "monitor_verdict"
    ]
    assert cancelled == [("approach", "timeout")]
    assert verdicts == [("approach", "TIMEOUT")]
    timeout_at = next(e.ts for e in events if e.type == "skill_cancelled")
    assert timeout_at == 5.0  # approach's timeout, in simulated seconds
    assert result.outcome == "failed"
    assert events[-1].payload.reason == "approach failed: TIMEOUT"
    assert fold(events).status == "failed"


@dataclass(frozen=True)
class _Idle:
    """Waits a fixed number of steps, then succeeds: shifts when the next skill starts."""

    steps: int
    name: str = "approach"
    timeout_s: float = 60.0

    def preconditions(self, state: RunState) -> bool:
        return True

    async def run(self, ctx: SkillContext) -> SkillResult:
        for _ in range(self.steps):
            await ctx.clock.sleep(ctx.period_s)
        return SkillResult(ok=True)


@pytest.mark.parametrize("idle_steps", range(55, 75))
def test_the_timeout_fires_exactly_timeout_s_after_the_skill_starts(
    tmp_path, idle_steps: int
) -> None:
    stalled = Scenario.model_validate({"id": "stall", "faults": [STALL_FOREVER]})
    registry = SkillRegistry([_Idle(idle_steps), Align(), InsertScripted(), Verify()])

    with EventStore(tmp_path / "events.sqlite") as store:
        events = store.events(run_unit(stalled, "A", seed=0, store=store, registry=registry).run_id)

    started = next(e for e in events if e.type == "skill_started" and e.payload.skill == "align")
    cancelled = next(e for e in events if e.type == "skill_cancelled")
    verdict = next(e.payload for e in events if e.type == "monitor_verdict")
    assert round(cancelled.ts - started.ts, 6) == 5.0
    assert verdict.evidence["elapsed_s"] == 5.0


@pytest.mark.parametrize(
    "bad",
    [
        {"onset_s": [-1.0, 1.0], "duration_s": [1.0, 2.0]},
        {"onset_s": [0.5, 1.0], "duration_s": [-1.0, 2.0]},
        {"onset_s": [0.5, 1.0], "duration_s": [0.01, 0.02]},
        {"onset_s": [1.0, 0.5], "duration_s": [1.0, 2.0]},
    ],
)
def test_fault_ranges_that_could_produce_no_fault_are_rejected(bad: dict) -> None:
    with pytest.raises(ValueError):
        Scenario.model_validate({"id": "x", "faults": [{"kind": "controller_stall", **bad}]})


def test_config_hash_changes_with_the_scenario_definition(store: EventStore) -> None:
    narrow = Scenario.model_validate({"id": "stall", "faults": [SHORT_STALL]})
    wide = Scenario.model_validate(
        {"id": "stall", "faults": [{**SHORT_STALL, "duration_s": [1.0, 9.0]}]}
    )

    hashes = []
    for scenario in (narrow, wide):
        events = store.events(run_unit(scenario, "A", seed=0, store=store).run_id)
        hashes.append(events[0].payload.config_hash)

    assert hashes[0] != hashes[1]


@dataclass(frozen=True)
class _GivesUp:
    """An align that reports failure on its own, optionally with a failure class."""

    failure: str | None = None
    name: str = "align"
    timeout_s: float = 5.0

    def preconditions(self, state: RunState) -> bool:
        return True

    async def run(self, ctx: SkillContext) -> SkillResult:
        await ctx.clock.sleep(ctx.period_s)
        return SkillResult(ok=False, failure=self.failure)


def _with_align(align: object) -> SkillRegistry:
    return SkillRegistry([Approach(), align, InsertScripted(), Verify(), Retract(), WaitSensor()])


@pytest.mark.parametrize("setting", ["A", "B"])
def test_a_skill_failure_without_a_class_is_reported_as_such_and_not_recovered(
    store: EventStore, baseline: Scenario, setting: str
) -> None:
    result = run_unit(baseline, setting, seed=0, store=store, registry=_with_align(_GivesUp()))

    events = store.events(result.run_id)
    assert result.outcome == "failed"
    assert events[-1].payload.reason == "align failed: skill reported failure"
    assert not any(e.type == "recovery_started" for e in events)


def test_a_class_reported_by_the_skill_is_recovered_and_labelled_as_coming_from_the_skill(
    store: EventStore, baseline: Scenario
) -> None:
    gives_up = _with_align(_GivesUp(failure="POSE_ERROR"))

    result = run_unit(baseline, "B", seed=0, store=store, registry=gives_up)

    events = store.events(result.run_id)
    recoveries = [
        (e.payload.failure_class, e.payload.source) for e in events if e.type == "recovery_started"
    ]
    assert recoveries == [("POSE_ERROR", "skill"), ("POSE_ERROR", "skill")]
    assert events[-1].payload.reason == (
        "align failed: POSE_ERROR (POSE_ERROR recovery budget used up)"
    )


def test_a_class_from_the_monitor_is_labelled_as_coming_from_the_monitor(store: EventStore) -> None:
    obstacle = Scenario.model_validate(
        {"id": "obstacle_once", "faults": [{"kind": "obstacle", "depth_mm": [3.0, 3.0]}]}
    )

    result = run_unit(obstacle, "B", seed=0, store=store)

    sources = [
        e.payload.source for e in store.events(result.run_id) if e.type == "recovery_started"
    ]
    assert sources == ["monitor"]


@dataclass(frozen=True)
class _DoneDuringDropout:
    """An approach that reports done after a few invalid sensor samples, just before the Monitor's
    dropout rule (5 samples, 0.2 s) would preempt it."""

    invalid_samples: int = 4
    name: str = "approach"
    timeout_s: float = 5.0

    def preconditions(self, state: RunState) -> bool:
        return True

    async def run(self, ctx: SkillContext) -> SkillResult:
        invalid = 0
        while invalid < self.invalid_samples:
            await ctx.clock.sleep(ctx.period_s)
            invalid = 0 if ctx.world.signals().wrench_valid else invalid + 1
        return SkillResult(ok=True)


def test_a_safety_verdict_on_the_tick_a_skill_completes_overrides_the_completion(
    store: EventStore,
) -> None:
    dropout = Scenario.model_validate(
        {
            "id": "dropout_early",
            "faults": [{"kind": "sensor_dropout", "onset_s": [0.5, 0.5], "duration_s": [1.0, 1.0]}],
        }
    )
    registry = SkillRegistry([_DoneDuringDropout(), Align(), InsertScripted(), Verify()])

    result = run_unit(dropout, "A", seed=0, store=store, registry=registry)

    events = store.events(result.run_id)
    verdicts = [
        (e.payload.skill, e.payload.failure_class) for e in events if e.type == "monitor_verdict"
    ]
    assert verdicts == [("approach", "SENSOR_DROPOUT")]
    # The skill did complete: its own result is still recorded, after the verdict, and it was
    # not cancelled.
    approach = [
        (e.type, getattr(e.payload, "ok", None))
        for e in events
        if e.type in ("monitor_verdict", "skill_finished", "skill_cancelled")
        and e.payload.skill == "approach"
    ]
    assert approach == [("monitor_verdict", None), ("skill_finished", True)]
    assert not any(e.type == "skill_started" and e.payload.skill == "align" for e in events)
    assert events[-1].payload.reason == "approach failed: SENSOR_DROPOUT"


@dataclass(frozen=True)
class _StallsThenGivesUp:
    """An insert that holds still and gives up on its own after `sleeps` periods: with 20 it
    gives up on the very tick the Monitor's stall rule (UNKNOWN) matches."""

    sleeps: int = 20
    name: str = "insert_scripted"
    timeout_s: float = 15.0

    def preconditions(self, state: RunState) -> bool:
        return True

    async def run(self, ctx: SkillContext) -> SkillResult:
        for _ in range(self.sleeps):
            ctx.world.command(HOLD)
            await ctx.clock.sleep(ctx.period_s)
        return SkillResult(ok=False)


def test_when_a_skill_gives_up_on_the_tick_of_a_verdict_the_monitor_class_is_used(
    store: EventStore, baseline: Scenario
) -> None:
    registry = SkillRegistry([Approach(), Align(), _StallsThenGivesUp(), Verify()])

    result = run_unit(baseline, "A", seed=0, store=store, registry=registry)

    events = store.events(result.run_id)
    insert = [
        (e.type, getattr(e.payload, "failure_class", getattr(e.payload, "ok", None)))
        for e in events
        if e.type in ("monitor_verdict", "skill_finished", "skill_cancelled")
        and e.payload.skill == "insert_scripted"
    ]
    assert insert == [("monitor_verdict", "UNKNOWN"), ("skill_finished", False)]
    assert events[-1].payload.reason == "insert_scripted failed: UNKNOWN"


@dataclass(frozen=True)
class _RaisesDuringDropout(_DoneDuringDropout):
    """Like `_DoneDuringDropout`, but raises instead of completing: on that tick the step has
    both a failure class (the verdict) and an error."""

    async def run(self, ctx: SkillContext) -> SkillResult:
        await super().run(ctx)
        raise ValueError("bug")


def test_a_skill_that_raises_on_the_tick_of_a_verdict_is_not_recovered(store: EventStore) -> None:
    dropout = Scenario.model_validate(
        {
            "id": "dropout_early",
            "faults": [{"kind": "sensor_dropout", "onset_s": [0.5, 0.5], "duration_s": [1.0, 1.0]}],
        }
    )
    registry = SkillRegistry(
        [_RaisesDuringDropout(), Align(), InsertScripted(), Verify(), Retract(), WaitSensor()]
    )

    result = run_unit(dropout, "B", seed=0, store=store, registry=registry)

    events = store.events(result.run_id)
    assert result.outcome == "aborted"
    assert events[-1].payload.reason == "approach raised ValueError"
    assert not any(e.type == "recovery_started" for e in events)
