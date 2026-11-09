"""Live hold (#55, #56, #57, ADR 020): in `live` mode the world keeps stepping while a planner
call runs. The arm holds, every step is sampled and recorded, the clock advances, and both the
call's wall deadline and the task's time limit apply. The Monitor watches the hold with the
safety rules only, and a verdict cuts the call short. An answer records the sim time it took and
is checked against the state it arrives in. `frozen`, the default, is ADR 016 unchanged.

The planner here answers when the world has taken a given number of steps, so the tests are
deterministic without any wall wait; wall time comes from a fake that each world step advances,
and that fails a loop which keeps reading it without stepping.
"""

from __future__ import annotations

import asyncio
import dataclasses
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from cellrt.eval.faults import materialize
from cellrt.eval.runner import config_parts
from cellrt.eval.scenarios import Scenario, load_scenarios
from cellrt.executor.behaviours import ExecEnv, PlannerWait
from cellrt.executor.recovery import RecoveryConfig
from cellrt.executor.runner import run_task
from cellrt.executor.trees import build_tree
from cellrt.executor.wall import wall_now
from cellrt.monitor.classifier import MonitorConfig
from cellrt.planner.base import FailureContext
from cellrt.planner.rule_planner import RulePlanner
from cellrt.replay import replay_lines
from cellrt.runtime.clock import SimClock
from cellrt.runtime.events import SCHEMA_VERSION, Event, RunFinished, RunStarted
from cellrt.runtime.recorder import Recorder
from cellrt.runtime.state import RunState
from cellrt.runtime.store import EventStore
from cellrt.skills.base import Skill
from cellrt.skills.mock import mock_registry
from cellrt.skills.registry import SkillRegistry
from cellrt.trace_export import export_trace
from cellrt.world.base import Command, Signals, WorldStopped
from cellrt.world.mock_world import MockParams, MockWorld

PHASE0 = Path(__file__).resolve().parents[1] / "eval" / "scenarios" / "phase0.yaml"
YAW = {s.id: s for s in load_scenarios(PHASE0)}["yaw_mismatch"]
SWITCH_TO_ACT = {"kind": "switch_skill", "skill": "insert_act"}
DT = MockParams().dt
# What the fake wall clock advances per world step: a power of two, so it adds up exactly.
WALL_PER_STEP_S = 1 / 64
# Reads of the fake wall clock allowed between two world steps: the live loop reads it once per
# step, and once more on a tick that cancels an expired call or records a preemption.
MAX_READS_PER_STEP = 3
SQUEEZE_N = 25.0  # contact force over the 20 N limit


