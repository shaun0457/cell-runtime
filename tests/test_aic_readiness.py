"""The bounded wait for the cell to be ready at a goal's start (ADR 011): ground-truth TF and a
fresh tared wrench, up to `READY_TIMEOUT_S` of sim time, as CheatCode waits up to 10 s for TF
(`CheatCode.py:45-68`, `:203` @ 3ab0653). Missing TF at the bound stops the run; a wrench that
never comes is left to the Monitor (SENSOR_DROPOUT)."""

from __future__ import annotations

import pytest
from aic_fakes import FakeNode

from adapters.aic.run import run_goal
from adapters.aic.world import READY_TIMEOUT_S
from cellrt.runtime.events import Event, MonitorVerdict, RunFinished, SkillStarted
from cellrt.runtime.store import EventStore
from cellrt.runtime.task import Task

TASK = Task(id="trial_1_task_1", time_limit=180.0)


def _events(store: EventStore) -> list[Event]:
    (run,) = store.runs()
    return store.events(run.run_id)


def _end(store: EventStore) -> RunFinished:
    end = _events(store)[-1].payload
    assert isinstance(end, RunFinished)
    return end


def _first_skill_ts(store: EventStore) -> float:
    return next(e.ts for e in _events(store) if isinstance(e.payload, SkillStarted))


def _verdicts(store: EventStore) -> list[MonitorVerdict]:
    return [e.payload for e in _events(store) if isinstance(e.payload, MonitorVerdict)]


def test_the_wait_is_cheatcodes_10_s() -> None:
    assert READY_TIMEOUT_S == 10.0


@pytest.mark.parametrize("frames_from_s", [3.0, READY_TIMEOUT_S - 0.05, READY_TIMEOUT_S])
def test_ground_truth_that_comes_up_within_the_bound_is_waited_for(
    store: EventStore, frames_from_s: float
) -> None:
    node = FakeNode(task=TASK, frames_from_s=frames_from_s)

    assert run_goal(node, TASK, store) is True

    # The run's clock counts the wait: the tree starts when the cell is ready.
    assert _first_skill_ts(store) == pytest.approx(frames_from_s, abs=0.05)
    assert not _verdicts(store)


def test_ground_truth_still_missing_at_the_bound_stops_the_run_without_moving(
    store: EventStore,
) -> None:
    node = FakeNode(task=TASK, frames_from_s=READY_TIMEOUT_S + 0.05)

    assert run_goal(node, TASK, store) is False

    end = _end(store)
    assert (end.outcome, end.reason) == ("aborted", "ground_truth_unavailable")
    assert end.duration == pytest.approx(READY_TIMEOUT_S, abs=0.05)
    assert node.sim_s - node.start_sim_s == pytest.approx(READY_TIMEOUT_S, abs=0.05)
    assert node.sent == []


def test_a_wrench_stream_that_starts_late_is_waited_for(store: EventStore) -> None:
    # Without the wait the first 0.2 s without a valid wrench would be SENSOR_DROPOUT.
    node = FakeNode(task=TASK, wrench_delay_s=1.0)

    assert run_goal(node, TASK, store) is True

    assert _first_skill_ts(store) == pytest.approx(1.0, abs=0.05)
    assert not _verdicts(store)


def test_a_wrench_stream_that_never_starts_is_left_to_the_monitor(store: EventStore) -> None:
    node = FakeNode(task=TASK, wrench_delay_s=1000.0)

    assert run_goal(node, TASK, store) is False

    assert _first_skill_ts(store) == pytest.approx(READY_TIMEOUT_S, abs=0.05)
    (verdict,) = _verdicts(store)
    assert verdict.failure_class == "SENSOR_DROPOUT"
    assert _end(store).outcome == "failed"


def test_a_cancel_during_the_wait_stops_the_run_without_moving(store: EventStore) -> None:
    # 40 sleeps of 25 ms: the goal is cancelled 1 s into the wait for TF.
    node = FakeNode(task=TASK, frames_from_s=5.0, goal_stop=("goal_cancel_requested", 40))

    assert run_goal(node, TASK, store) is False

    end = _end(store)
    assert (end.outcome, end.reason) == ("aborted", "goal_cancel_requested")
    assert node.sim_s - node.start_sim_s < 1.1  # acted on within one poll step
    assert node.sent == []


def test_a_sim_clock_that_stops_during_the_wait_ends_it_within_the_stall_bound(
    store: EventStore,
) -> None:
    node = FakeNode(task=TASK, frames_from_s=5.0, stall_after_sleeps=10)

    assert run_goal(node, TASK, store, stall_wall_s=0.5) is False

    assert _end(store).reason == "sim_clock_stalled"
    assert node.wall_s < 10 * 0.002 + 0.5 + 0.01
    assert node.sent == []


def test_a_time_limit_shorter_than_the_wait_bounds_it_and_nothing_moves(
    store: EventStore,
) -> None:
    task = TASK.model_copy(update={"time_limit": 5.0})
    node = FakeNode(task=task, frames_from_s=8.0)

    assert run_goal(node, task, store) is False

    end = _end(store)
    assert (end.outcome, end.reason) == ("aborted", "ground_truth_unavailable")
    assert end.duration == pytest.approx(5.0, abs=0.05)
    assert node.sent == []


def test_a_wait_that_uses_the_whole_time_limit_ends_the_run_without_moving(
    store: EventStore,
) -> None:
    task = TASK.model_copy(update={"time_limit": 2.0})
    node = FakeNode(task=task, wrench_delay_s=1000.0)  # frames are up; the wrench never comes

    assert run_goal(node, task, store) is False

    end = _end(store)
    assert (end.outcome, end.reason) == ("aborted", "time limit reached")
    assert end.duration == pytest.approx(2.0, abs=0.05)
    assert node.sent == []
    assert not [e for e in _events(store) if isinstance(e.payload, SkillStarted)]


def test_the_time_limit_counts_the_wait(store: EventStore) -> None:
    task = TASK.model_copy(update={"time_limit": 5.0})
    node = FakeNode(task=task, frames_from_s=3.0)

    assert run_goal(node, task, store) is False

    end = _end(store)
    assert (end.outcome, end.reason) == ("aborted", "time limit reached")
    assert end.duration == pytest.approx(5.0, abs=0.05)
