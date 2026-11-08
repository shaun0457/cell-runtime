"""Tracer bullet through the AIC adapter: the policy body (`run_goal`) runs `approach` on one
task against a fake node, and touches every layer once: the policy thread's own asyncio loop,
the sim clock, TF to `Signals`, position targets out, events in the store, the return value.
"""

from __future__ import annotations

import itertools
import math
import threading
from pathlib import Path

import pytest
from aic_fakes import PLUG_BELOW_TCP_M, PORT, FakeNode

from adapters.aic.run import run_goal
from adapters.aic.world import WRENCH_FRESH_S, AicWorld
from cellrt.runtime.events import (
    SCHEMA_VERSION,
    Event,
    RunFinished,
    RunStarted,
    SignalSampled,
    SkillFinished,
    SkillStarted,
)
from cellrt.runtime.store import EventStore
from cellrt.runtime.task import Task
from cellrt.world.base import HOLD

TASK = Task(id="trial_1_task_1", time_limit=180.0)


def _events(store: EventStore) -> list[Event]:
    (run_id,) = [r.run_id for r in store.runs()]
    return store.events(run_id)


def test_insert_cable_runs_the_setting_a_tree_and_records_the_run(store: EventStore) -> None:
    node = FakeNode(task=TASK)

    assert run_goal(node, TASK, store) is True

    events = _events(store)
    started = events[0].payload
    assert isinstance(started, RunStarted)
    assert (started.scenario_id, started.setting, started.seed) == ("trial_1_task_1", "A", 0)
    assert started.geometry_source == "ground_truth"
    assert started.schema_version == SCHEMA_VERSION
    assert started.task == TASK
    skills = [e.payload for e in events if isinstance(e.payload, SkillStarted | SkillFinished)]
    assert [(type(p).__name__, p.skill) for p in skills] == [
        (kind, skill)
        for skill in ("approach", "align", "insert_scripted", "verify")
        for kind in ("SkillStarted", "SkillFinished")
    ]
    assert all(p.ok for p in skills if isinstance(p, SkillFinished))
    samples = [e.payload for e in events if isinstance(e.payload, SignalSampled)]
    assert [s.step for s in samples] == list(range(1, len(samples) + 1))  # one per step
    assert len(samples) >= 100  # 100 approach steps of 50 ms
    end = events[-1].payload
    assert isinstance(end, RunFinished) and end.outcome == "success"


def test_the_signals_come_from_ground_truth_tf_and_the_tared_wrench(store: EventStore) -> None:
    node = FakeNode(task=TASK)
    run_goal(node, TASK, store)

    last = [e.payload for e in _events(store) if isinstance(e.payload, SignalSampled)][-1]
    plug_z = node.tcp.position[2] - PLUG_BELOW_TCP_M
    assert last.height_mm == pytest.approx((plug_z - PORT.position[2]) * 1000.0)
    assert last.force_n == pytest.approx(0.5)  # 1.5 N raw minus the 1.0 N tare
    assert last.wrench_valid


@pytest.mark.parametrize(
    ("age_s", "valid"), [(WRENCH_FRESH_S, True), (WRENCH_FRESH_S + 0.001, False)]
)
def test_a_wrench_older_than_the_freshness_bound_is_invalid(age_s: float, valid: bool) -> None:
    world = AicWorld(FakeNode(task=TASK, wrench_age_s=age_s), TASK)

    assert world.signals().wrench_valid is valid
    assert world.signals().force_n == (0.5 if valid else 0.0)


def test_without_controller_state_the_wrench_is_invalid_and_hold_is_a_zero_twist() -> None:
    node = FakeNode(task=TASK, has_controller_state=False)
    world = AicWorld(node, TASK)

    assert not world.signals().wrench_valid  # no tare to subtract
    world.command(HOLD)
    assert node.sent[-1].mode == "velocity"
    assert node.sent[-1].linear_m_s == (0.0, 0.0, 0.0)


@pytest.mark.parametrize(
    ("stall", "poll"), [(0.0, 0.002), (math.inf, 0.002), (1.0, 0.0), (1.0, 2.0), (1.0, math.nan)]
)
def test_the_world_rejects_bad_watchdog_settings(stall: float, poll: float) -> None:
    with pytest.raises(ValueError):
        AicWorld(FakeNode(task=TASK), TASK, stall_wall_s=stall, poll_wall_s=poll)


def test_the_motion_updates_are_position_targets_interpolated_to_above_the_port(
    store: EventStore,
) -> None:
    node = FakeNode(task=TASK)
    run_goal(node, TASK, store)

    assert node.sent and all(m.mode == "position" for m in node.sent)
    assert all(m.frame_id == "base_link" for m in node.sent)
    # Approach: 100 targets, each a fraction closer to CheatCode's pose for z_offset 0.2 (TCP at
    # port + 0.2 - (TCP - tip), see test_aic_convert); then HOLD at the measured pose.
    approach = [m.position_m[2] for m in node.sent[:100]]
    assert approach[0] == pytest.approx(0.45)  # fraction 0: the first target is where it is
    assert all(b <= a + 1e-12 for a, b in itertools.pairwise(approach))  # descending
    assert approach[-1] == pytest.approx(PORT.position[2] + 0.2 - PLUG_BELOW_TCP_M, abs=1e-3)
    xy = [m.position_m[:2] for m in node.sent[:100]]
    assert xy[-1] == pytest.approx(PORT.position[:2], abs=1e-3)