class _FakeWall:
    """The loop's wall clock: each world step advances it by WALL_PER_STEP_S. Read more than
    MAX_READS_PER_STEP times without a step, the loop is spinning: fail instead of hanging."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.reads = 0

    def monotonic(self) -> float:
        self.reads += 1
        if self.reads > MAX_READS_PER_STEP:
            raise AssertionError("the loop keeps reading the wall clock without a world step")
        return self.now

    def step(self) -> None:
        self.now += WALL_PER_STEP_S
        self.reads = 0


class _CountingWorld:
    """A mock world that counts its steps, wakes waiters at a step count, advances the fake wall
    clock on each step, and can read `SQUEEZE_N` of contact force, or an invalid wrench, for a
    range of steps."""

    def __init__(self, inner: MockWorld, wall: _FakeWall) -> None:
        self._inner = inner
        self._wall = wall
        self.steps = 0
        self._waiters: list[tuple[int, asyncio.Event]] = []
        self.stop_after: int | None = None  # steps after which the world stops
        self.squeeze: tuple[int, int] | None = None  # first and last step that read SQUEEZE_N
        self.dropout: tuple[int, int] | None = None  # first and last step with no wrench

    def signals(self) -> Signals:
        s = self._inner.signals()
        if self.dropout is not None and self.dropout[0] <= self.steps <= self.dropout[1]:
            return dataclasses.replace(s, wrench_valid=False)
        if self.squeeze is None or not self.squeeze[0] <= self.steps <= self.squeeze[1]:
            return s
        tare = s.tare_offset_n
        raw = (tare[0], tare[1], tare[2] + SQUEEZE_N)
        return dataclasses.replace(s, raw_force_n=raw, wrench_valid=True)

    def command(self, cmd: Command) -> None:
        self._inner.command(cmd)

    def step(self, dt: float) -> float:
        if self.stop_after is not None and self.steps >= self.stop_after:
            raise WorldStopped("goal_cancelled")
        elapsed = self._inner.step(dt)
        self.steps += 1
        self._wall.step()
        for target, event in self._waiters:
            if self.steps >= target:
                event.set()
        return elapsed

    def after_steps(self, n: int) -> asyncio.Event:
        event = asyncio.Event()
        self._waiters.append((self.steps + n, event))
        return event

    # The fault ground truth, as the eval runner hands it to ExecEnv.
    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


@dataclass
class _AfterSteps:
    """Answers once the world has taken `steps` steps since the call started."""

    world: _CountingWorld
    steps: int
    deadline_s: float = 30.0
    released: list[bool] = field(default_factory=list)

    async def triage(self, ctx: FailureContext) -> object:
        try:
            await self.world.after_steps(self.steps).wait()
        finally:
            self.released.append(True)
        return dict(SWITCH_TO_ACT)


@dataclass
class _SqueezedOnce:
    """First call: from `after` steps into the call, for `steps` steps, the arm is pressed with
    SQUEEZE_N (or, with `fault` "dropout", the wrench is invalid), and the call never answers
    by itself; cancelled, it answers anyway (an answer that must never be read). Later calls
    abort at once."""

    world: _CountingWorld
    after: int = 5
    steps: int = 10
    fault: str = "squeeze"
    deadline_s: float = 30.0
    released: list[bool] = field(default_factory=list)
    calls: int = 0

    async def triage(self, ctx: FailureContext) -> object:
        self.calls += 1
        if self.calls > 1:
            return {"kind": "abort", "reason": "second call"}
        first = self.world.steps + self.after
        setattr(self.world, self.fault, (first, first + self.steps - 1))
        try:
            await self.world.after_steps(10_000).wait()
        except asyncio.CancelledError:
            return dict(SWITCH_TO_ACT)  # ignores the cancel and answers late
        finally:
            self.released.append(True)
        return dict(SWITCH_TO_ACT)


@dataclass
class _AnswersAtOnce:
    """Answers in its first step, so the loop never holds for it; the arm is pressed with
    SQUEEZE_N on the one sample the world takes before the answer is read."""

    world: _CountingWorld
    deadline_s: float = 30.0

    async def triage(self, ctx: FailureContext) -> object:
        self.world.squeeze = (self.world.steps + 1, self.world.steps + 1)
        return dict(SWITCH_TO_ACT)


@dataclass
class _DropsThenAnswers:
    """Answers after `steps` steps; the wrench is invalid for `dropout` steps that end `gap`
    steps before the answer. Too short a dropout for the Monitor's rule (0.2 s)."""

    world: _CountingWorld
    answer: dict[str, str]
    steps: int = 6
    dropout: int = 2
    gap: int = 0
    deadline_s: float = 30.0

    async def triage(self, ctx: FailureContext) -> object:
        last = self.world.steps + self.steps - self.gap
        self.world.dropout = (last - self.dropout + 1, last)
        await self.world.after_steps(self.steps).wait()
        return dict(self.answer)


class _SleepsThenSwitches:
    """Answers after a short wall-time wait, so the loop waits on it."""

    deadline_s = 30.0

    async def triage(self, ctx: FailureContext) -> object:
        done = asyncio.Event()  # real time, through the loop's timer
        asyncio.get_running_loop().call_later(0.01, done.set)
        await done.wait()
        return dict(SWITCH_TO_ACT)


