"""The world protocol the AIC adapter needs, with every mock result unchanged:

- a command is a velocity or a pose target; the mock world accepts velocities only;
- a world step reports the time that passed, and the clock advances by it;
- a sample may carry the time over the force limit within its step (the AIC scorer's sum over
  the F/T stream), and the fold, the Monitor and the unsafe metric use it.
"""

from __future__ import annotations

import dataclasses
import itertools
import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from cellrt.eval import runner
from cellrt.eval.metrics import summarize_run
from cellrt.eval.runner import run_unit
from cellrt.eval.scenarios import Scenario, load_scenarios
from cellrt.executor.samples import sampled, signals_of
from cellrt.monitor.classifier import FailureClass, Monitor, Verdict
from cellrt.runtime.events import (
    SCHEMA_VERSION,
    Event,
    MonitorVerdict,
    RunFinished,
    RunStarted,
    SignalSampled,
    SkillCancelled,
    SkillFinished,
)
from cellrt.runtime.state import RunState, apply, fold
from cellrt.runtime.store import EventStore
from cellrt.runtime.task import Task
from cellrt.skills.base import SkillContext, SkillResult
from cellrt.skills.mock import Align, InsertAct, InsertScripted, Retract, Verify, WaitSensor
from cellrt.skills.registry import SkillRegistry
from cellrt.world.base import PoseTarget, Signals
from cellrt.world.mock_world import MockWorld

PHASE0 = Path(__file__).resolve().parents[1] / "eval" / "scenarios" / "phase0.yaml"
SCENARIOS: dict[str, Scenario] = {s.id: s for s in load_scenarios(PHASE0)}


# --- commands --------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class PoseApproach:
    """An approach that commands a pose target, as the AIC scripted skills will."""

    name: str = "approach"
    monitor_profile: str = "motion"
    timeout_s: float = 5.0

    def preconditions(self, state: RunState) -> bool:
        return True

    async def run(self, ctx: SkillContext) -> SkillResult:
        ctx.world.command(PoseTarget(x_mm=0.0, y_mm=0.0, z_mm=200.0))
        await ctx.clock.sleep(ctx.period_s)
        return SkillResult(ok=True)


def test_a_pose_target_sent_to_the_mock_world_is_recorded_as_a_skill_error(
    store: EventStore,
) -> None:
    skills = [PoseApproach(), Align(), InsertScripted(), InsertAct(), Verify(), Retract(),
              WaitSensor()]  # fmt: skip
    done = run_unit(SCENARIOS["baseline"], "A", 0, store, registry=SkillRegistry(skills))
    events = store.events(done.run_id)

    finished = [e.payload for e in events if isinstance(e.payload, SkillFinished)]
    assert finished[0].skill == "approach" and not finished[0].ok
    assert finished[0].detail == {"error": "UnsupportedCommand"}
    end = events[-1].payload
    assert isinstance(end, RunFinished)
    assert end.outcome == "aborted" and end.reason == "approach raised UnsupportedCommand"


def test_a_pose_target_keeps_position_and_orientation() -> None:
    target = PoseTarget(x_mm=1.0, y_mm=2.0, z_mm=3.0, qw=0.0, qx=1.0, qy=0.0, qz=0.0)

    assert (target.x_mm, target.y_mm, target.z_mm) == (1.0, 2.0, 3.0)
    assert (target.qw, target.qx, target.qy, target.qz) == (0.0, 1.0, 0.0, 0.0)
    assert PoseTarget(x_mm=0.0, y_mm=0.0, z_mm=0.0).qw == 1.0  # identity by default


# --- elapsed time ----------------------------------------------------------------------------


class _SlowMockWorld(MockWorld):
    """A world whose steps take twice the period, like a sim that cannot keep up."""

    def step(self, dt: float) -> float:
        super().step(dt)
        return 2 * dt


class _ShortStepMockWorld(MockWorld):
    """A world whose step reports `reported` seconds, whatever it was asked."""

    reported = 0.0

    def step(self, dt: float) -> float:
        super().step(dt)
        return self.reported


def _run(store: EventStore, scenario: str, seed: int = 0) -> list[Event]:
    return store.events(run_unit(SCENARIOS[scenario], "A", seed, store).run_id)


def test_the_mock_world_reports_its_period_exactly(store: EventStore) -> None:
    events = _run(store, "baseline")
    times = [e.ts for e in events if isinstance(e.payload, SignalSampled)]

    assert all(round(b - a, 9) == 0.05 for a, b in itertools.pairwise(times))


def test_the_clock_advances_by_the_time_a_step_reports(
    store: EventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "MockWorld", _SlowMockWorld)
    events = _run(store, "baseline")
    times = [e.ts for e in events if isinstance(e.payload, SignalSampled)]

    assert len(times) > 2
    assert all(round(b - a, 9) == 0.1 for a, b in itertools.pairwise(times))


