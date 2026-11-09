"""Live hold (#55, ADR 020): in `live` mode the world keeps stepping while a planner call runs.
The arm holds, every step is sampled and recorded, the clock advances, and both the call's wall
deadline and the task's time limit apply. `frozen`, the default, is ADR 016 unchanged.

The planner here answers when the world has taken a given number of steps, so the tests are
deterministic without any wall wait; wall time for the deadline comes from a fake that each world
step advances, and that fails a loop which keeps reading it without stepping.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

import cellrt.executor.runner as runner_module
from cellrt.eval.faults import materialize
from cellrt.eval.runner import config_parts
from cellrt.eval.scenarios import Scenario, load_scenarios
from cellrt.executor.behaviours import ExecEnv, PlannerWait
from cellrt.executor.recovery import RecoveryConfig
from cellrt.executor.runner import run_task
from cellrt.executor.trees import build_tree
from cellrt.monitor.classifier import MonitorConfig
from cellrt.planner.base import FailureContext
from cellrt.planner.rule_planner import RulePlanner
from cellrt.runtime.clock import SimClock
from cellrt.runtime.events import SCHEMA_VERSION, Event, RunFinished, RunStarted
from cellrt.runtime.recorder import Recorder
from cellrt.runtime.store import EventStore
from cellrt.skills.mock import mock_registry
from cellrt.world.base import Command, Signals, WorldStopped
from cellrt.world.mock_world import MockParams, MockWorld

PHASE0 = Path(__file__).resolve().parents[1] / "eval" / "scenarios" / "phase0.yaml"
YAW = {s.id: s for s in load_scenarios(PHASE0)}["yaw_mismatch"]
SWITCH_TO_ACT = {"kind": "switch_skill", "skill": "insert_act"}
# What the fake wall clock advances per world step: a power of two, so it adds up exactly.
WALL_PER_STEP_S = 1 / 64
# Reads of the fake wall clock allowed between two world steps: the live loop reads it once per
# step, and once more on a tick that cancels an expired call.
MAX_READS_PER_STEP = 3


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
    """A mock world that counts its steps, wakes waiters at a step count, and advances the
    fake wall clock on each step."""

    def __init__(self, inner: MockWorld, wall: _FakeWall) -> None:
        self._inner = inner
        self._wall = wall
        self.steps = 0
        self._waiters: list[tuple[int, asyncio.Event]] = []
        self.stop_after: int | None = None  # steps after which the world stops

    def signals(self) -> Signals:
        return self._inner.signals()

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


def _run(
    store: EventStore,
    scenario: Scenario,
    planner_wait: PlannerWait,
    make_planner: Callable[[_CountingWorld], _AfterSteps],
    monkeypatch: pytest.MonkeyPatch,
    *,
    fake_wall: bool = True,
) -> tuple[list[Event], object]:
    wall = _FakeWall()
    if fake_wall:  # live mode reads the deadline per step; frozen waits on the loop's timer
        monkeypatch.setattr(runner_module, "time", SimpleNamespace(monotonic=wall.monotonic))
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
        monitor_config=MonitorConfig(),
        time_limit_s=scenario.task.time_limit,
        planner_wait=planner_wait,
    )
    root = build_tree("C", mock_registry(), env, RecoveryConfig(), planner)  # type: ignore[arg-type]

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


def test_in_live_mode_the_world_steps_and_is_recorded_while_the_planner_thinks(
    store: EventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    events, _ = _run(store, YAW, "live", lambda w: _AfterSteps(w, steps=40), monkeypatch)

    during = _between(events, "planner_called", "planner_decided")
    # 40 steps until the answer, which the next tick reads before another step.
    assert _world_events(during) == ["signal_sampled"] * 40
    called = next(e for e in events if e.type == "planner_called")
    decided = next(e for e in events if e.type == "planner_decided")
    assert decided.ts - called.ts == pytest.approx(40 * MockParams().dt)
    assert events[-1].payload.outcome == "success"  # the switch to insert_act still runs


def test_in_frozen_mode_the_world_never_steps_during_a_call(
    store: EventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The same planner never answers when the world stands still: the call times out at its
    # deadline, with no step taken (ADR 016, unchanged).
    events, planner = _run(
        store,
        YAW,
        "frozen",
        lambda w: _AfterSteps(w, steps=40, deadline_s=0.05),
        monkeypatch,
        fake_wall=False,
    )

    during = _between(events, "planner_called", "planner_timed_out")
    assert _world_events(during) == []
    assert planner.released == [True]  # type: ignore[attr-defined]


def test_in_live_mode_the_wall_deadline_cancels_a_call_without_blocking_the_loop(
    store: EventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Never answers within the deadline: 0.5 s of fake wall time is 32 world steps, and the
    # call is cancelled on the next tick, before another step.
    events, planner = _run(
        store, YAW, "live", lambda w: _AfterSteps(w, steps=10_000, deadline_s=0.5), monkeypatch
    )

    during = _between(events, "planner_called", "planner_timed_out")
    assert _world_events(during) == ["signal_sampled"] * 32
    timed_out = next(e for e in events if e.type == "planner_timed_out")
    assert timed_out.payload.waited_s == pytest.approx(0.5)
    assert planner.released == [True]  # type: ignore[attr-defined]
    assert events[-1].payload.reason.endswith("(planner timed out)")


def test_in_live_mode_the_time_limit_ends_a_run_during_a_call(
    store: EventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    short = YAW.model_copy(update={"task": YAW.task.model_copy(update={"time_limit": 9.0})})

    events, planner = _run(
        store, short, "live", lambda w: _AfterSteps(w, steps=10_000), monkeypatch
    )

    finished = events[-1].payload
    assert (finished.outcome, finished.reason) == ("aborted", "time limit reached")
    assert finished.duration == pytest.approx(9.0)
    assert planner.released == [True]  # type: ignore[attr-defined]
    assert not any(e.type == "planner_decided" for e in events)


def test_in_live_mode_a_world_that_stops_during_a_call_ends_the_run_and_the_call(
    store: EventStore, monkeypatch: pytest.MonkeyPatch
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

    events, planner = _run(store, YAW, "live", stops_while_waiting, monkeypatch)

    during = _between(events, "planner_called", "run_finished")
    assert not any(e.type == "planner_decided" for e in events)
    assert _world_events(during).count("signal_sampled") == 5
    finished = events[-1].payload
    assert (finished.outcome, finished.reason) == ("aborted", "goal_cancelled")
    assert planner.released == [True]  # type: ignore[attr-defined]


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