class _NeedsWrench:
    """A skill that runs only on a valid wrench reading (its preconditions), else as `inner`."""

    def __init__(self, inner: Skill) -> None:
        self._inner = inner

    def preconditions(self, state: RunState) -> bool:
        return state.last_signal is not None and state.last_signal.wrench_valid

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


class _NeverReady:
    """A skill whose preconditions never hold, else as `inner`."""

    def __init__(self, inner: Skill) -> None:
        self._inner = inner

    def preconditions(self, state: RunState) -> bool:
        return False

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def _never_ready(*names: str) -> SkillRegistry:
    mock = mock_registry()
    return SkillRegistry(
        _NeverReady(mock.get(n)) if n in names else mock.get(n)  # type: ignore[misc]
        for n in mock.names()
    )


def _needing_wrench(*names: str) -> SkillRegistry:
    """The mock skills, with `names` needing a valid wrench reading to start."""
    mock = mock_registry()
    return SkillRegistry(
        _NeedsWrench(mock.get(n)) if n in names else mock.get(n)  # type: ignore[misc]
        for n in mock.names()
    )


Planner = Any  # the scripted planners above


def _run(
    store: EventStore,
    scenario: Scenario,
    planner_wait: PlannerWait,
    make_planner: Callable[[_CountingWorld], Planner],
    *,
    fake_wall: bool = True,
    recovery: RecoveryConfig | None = None,
    monitor_config: MonitorConfig | None = None,
    judged: list[float] | None = None,
    registry: SkillRegistry | None = None,
) -> tuple[list[Event], Planner]:
    """Runs `scenario` in setting C. `judged`, if given, receives the time of the sample behind
    each Monitor observation."""
    wall = _FakeWall()
    params = MockParams()
    faults = materialize(scenario.faults, random.Random(f"{scenario.id}:0:faults"))
    world = _CountingWorld(MockWorld(random.Random(f"{scenario.id}:0"), params, faults), wall)
    planner = make_planner(world)
    clock = SimClock()
    run_id = f"{scenario.id}/C/0/a1"
    recorder = Recorder(run_id, store, clock)
    recorder.emit(
        RunStarted(
            scenario_id=scenario.id,
            setting="C",
            seed=0,
            attempt=1,
            task=scenario.task,
            config_hash="h",
            schema_version=SCHEMA_VERSION,
        )
    )
    env = ExecEnv(
        world=world,
        clock=clock,
        recorder=recorder,
        period_s=params.dt,
        fault_source=world,  # type: ignore[arg-type]
        monitor_config=monitor_config or MonitorConfig(),
        time_limit_s=scenario.task.time_limit,
        planner_wait=planner_wait,
        # live mode reads the deadline per step; frozen waits on the loop's own timer
        wall_clock=wall.monotonic if fake_wall else wall_now,
    )
    if judged is not None:
        observe = env.monitor.observe

        def judge(signals: Signals) -> Any:
            judged.append(recorder.state.last_sample_ts)
            return observe(signals)

        env.monitor.observe = judge  # type: ignore[method-assign]
    skills = registry or mock_registry()
    root = build_tree("C", skills, env, recovery or RecoveryConfig(), planner)

    async def go() -> None:
        outcome, reason = await run_task(root, env, scenario.task)
        recorder.emit(
            RunFinished(
                outcome=outcome, reason=reason, steps=recorder.state.steps, duration=clock.now()
            )
        )

    asyncio.run(go())
    store.flush()
    return store.events(run_id), planner


def _between(events: list[Event], first: str, last: str) -> list[Event]:
    start = next(i for i, e in enumerate(events) if e.type == first)
    end = next(i for i, e in enumerate(events) if e.type == last and i > start)
    return events[start + 1 : end]


def _world_events(events: list[Event]) -> list[str]:
    """The event types, without the status changes of tree nodes."""
    return [e.type for e in events if e.type != "bt_node_changed"]