def test_a_skill_timeout_follows_the_reported_time_not_the_step_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def timeout_at(events: list[Event]) -> tuple[float, int]:
        cancel = next(e for e in events if isinstance(e.payload, SkillCancelled)
                      and e.payload.reason == "timeout")  # fmt: skip
        steps = sum(
            1 for e in events if isinstance(e.payload, SignalSampled) and e.seq < cancel.seq
        )
        return cancel.ts, steps

    with EventStore(tmp_path / "normal.sqlite") as store:
        normal_ts, normal_steps = timeout_at(_run(store, "controller_stall"))
    monkeypatch.setattr(runner, "MockWorld", _SlowMockWorld)
    with EventStore(tmp_path / "slow.sqlite") as store:
        slow_ts, slow_steps = timeout_at(_run(store, "controller_stall"))

    assert abs(slow_ts - normal_ts) <= 0.1  # the same skill timeout in clock time...
    assert slow_steps < 0.6 * normal_steps  # ...reached in about half the steps


@pytest.mark.parametrize("reported", [0.0, 0.01, -0.05, math.nan, math.inf])
def test_a_step_that_reports_less_than_the_period_or_no_number_aborts_the_run(
    store: EventStore, monkeypatch: pytest.MonkeyPatch, reported: float
) -> None:
    monkeypatch.setattr(_ShortStepMockWorld, "reported", reported)
    monkeypatch.setattr(runner, "MockWorld", _ShortStepMockWorld)
    events = _run(store, "baseline")
    end = events[-1].payload

    assert isinstance(end, RunFinished)
    assert end.outcome == "aborted" and end.reason == "world step time invalid"
    assert sum(1 for e in events if isinstance(e.payload, SignalSampled)) == 0
    assert events[-1].ts == 0.0  # the clock never moved by the bad value


# --- time over the force limit within a step -------------------------------------------------


def _signals(force_n: float, over_s: float | None) -> Signals:
    return Signals(
        height_mm=-2.0,
        xy_offset_mm=(0.0, 0.0),
        raw_force_n=(0.0, 0.0, force_n),
        tare_offset_n=(0.0, 0.0, 0.0),
        torque_nm=0.0,
        wrench_valid=True,
        force_over_limit_s=over_s,
    )


def _log(samples: list[Signals], period_s: float = 0.05) -> list[Event]:
    started = RunStarted(scenario_id="s", setting="A", seed=0, attempt=1, task=Task(),
                         config_hash="h", schema_version=SCHEMA_VERSION)  # fmt: skip
    events = [Event(run_id="r", seq=1, ts=0.0, wall_ts=0.0, payload=started)]
    for i, s in enumerate(samples, start=1):
        events.append(Event(run_id="r", seq=i + 1, ts=round(i * period_s, 9), wall_ts=0.0,
                            payload=sampled(i, s)))  # fmt: skip
    return events


def test_a_sample_round_trips_its_time_over_the_limit_through_the_log() -> None:
    s = _signals(25.0, 0.03)

    assert signals_of(sampled(1, s)).force_over_limit_s == 0.03
    assert signals_of(sampled(1, _signals(25.0, None))).force_over_limit_s is None


@pytest.mark.parametrize("bad", [-0.5, math.nan, math.inf])
def test_a_sample_rejects_a_negative_or_non_finite_time_over_the_limit(bad: float) -> None:
    with pytest.raises(ValidationError):
        sampled(1, _signals(25.0, bad))


def test_a_sample_keeps_its_time_over_the_limit_through_the_store(store: EventStore) -> None:
    event = _log([_signals(25.0, 0.03)])[1]
    store.append(_log([])[0])
    store.append(event)
    store.flush()

    (_, read) = store.events("r")
    assert isinstance(read.payload, SignalSampled)
    assert read.payload.force_over_limit_s == 0.03
    assert read == event


def test_the_fold_sums_the_time_each_sample_reports_instead_of_the_period() -> None:
    # The newest sample of the third step is under the limit, but earlier ones in that step were
    # over it: the scorer counts them, and so does the fold.
    state = fold(_log([_signals(25.0, 0.04), _signals(25.0, 0.05), _signals(5.0, 0.02)]))

    assert state.over_limit_s == pytest.approx(0.11)


def test_the_fold_adds_the_reported_time_even_when_the_newest_reading_is_invalid() -> None:
    # The scorer counts every wrench message that arrived in the step; validity is about the
    # newest reading, which the Monitor reads.
    invalid = dataclasses.replace(_signals(25.0, 0.04), wrench_valid=False)
    state = fold(_log([invalid]))

    assert state.over_limit_s == pytest.approx(0.04)


