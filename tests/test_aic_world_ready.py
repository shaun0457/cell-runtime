"""`world_ready` (#79): once the AIC world is ready and before the first skill, the run records
the node's sim time at the run clock's 0 and the poses the oracle reads, so the scene as it
spawned can be compared with the one requested (#13) and the referee's times mapped onto the
run (ADR 029)."""

from __future__ import annotations

import math
from dataclasses import dataclass

import pytest
from aic_fakes import FakeNode

from adapters.aic.convert import Pose
from adapters.aic.run import pose_record, run_goal
from adapters.aic.world import READY_TIMEOUT_S
from cellrt.eval.runner import run_unit
from cellrt.eval.scenarios import Scenario
from cellrt.replay import replay_lines
from cellrt.runtime.events import (
    Event,
    PoseRecord,
    RunFinished,
    SkillStarted,
    WorldReady,
)
from cellrt.runtime.store import EventStore
from cellrt.runtime.task import Task

TASK = Task(id="trial_1_task_1", time_limit=180.0)


def _events(store: EventStore) -> list[Event]:
    (run,) = store.runs()
    return store.events(run.run_id)


def _ready(events: list[Event]) -> list[tuple[int, Event]]:
    return [(i, e) for i, e in enumerate(events) if isinstance(e.payload, WorldReady)]


def _record(pose: Pose) -> PoseRecord:
    return PoseRecord(position=pose.position, orientation=pose.orientation)


def test_a_run_records_the_world_once_before_its_first_skill(store: EventStore) -> None:
    node = FakeNode(task=TASK)
    expected = {
        "port": node.transform(node.port_frame),
        "plug": node.transform(node.plug_frame),
        "gripper": node.transform("gripper/tcp"),
    }

    assert run_goal(node, TASK, store) is True

    events = _events(store)
    ((at, event),) = _ready(events)
    first_skill = next(i for i, e in enumerate(events) if isinstance(e.payload, SkillStarted))
    assert at < first_skill
    ready = event.payload
    assert isinstance(ready, WorldReady)
    assert ready.sim_origin_s == node.start_sim_s == 100.0
    for name, pose in expected.items():
        assert pose is not None
        assert getattr(ready, name) == _record(pose), name
    # The anchor holds to the end of the run, not only at the event: origin + run time is the
    # node's sim time after the last step and the hold check's settle (ADR 029).
    assert ready.sim_origin_s + events[-1].ts == pytest.approx(node.sim_now_s(), abs=1e-6)


def test_the_origin_is_the_sim_time_at_the_run_clocks_zero_after_a_wait(
    store: EventStore,
) -> None:
    # TF comes up 3 s into the goal: the event is at run time ~3 s, its origin still the goal's
    # start, so origin + run time is the sim time the world was ready.
    node = FakeNode(task=TASK, frames_from_s=3.0)

    run_goal(node, TASK, store)

    ((_, event),) = _ready(_events(store))
    ready = event.payload
    assert isinstance(ready, WorldReady)
    assert ready.sim_origin_s == 100.0
    assert event.ts == pytest.approx(3.0, abs=0.05)


def test_the_anchor_holds_when_ground_truth_is_lost_mid_run(store: EventStore) -> None:
    # The step whose read finds no TF stops the run without advancing the clock; the stop
    # confirmation's settle must carry that step's time, or every later event is a step early.
    node = FakeNode(task=TASK, hide_frames_after_sleeps=40)

    run_goal(node, TASK, store)

    events = _events(store)
    ((_, event),) = _ready(events)
    ready = event.payload
    assert isinstance(ready, WorldReady) and ready.sim_origin_s is not None
    end = events[-1].payload
    assert isinstance(end, RunFinished) and end.reason == "ground_truth_unavailable"
    assert ready.sim_origin_s + events[-1].ts == pytest.approx(node.sim_now_s(), abs=1e-6)


def test_a_world_that_is_never_ready_records_no_world_ready(store: EventStore) -> None:
    node = FakeNode(task=TASK, frames_from_s=READY_TIMEOUT_S + 0.05)

    run_goal(node, TASK, store)

    events = _events(store)
    assert _ready(events) == []
    end = events[-1].payload
    assert isinstance(end, RunFinished) and end.reason == "ground_truth_unavailable"


def test_a_ready_wait_that_spends_the_time_limit_still_records_the_world(
    store: EventStore,
) -> None:
    # The frames are up but the wrench never comes within the time limit: the world is built,
    # so its readback is recorded, and the run ends without moving.
    task = Task(id="trial_1_task_1", time_limit=0.5)
    node = FakeNode(task=task, wrench_delay_s=1.0)

    assert run_goal(node, task, store) is False

    events = _events(store)
    ((at, _),) = _ready(events)
    end = events[-1].payload
    assert isinstance(end, RunFinished) and end.reason == "time limit reached"
    assert at == len(events) - 2
    assert node.sent == []


@pytest.mark.parametrize(
    "pose",
    [
        Pose(position=(math.nan, 0.0, 0.0)),
        Pose(position=(0.0, 0.0, 0.0), orientation=(math.inf, 0.0, 0.0, 0.0)),
    ],
)
def test_a_reading_that_is_not_finite_is_recorded_as_none(pose: Pose) -> None:
    assert pose_record(pose) is None
    assert pose_record(Pose(position=(0.5, 0.1, 0.05))) == PoseRecord(
        position=(0.5, 0.1, 0.05), orientation=(1.0, 0.0, 0.0, 0.0)
    )


@dataclass
class _OddClock(FakeNode):
    """A node whose clock reads `origin` until the policy thread first sleeps."""

    origin: float = 0.0

    def sim_now_s(self) -> float:
        return self.origin if self.sleeps == 0 else super().sim_now_s()


def test_a_negative_origin_is_recorded_as_none(store: EventStore) -> None:
    run_goal(_OddClock(task=TASK, origin=-1.0), TASK, store)

    readies = _ready(_events(store))
    assert [e.payload.sim_origin_s for _, e in readies] == [None]  # type: ignore[union-attr]


def test_a_clock_that_reads_nan_at_the_start_never_gets_ready(store: EventStore) -> None:
    # No wait can end on a NaN start, so the world stops while it is built: no origin to record.
    node = _OddClock(task=TASK, origin=math.nan)

    assert run_goal(node, TASK, store, stall_wall_s=0.5) is False

    events = _events(store)
    assert _ready(events) == []
    end = events[-1].payload
    assert isinstance(end, RunFinished) and end.reason == "sim_clock_stalled"


def test_replay_prints_the_origin_and_the_poses(store: EventStore) -> None:
    run_goal(FakeNode(task=TASK), TASK, store)

    (line,) = [line for line in replay_lines(_events(store)) if "world_ready" in line]

    assert "sim_origin=100.000s" in line
    assert "port=(0.5000,0.1000,0.0500)m" in line
    assert "gripper=(0.3000,-0.1000,0.4500)m" in line


def test_a_mock_run_records_no_world_ready(store: EventStore, baseline: Scenario) -> None:
    done = run_unit(baseline, "A", 0, store)

    assert not any(isinstance(e.payload, WorldReady) for e in store.events(done.run_id))