def _first(events: list[Event], kind: str) -> Event:
    return next(e for e in events if e.type == kind)


# --- the world during a call (#55) -----------------------------------------------------------


def test_in_live_mode_the_world_steps_and_is_recorded_while_the_planner_thinks(
    store: EventStore,
) -> None:
    events, _ = _run(store, YAW, "live", lambda w: _AfterSteps(w, steps=40))

    during = _between(events, "planner_called", "planner_decided")
    # 40 steps until the answer, which the next tick reads before another step. 2 s of holding
    # still, twice the stall window: the Monitor watches the hold with the safety rules only, so
    # no progress, stall or pose rule calls it a failure (#56).
    assert _world_events(during) == ["signal_sampled"] * 40
    called = _first(events, "planner_called")
    decided = _first(events, "planner_decided")
    assert decided.ts - called.ts == pytest.approx(40 * DT)
    assert events[-1].payload.outcome == "success"  # the switch to insert_act still runs


def test_in_frozen_mode_the_world_never_steps_during_a_call(store: EventStore) -> None:
    # The same planner never answers when the world stands still: the call times out at its
    # deadline, with no step taken (ADR 016, unchanged).
    events, planner = _run(
        store, YAW, "frozen", lambda w: _AfterSteps(w, steps=40, deadline_s=0.05), fake_wall=False
    )

    during = _between(events, "planner_called", "planner_timed_out")
    assert _world_events(during) == []
    assert planner.released == [True]


def test_in_live_mode_the_wall_deadline_cancels_a_call_without_blocking_the_loop(
    store: EventStore,
) -> None:
    # Never answers within the deadline: 0.5 s of fake wall time is 32 world steps, and the
    # call is cancelled on the next tick, before another step.
    events, planner = _run(
        store, YAW, "live", lambda w: _AfterSteps(w, steps=10_000, deadline_s=0.5)
    )

    during = _between(events, "planner_called", "planner_timed_out")
    assert _world_events(during) == ["signal_sampled"] * 32
    assert _first(events, "planner_timed_out").payload.waited_s == pytest.approx(0.5)
    assert planner.released == [True]
    assert events[-1].payload.reason.endswith("(planner timed out)")


def test_in_live_mode_the_time_limit_ends_a_run_during_a_call(store: EventStore) -> None:
    short = YAW.model_copy(update={"task": YAW.task.model_copy(update={"time_limit": 9.0})})

    events, planner = _run(store, short, "live", lambda w: _AfterSteps(w, steps=10_000))

    finished = events[-1].payload
    assert (finished.outcome, finished.reason) == ("aborted", "time limit reached")
    assert finished.duration == pytest.approx(9.0)
    assert planner.released == [True]
    assert not any(e.type == "planner_decided" for e in events)


def test_in_live_mode_a_world_that_stops_during_a_call_ends_the_run_and_the_call(
    store: EventStore,
) -> None:
    # On AIC a cancelled goal stops the world on its next step (ADR 012): during a live wait
    # that is seen within a step, not after the answer.
    def stops_while_waiting(world: _CountingWorld) -> _AfterSteps:
        planner = _AfterSteps(world, steps=10_000)
        triage = planner.triage

        async def stop_soon(ctx: FailureContext) -> object:
            world.stop_after = world.steps + 5
            return await triage(ctx)

        planner.triage = stop_soon  # type: ignore[method-assign]
        return planner

    events, planner = _run(store, YAW, "live", stops_while_waiting)

    during = _between(events, "planner_called", "run_finished")
    assert not any(e.type == "planner_decided" for e in events)
    assert _world_events(during).count("signal_sampled") == 5
    finished = events[-1].payload
    assert (finished.outcome, finished.reason) == ("aborted", "goal_cancelled")
    assert planner.released == [True]


# --- the Monitor during a live wait (#56) ----------------------------------------------------