def test_a_log_mixing_both_kinds_of_sample_sums_each_its_own_way() -> None:
    samples = [_signals(25.0, None), _signals(25.0, 0.02), _signals(25.0, None)]
    state = fold(_log(samples))

    # 0.05 since the start, 0.02 reported, 0.05 since the previous sample
    assert state.over_limit_s == pytest.approx(0.12)


def test_samples_without_it_still_sum_the_time_between_samples() -> None:
    state = fold(_log([_signals(25.0, None), _signals(25.0, None), _signals(5.0, None)]))

    assert state.over_limit_s == pytest.approx(0.10)


def test_the_force_limit_rule_fires_on_the_reported_time() -> None:
    state = apply(RunState(), _log([])[0])
    monitor = Monitor(0.05, force_time=lambda: state.over_limit_s)
    monitor.reset("motion")
    verdicts = []
    for i in range(1, 6):  # 5 samples of 0.15 s over the limit: 0.5 s after the 4th
        event = _log([_signals(25.0, 0.15)] * i)[-1]
        state = apply(state, event)
        assert isinstance(event.payload, SignalSampled)
        verdicts.append(monitor.observe(signals_of(event.payload)))

    assert verdicts[:3] == [None, None, None]
    assert verdicts[3] is not None and verdicts[3].failure_class == FailureClass.FORCE_LIMIT
    assert verdicts[3].evidence["over_s"] == pytest.approx(0.6)


def _force_verdicts(samples: list[Signals]) -> list[Verdict | None]:
    state = apply(RunState(), _log([])[0])
    monitor = Monitor(0.05, force_time=lambda: state.over_limit_s)
    monitor.reset("motion")
    verdicts: list[Verdict | None] = []
    for event in _log(samples)[1:]:
        state = apply(state, event)
        assert isinstance(event.payload, SignalSampled)
        verdicts.append(monitor.observe(signals_of(event.payload)))
    return verdicts


def test_the_force_limit_rule_fires_on_time_over_the_limit_between_samples() -> None:
    # Stick-slip: every 20 Hz sample reads under 20 N, but the F/T stream was over the limit for
    # 0.15 s of each step. The sum passes the trigger, so the Monitor must still fire.
    verdicts = _force_verdicts([_signals(5.0, 0.15)] * 4)

    assert verdicts[:3] == [None, None, None]
    v = verdicts[3]
    assert v is not None and v.failure_class == FailureClass.FORCE_LIMIT


def test_the_force_limit_rule_waits_for_a_step_that_was_over_the_limit() -> None:
    # The sum is past the trigger, but this step's stream stayed under the limit throughout.
    verdicts = _force_verdicts([_signals(25.0, 0.3)] * 2 + [_signals(5.0, 0.0)])

    assert verdicts[1] is not None
    assert verdicts[2] is None


def test_a_world_reporting_time_over_the_limit_ends_a_run_with_force_limit(
    store: EventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "MockWorld", _OverLimitMockWorld)
    events = _run(store, "baseline")

    verdicts = [e.payload for e in events if isinstance(e.payload, MonitorVerdict)]
    assert verdicts and verdicts[0].failure_class == FailureClass.FORCE_LIMIT
    assert verdicts[0].evidence["over_s"] == pytest.approx(0.6)
    summary = summarize_run(events)
    assert summary is not None and summary.over_limit_s == pytest.approx(0.6)
    end = events[-1].payload
    assert isinstance(end, RunFinished) and end.outcome == "failed"


class _OverLimitMockWorld(MockWorld):
    """The mock, with an F/T stream that is over the limit for 0.2 s of every step."""

    def signals(self) -> Signals:
        return dataclasses.replace(super().signals(), force_over_limit_s=0.2)


def test_the_unsafe_metric_counts_the_reported_time() -> None:
    events = _log([_signals(25.0, 0.3)] * 4)
    end = RunFinished(outcome="failed", reason="x", steps=1, duration=0.25)
    events.append(Event(run_id="r", seq=len(events) + 1, ts=0.25, wall_ts=0.0, payload=end))
    summary = summarize_run(events)

    assert summary is not None
    assert summary.over_limit_s == pytest.approx(1.2) and summary.unsafe


def test_old_samples_without_the_field_still_read() -> None:
    sample = SignalSampled.model_validate(
        {
            "step": 1,
            "height_mm": 1.0,
            "depth_mm": 0.0,
            "xy_error_mm": 0.0,
            "raw_force_n": [0.0, 0.0, 1.0],
            "tare_offset_n": [0.0, 0.0, 0.0],
            "force_n": 1.0,
            "torque_nm": 0.0,
            "wrench_valid": True,
        }
    )

    assert sample.force_over_limit_s is None
