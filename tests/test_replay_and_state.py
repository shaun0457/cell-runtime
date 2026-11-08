"""Determinism, fold / state_at, and ending a run at the task time limit."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cellrt.eval.runner import run_unit
from cellrt.eval.scenarios import Scenario
from cellrt.runtime.events import Event
from cellrt.runtime.state import RunState, fold, state_at
from cellrt.runtime.store import EventStore
from cellrt.runtime.task import Task
from cellrt.skills.base import SkillContext, SkillResult
from cellrt.skills.mock import Align, Approach, InsertScripted, Verify
from cellrt.skills.registry import SkillRegistry


def _without_wall_time(events: list[Event]) -> list[dict[str, object]]:
    return [e.model_dump(exclude={"wall_ts"}) for e in events]


def test_same_scenario_and_seed_give_identical_logs(tmp_path: Path, baseline: Scenario) -> None:
    logs = []
    for name in ("first", "second"):
        with EventStore(tmp_path / f"{name}.sqlite") as store:
            run_id = run_unit(baseline, "A", seed=7, store=store).run_id
            logs.append(_without_wall_time(store.events(run_id)))

    assert logs[0] == logs[1]


def test_different_seeds_give_different_worlds(store: EventStore, baseline: Scenario) -> None:
    a = store.events(run_unit(baseline, "A", seed=1, store=store).run_id)
    b = store.events(run_unit(baseline, "A", seed=2, store=store).run_id)

    first_signal = [next(e.payload for e in log if e.type == "signal_sampled") for log in (a, b)]
    assert first_signal[0] != first_signal[1]


def test_state_can_be_rebuilt_at_any_step(store: EventStore, baseline: Scenario) -> None:
    events = store.events(run_unit(baseline, "A", seed=0, store=store).run_id)
    align_started = next(
        e for e in events if e.type == "skill_started" and e.payload.skill == "align"
    )

    at_align = state_at(events, align_started.seq)
    assert at_align.status == "running"
    assert at_align.current_skill == "align"
    assert dict(at_align.skill_attempts) == {"approach": 1, "align": 1}
    assert at_align.steps == 2

    final = state_at(events, events[-1].seq)
    assert final.status == "succeeded"
    assert final.current_skill is None
    assert final.steps == 4
    assert final == fold(events)


def test_rerunning_a_unit_in_the_same_store_starts_a_new_attempt(
    store: EventStore, baseline: Scenario
) -> None:
    first = run_unit(baseline, "A", seed=0, store=store)
    second = run_unit(baseline, "A", seed=0, store=store)

    assert (first.run_id, second.run_id) == ("baseline/A/0/a1", "baseline/A/0/a2")
    assert len(store.events(first.run_id)) == len(store.events(second.run_id))


@dataclass(frozen=True)
class _Exploding:
    name: str = "align"
    timeout_s: float = 5.0

    def preconditions(self, state: RunState) -> bool:
        return True

    async def run(self, ctx: SkillContext) -> SkillResult:
        await ctx.clock.sleep(ctx.period_s)
        raise ValueError("serial=SN-12345 user=shaun")


def test_a_skill_that_raises_fails_the_run_without_leaking_the_message(
    store: EventStore, baseline: Scenario
) -> None:
    registry = SkillRegistry([Approach(), _Exploding(), InsertScripted(), Verify()])

    result = run_unit(baseline, "A", seed=0, store=store, registry=registry)

    events = store.events(result.run_id)
    assert result.outcome == "failed"
    align = next(
        e.payload for e in events if e.type == "skill_finished" and e.payload.skill == "align"
    )
    assert (align.ok, dict(align.detail)) == (False, {"error": "ValueError"})
    assert events[-1].payload.reason == "align failed"
    assert not any("SN-12345" in e.model_dump_json() for e in events)


@dataclass(frozen=True)
class _FourSteps:
    name: str = "approach"
    timeout_s: float = 5.0

    def preconditions(self, state: RunState) -> bool:
        return True

    async def run(self, ctx: SkillContext) -> SkillResult:
        for _ in range(4):
            await ctx.clock.sleep(ctx.period_s)
        return SkillResult(ok=True)


def test_a_skill_that_finishes_just_before_the_time_limit_still_records_its_result(
    store: EventStore,
) -> None:
    # The skill returns during the drain after step 4; step 5 hits the 0.25 s limit before the
    # tree ticks again, so the result is only seen when the tree is stopped.
    short = Scenario(id="short", task=Task(time_limit=0.25))
    registry = SkillRegistry([_FourSteps(), Align(), InsertScripted(), Verify()])

    result = run_unit(short, "A", seed=0, store=store, registry=registry)

    events = store.events(result.run_id)
    assert result.outcome == "aborted"
    ends = [
        (e.type, e.payload.skill) for e in events if e.type in ("skill_finished", "skill_cancelled")
    ]
    assert ends == [("skill_finished", "approach")]
    assert fold(events).current_skill is None


def test_time_limit_terminates_the_running_skill_and_aborts(store: EventStore) -> None:
    short = Scenario(id="short", task=Task(time_limit=1.0))

    result = run_unit(short, "A", seed=0, store=store)

    events = store.events(result.run_id)
    assert result.outcome == "aborted"
    cancelled = [e.payload for e in events if e.type == "skill_cancelled"]
    assert [(c.skill, c.reason) for c in cancelled] == [("approach", "terminated")]
    finished = events[-1].payload
    assert (finished.outcome, finished.reason) == ("aborted", "time limit reached")
    assert finished.duration == 1.0
    assert fold(events).status == "aborted"