def test_a_force_limit_during_a_live_wait_cuts_the_call_short_and_runs_the_force_plan(
    store: EventStore,
) -> None:
    # 25 N from the 5th step of the wait: each sample over 20 N adds the 0.05 s before it, so
    # the 10th (step 14 of the wait) brings the task's time over the limit to the 0.5 s trigger.
    events, planner = _run(store, YAW, "live", _SqueezedOnce)

    during = _between(events, "planner_called", "planner_preempted")
    assert _world_events(during) == ["signal_sampled"] * 14 + ["monitor_verdict"]
    verdict = during[-1].payload
    assert (verdict.skill, verdict.failure_class) == ("insert_scripted", "FORCE_LIMIT")
    preempted = _first(events, "planner_preempted").payload
    assert (preempted.skill, preempted.failure_class) == ("insert_scripted", "FORCE_LIMIT")
    assert preempted.waited_sim_s == pytest.approx(14 * DT)
    assert preempted.waited_s == pytest.approx(14 * WALL_PER_STEP_S)
    assert planner.released == [True]  # the cancelled call's cleanup ran
    after = _between(events, "planner_preempted", "run_finished")
    recovery = next(e.payload for e in after if e.type == "recovery_started")
    assert (recovery.failure_class, recovery.action, recovery.source) == (
        "FORCE_LIMIT",
        "retract_slow",
        "monitor",
    )
    started = [e.payload.skill for e in after if e.type == "skill_started"]
    assert started[:2] == ["retract", "insert_scripted"]


def test_the_late_answer_of_a_cut_call_is_never_read(store: EventStore) -> None:
    # The cancelled call answers anyway (switch to insert_act): nothing records or runs it. The
    # only decision is the second call's abort.
    events, planner = _run(store, YAW, "live", _SqueezedOnce)

    decided = [e.payload.decision for e in events if e.type == "planner_decided"]
    assert decided == [{"kind": "abort", "reason": "second call"}]
    assert planner.calls == 2
    assert not any(e.type == "skill_started" and e.payload.skill == "insert_act" for e in events)


def test_with_the_force_budget_used_up_a_cut_call_fails_the_step(store: EventStore) -> None:
    no_force_recovery = RecoveryConfig(budgets={**RecoveryConfig().budgets, "FORCE_LIMIT": 0})

    events, _ = _run(store, YAW, "live", _SqueezedOnce, recovery=no_force_recovery)

    finished = events[-1].payload
    assert (finished.outcome, finished.reason) == (
        "failed",
        "insert_scripted failed: FORCE_LIMIT (FORCE_LIMIT recovery budget used up)",
    )
    assert not any(
        e.type == "recovery_started" and e.ts > _first(events, "planner_called").ts for e in events
    )


def test_replay_and_the_trace_show_the_preemption(store: EventStore) -> None:
    events, _ = _run(store, YAW, "live", _SqueezedOnce)

    lines = list(replay_lines(events))
    assert any(
        "insert_scripted call cut short by FORCE_LIMIT after 0.70s" in line for line in lines
    )
    spans = [
        e
        for e in export_trace(events, clock="sim")["traceEvents"]
        if e["ph"] == "X" and e["name"] == "planner call 1"
    ]
    assert [s["args"]["result"] for s in spans] == ["planner_preempted"]


def test_after_a_cut_call_the_monitor_judges_every_sample_once(store: EventStore) -> None:
    # The cut call ignores its cancel and returns: that must not hold the world for a tick in
    # which the next skill judges the same sample again (it is in the log only once).
    judged: list[float] = []

    events, _ = _run(store, YAW, "live", _SqueezedOnce, judged=judged)

    assert _first(events, "planner_preempted")
    assert len(judged) == len(set(judged))