def test_the_clock_advances_by_the_sim_time_the_node_reports(store: EventStore) -> None:
    # 0.03 s of sim time per poll: a 50 ms step takes two polls and reports 0.06 s.
    node = FakeNode(task=TASK, sim_per_sleep_s=0.03)
    run_goal(node, TASK, store)

    times = [e.ts for e in _events(store) if isinstance(e.payload, SignalSampled)]
    assert len(times) > 2
    assert all(round(b - a, 9) == 0.06 for a, b in itertools.pairwise(times))
    assert times[0] == pytest.approx(0.06)  # relative to the goal's start, not the node's 100 s


def test_sim_time_not_advancing_stops_the_run_as_sim_clock_stalled(store: EventStore) -> None:
    node = FakeNode(task=TASK, sim_per_sleep_s=0.0)

    assert run_goal(node, TASK, store, stall_wall_s=2.0) is False

    end = _events(store)[-1].payload
    assert isinstance(end, RunFinished)
    assert (end.outcome, end.reason) == ("aborted", "sim_clock_stalled")
    assert 2.0 <= node.wall_s <= 2.1  # gave up at the wall-clock bound, not before or long after


def test_a_slow_sim_is_not_a_stall_while_each_step_ends_within_the_bound(
    store: EventStore,
) -> None:
    # A low real-time factor: a 50 ms step takes 0.1 s of wall time, under the 0.5 s bound.
    node = FakeNode(task=TASK, sim_per_sleep_s=0.005)

    assert run_goal(node, TASK, store, stall_wall_s=0.5, poll_wall_s=0.01) is True


def test_a_sim_creeping_too_slowly_to_end_a_step_within_the_bound_is_a_stall(
    store: EventStore,
) -> None:
    # Sim time still moves, but a step would take 25 s of wall time: as good as paused.
    node = FakeNode(task=TASK, sim_per_sleep_s=0.0001)

    assert run_goal(node, TASK, store, stall_wall_s=1.0, poll_wall_s=0.05) is False

    end = _events(store)[-1].payload
    assert isinstance(end, RunFinished) and end.reason == "sim_clock_stalled"
    assert 1.0 <= node.wall_s <= 1.05


def test_sim_time_going_backwards_stops_the_run_as_sim_time_reset(store: EventStore) -> None:
    node = FakeNode(task=TASK, reset_after_sleeps=40)

    assert run_goal(node, TASK, store) is False

    end = _events(store)[-1].payload
    assert isinstance(end, RunFinished)
    assert (end.outcome, end.reason) == ("aborted", "sim_time_reset")


@pytest.mark.parametrize("frame", ["port", "plug", "gripper/tcp"])
def test_missing_ground_truth_stops_the_run(store: EventStore, frame: str) -> None:
    node = FakeNode(task=TASK)
    hidden = {"port": node.port_frame, "plug": node.plug_frame}.get(frame, frame)
    node.hide_frames = frozenset({hidden})

    assert run_goal(node, TASK, store) is False

    events = _events(store)
    assert isinstance(events[0].payload, RunStarted)
    end = events[-1].payload
    assert isinstance(end, RunFinished)
    assert (end.outcome, end.reason) == ("aborted", "ground_truth_unavailable")
    assert node.sent == []  # nothing moved without ground truth


def test_a_goal_runs_its_own_event_loop_in_the_policy_thread(tmp_path: Path) -> None:
    # aic_model calls insert_cable in its own thread, one goal after another; the store is
    # opened in that thread (a sqlite connection stays in its thread), as RuntimePolicy does.
    db = tmp_path / "aic.sqlite"
    results: list[bool] = []

    def goal(task_id: str) -> None:
        task = TASK.model_copy(update={"id": task_id})
        with EventStore(db) as store:
            results.append(run_goal(FakeNode(task=task), task, store))

    for task_id in ("trial_1_task_1", "trial_1_task_2"):
        thread = threading.Thread(target=goal, args=(task_id,))
        thread.start()
        thread.join(timeout=60)
        assert not thread.is_alive()

    assert results == [True, True]
    with EventStore(db) as store:
        runs = sorted(r.scenario_id for r in store.runs())
    assert runs == ["trial_1_task_1", "trial_1_task_2"]


def test_a_second_goal_for_the_same_task_is_a_new_attempt(store: EventStore) -> None:
    run_goal(FakeNode(task=TASK), TASK, store)
    run_goal(FakeNode(task=TASK), TASK, store)

    assert [r.run_id for r in store.runs()] == ["trial_1_task_1/A/0/a1", "trial_1_task_1/A/0/a2"]