def test_a_sensor_dropout_during_a_live_wait_cuts_the_call_short_and_waits_for_the_sensor(
    store: EventStore,
) -> None:
    # No valid wrench from the 5th step of the wait: the dropout rule needs 0.2 s of invalid
    # samples, 5 at 0.05 s, so the 5th of them (step 9 of the wait) is the verdict.
    def dropped(world: _CountingWorld) -> _SqueezedOnce:
        return _SqueezedOnce(world, steps=5, fault="dropout")

    events, _ = _run(store, YAW, "live", dropped)

    during = _between(events, "planner_called", "planner_preempted")
    assert _world_events(during) == ["signal_sampled"] * 9 + ["monitor_verdict"]
    assert during[-1].payload.failure_class == "SENSOR_DROPOUT"
    preempted = _first(events, "planner_preempted").payload
    assert preempted.waited_sim_s == pytest.approx(9 * DT)
    after = _between(events, "planner_preempted", "run_finished")
    recovery = next(e.payload for e in after if e.type == "recovery_started")
    assert (recovery.failure_class, recovery.action, recovery.source) == (
        "SENSOR_DROPOUT",
        "wait_sensor",
        "monitor",
    )
    assert next(e.payload.skill for e in after if e.type == "skill_started") == "wait_sensor"


def test_a_verdict_wins_over_an_answer_that_arrived_with_the_same_sample(
    store: EventStore,
) -> None:
    # The call answers in its first step, so the loop never holds for it: the world steps once
    # before the answer is read, and that sample is over the force limit. With the trigger at
    # one sample's worth (0.05 s), the Monitor's verdict comes first and the answer is never
    # read. The loop never waited on the call, so no wall time was spent on it.
    one_sample = MonitorConfig(force_trigger_s=DT)

    events, _ = _run(store, YAW, "live", _AnswersAtOnce, monitor_config=one_sample)

    during = _between(events, "planner_called", "planner_preempted")
    assert _world_events(during) == ["signal_sampled", "monitor_verdict"]
    first_call = _between(events, "planner_called", "planner_called")
    assert not any(e.type == "planner_decided" for e in first_call)
    preempted = _first(events, "planner_preempted").payload
    assert (preempted.waited_s, preempted.waited_sim_s) == (0.0, pytest.approx(DT))


# --- an answer in the state it arrives in (#57) -------------------------------------------


def test_in_live_mode_a_decision_records_the_sim_time_the_call_took(store: EventStore) -> None:
    events, _ = _run(store, YAW, "live", lambda w: _AfterSteps(w, steps=7))

    decided = _first(events, "planner_decided").payload
    assert decided.waited_sim_s == pytest.approx(7 * DT)
    lines = list(replay_lines(events))
    assert any("insert_scripted -> " in line and "after 0.35s" in line for line in lines)


def test_in_frozen_mode_a_call_the_loop_waited_on_took_no_sim_time(store: EventStore) -> None:
    events, _ = _run(store, YAW, "frozen", lambda w: _SleepsThenSwitches(), fake_wall=False)

    assert _first(events, "planner_decided").payload.waited_sim_s == 0.0


def test_an_answer_in_the_calls_first_step_is_read_one_period_later(store: EventStore) -> None:
    # The loop never waits on such a call (ADR 016): the world takes its step, and the answer
    # is read on the next tick. The rule planner answers so, in either mode.
    events, _ = _run(store, YAW, "frozen", lambda w: RulePlanner())

    assert _first(events, "planner_decided").payload.waited_sim_s == pytest.approx(DT)


def test_a_switch_whose_preconditions_stopped_holding_during_the_call_is_rejected(
    store: EventStore,
) -> None:
    # insert_act needs a valid wrench: it had one when the call began, and the answer arrives
    # in the second of two samples without one. Rejected like invalid output (ADR 008):
    # nothing runs, and the run ends aborted.
    def drops(world: _CountingWorld) -> _DropsThenAnswers:
        return _DropsThenAnswers(world, SWITCH_TO_ACT)

    events, _ = _run(store, YAW, "live", drops, registry=_needing_wrench("insert_act"))

    called = _first(events, "planner_called")
    before = [e for e in events if e.type == "signal_sampled" and e.ts <= called.ts]
    assert before[-1].payload.wrench_valid  # it held when the call began
    rejected = _first(events, "planner_output_rejected").payload
    assert rejected.errors == ["skill: preconditions_not_met"]
    assert "switch_skill" in rejected.raw
    assert not any(e.type in ("planner_decided", "precondition_failed") for e in events)
    assert not any(e.type == "skill_started" and e.payload.skill == "insert_act" for e in events)
    # No recovery is spent on it.
    assert not any(e.type == "recovery_started" and e.ts >= called.ts for e in events)
    finished = events[-1].payload
    assert (finished.outcome, finished.reason) == (
        "aborted",
        "insert_scripted failed: UNKNOWN (planner answer no longer applies)",
    )


def test_a_retry_whose_preconditions_stopped_holding_during_the_call_is_rejected(
    store: EventStore,
) -> None:
    # A retry runs the failed skill again: its preconditions are the ones checked.
    def drops(world: _CountingWorld) -> _DropsThenAnswers:
        return _DropsThenAnswers(world, {"kind": "retry"})

    events, _ = _run(store, YAW, "live", drops, registry=_needing_wrench("insert_scripted"))

    rejected = _first(events, "planner_output_rejected").payload
    assert rejected.errors == ["skill: preconditions_not_met"]
    called = _first(events, "planner_called")
    assert not any(
        e.type == "skill_started" and e.ts > called.ts and e.payload.skill == "insert_scripted"
        for e in events
    )


def test_in_frozen_mode_a_switch_whose_preconditions_do_not_hold_is_rejected_too(
    store: EventStore,
) -> None:
    # The state did not change during the call, but the answer is still checked against it.
    events, _ = _run(
        store,
        YAW,
        "frozen",
        lambda w: _SleepsThenSwitches(),
        fake_wall=False,
        registry=_never_ready("insert_act"),
    )

    rejected = _first(events, "planner_output_rejected").payload
    assert rejected.errors == ["skill: preconditions_not_met"]
    assert not any(e.type == "skill_started" and e.payload.skill == "insert_act" for e in events)


def test_a_switch_whose_preconditions_hold_again_on_arrival_runs(store: EventStore) -> None:
    # The wrench comes back one sample before the answer arrives: the switch runs.
    def recovers(world: _CountingWorld) -> _DropsThenAnswers:
        return _DropsThenAnswers(world, SWITCH_TO_ACT, gap=1)

    events, _ = _run(store, YAW, "live", recovers, registry=_needing_wrench("insert_act"))

    assert not any(e.type == "planner_output_rejected" for e in events)
    assert any(e.type == "skill_started" and e.payload.skill == "insert_act" for e in events)
    assert events[-1].payload.outcome == "success"


# --- configuration -----------------------------------------------------------------------------


def test_the_wait_mode_counts_in_the_planner_part_of_the_config_only() -> None:
    def parts(setting: str, wait: PlannerWait) -> dict[str, str]:
        return config_parts(
            YAW,
            setting,  # type: ignore[arg-type]
            MockParams(),
            RecoveryConfig(),
            MonitorConfig(),
            mock_registry(),
            RulePlanner() if setting == "C" else None,
            planner_wait=wait,
        )

    frozen, live = parts("C", "frozen"), parts("C", "live")
    assert [k for k in frozen if frozen[k] != live[k]] == ["planner"]
    # Without a planner nothing waits on one: the mode changes nothing, and no hash.
    assert parts("B", "frozen") == parts("B", "live")


def test_a_planner_wait_mode_must_be_known(store: EventStore) -> None:
    clock = SimClock()
    with pytest.raises(ValueError, match="planner_wait"):
        ExecEnv(
            world=MockWorld(random.Random(0), MockParams(), []),
            clock=clock,
            recorder=Recorder("r", store, clock),
            period_s=0.05,
            planner_wait="paused",  # type: ignore[arg-type]
        )
