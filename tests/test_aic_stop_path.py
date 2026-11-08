"""Every way a run in the cell ends stops the arm the same way (ADR 012): the running skill's
lease is revoked, HOLD goes out as a position target at the measured TCP pose, the run ends
with a reason code, `insert_cable` returns False, and at run end the stop is confirmed."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from aic_fakes import SECRET, FakeNode

from adapters.aic import run as aic_run
from adapters.aic.run import run_goal
from cellrt.replay import replay_lines
from cellrt.runtime.events import (
    CommandDropped,
    Event,
    HoldChecked,
    RunFinished,
    SignalSampled,
    SkillCancelled,
    SkillFinished,
)
from cellrt.runtime.store import EventStore
from cellrt.runtime.task import Task
from cellrt.skills.base import SkillContext, SkillResult
from cellrt.world.base import PoseTarget

TASK = Task(id="trial_1_task_1", time_limit=180.0)


def _events(store: EventStore) -> list[Event]:
    (run,) = store.runs()
    return store.events(run.run_id)


def _end(store: EventStore) -> RunFinished:
    end = _events(store)[-1].payload
    assert isinstance(end, RunFinished)
    return end


def _assert_held(node: FakeNode) -> None:
    """The last motion update is HOLD: a position target at the TCP pose measured just before
    it. approach's targets move on every step, so none is where the TCP already is (the fake
    arm reaches each target): this fails if the stop sent nothing after approach's last one."""
    last, measured = node.sent[-1], node.tcp_at_send[-1]
    assert last.mode == "position"
    assert (last.position_m, last.orientation) == (measured.position, measured.orientation)
    assert last.linear_m_s == (0.0, 0.0, 0.0)


def _assert_skill_ended(store: EventStore) -> None:
    """approach was stopped (its lease revoked) before the run ended."""
    types = [type(e.payload) for e in _events(store)]
    ended = [i for i, t in enumerate(types) if t in (SkillCancelled, SkillFinished)]
    assert ended and ended[-1] < types.index(RunFinished)


# --- one test per stop reason ----------------------------------------------------------------

GOAL_STOPS = ["goal_cancel_requested", "goal_aborted", "node_deactivated"]


@pytest.mark.parametrize("code", GOAL_STOPS)
def test_a_goal_stop_holds_the_arm_and_ends_the_run_with_its_code(
    store: EventStore, code: str
) -> None:
    node = FakeNode(task=TASK, goal_stop=(code, 40))

    assert run_goal(node, TASK, store) is False

    assert (_end(store).outcome, _end(store).reason) == ("aborted", code)
    _assert_held(node)
    _assert_skill_ended(store)


@pytest.mark.parametrize(
    ("node_args", "code"),
    [
        ({"stall_after_sleeps": 40}, "sim_clock_stalled"),  # after approach has moved
        ({"reset_after_sleeps": 40}, "sim_time_reset"),
        ({"fail_transform_after_sleeps": 40}, "node_error"),
    ],
)
def test_a_world_stop_holds_the_arm_and_ends_the_run_with_its_code(
    store: EventStore, node_args: dict[str, Any], code: str
) -> None:
    node = FakeNode(task=TASK, **node_args)

    assert run_goal(node, TASK, store, stall_wall_s=0.5) is False

    assert (_end(store).outcome, _end(store).reason) == ("aborted", code)
    _assert_held(node)
    _assert_skill_ended(store)


def test_the_time_limit_holds_the_arm(store: EventStore) -> None:
    task = TASK.model_copy(update={"time_limit": 1.0})
    node = FakeNode(task=task)

    assert run_goal(node, task, store) is False

    assert (_end(store).outcome, _end(store).reason) == ("aborted", "time limit reached")
    _assert_held(node)
    _assert_skill_ended(store)


def test_a_monitor_preemption_holds_the_arm(store: EventStore) -> None:
    # 30 N raw (29 N tared) from the start: FORCE_LIMIT once 0.5 s are summed over 20 N. In
    # setting A a verdict fails the task (the core's outcome for a known failure).
    node = FakeNode(task=TASK, raw_force=(0.0, 0.0, 30.0))

    assert run_goal(node, TASK, store) is False

    end = _end(store)
    assert (end.outcome, end.reason) == ("failed", "approach failed: FORCE_LIMIT")
    _assert_held(node)
    _assert_skill_ended(store)


def test_a_skill_timeout_holds_the_arm(store: EventStore) -> None:
    # Every step reports 0.11 s of sim time: approach's 100 steps would take 11 s, past its
    # 10 s timeout.
    node = FakeNode(task=TASK, sim_per_sleep_s=0.11)

    assert run_goal(node, TASK, store) is False

    end = _end(store)
    assert (end.outcome, end.reason) == ("failed", "approach failed: TIMEOUT")
    _assert_held(node)
    _assert_skill_ended(store)


def test_a_failing_motion_command_holds_the_arm_and_records_the_type_only(
    store: EventStore,
) -> None:
    node = FakeNode(task=TASK, fail_send_at=10)  # the 10th motion update raises, once

    assert run_goal(node, TASK, store) is False

    assert (_end(store).outcome, _end(store).reason) == ("aborted", "approach raised RuntimeError")
    _assert_held(node)
    _assert_skill_ended(store)


def test_an_exception_in_the_runtime_holds_the_arm_and_ends_the_run(
    store: EventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(root: Any, env: Any, task: Task) -> Any:
        env.world.command(PoseTarget(x_mm=300.0, y_mm=0.0, z_mm=400.0))
        raise RuntimeError(SECRET)

    monkeypatch.setattr(aic_run, "run_task", broken)
    node = FakeNode(task=TASK)

    assert run_goal(node, TASK, store) is False

    assert (_end(store).outcome, _end(store).reason) == ("aborted", "runtime raised RuntimeError")
    _assert_held(node)


def test_no_upstream_text_reaches_the_store(store: EventStore) -> None:
    for node in (
        FakeNode(task=TASK, fail_send_at=10),
        FakeNode(task=TASK, fail_transform_after_sleeps=40),
    ):
        run_goal(node, TASK, store)

    for run in store.runs():
        for event in store.events(run.run_id):
            assert SECRET not in event.payload.model_dump_json()


# --- the lease, and how fast a cancel lands --------------------------------------------------


class _StubbornApproach:
    """Breaks the contract on purpose: commands again after being cancelled."""

    name = "approach"
    monitor_profile = "motion"
    timeout_s = 10.0

    def __init__(self, geometry: Callable[[], object]) -> None:
        pass

    @classmethod
    def config(cls) -> dict[str, object]:
        return {"type": cls.__qualname__}

    def preconditions(self, state: object) -> bool:
        return True

    async def run(self, ctx: SkillContext) -> SkillResult:
        try:
            while True:
                ctx.world.command(PoseTarget(x_mm=300.0, y_mm=0.0, z_mm=400.0))
                await ctx.clock.sleep(ctx.period_s)
        finally:
            ctx.world.command(PoseTarget(x_mm=999.0, y_mm=999.0, z_mm=999.0))


def test_a_command_from_a_skill_whose_lease_was_revoked_never_reaches_the_node(
    store: EventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(aic_run, "AicApproach", _StubbornApproach)
    node = FakeNode(task=TASK, goal_stop=("goal_cancel_requested", 20))

    assert run_goal(node, TASK, store) is False

    assert all(m.position_m != (0.999, 0.999, 0.999) for m in node.sent)
    assert any(isinstance(e.payload, CommandDropped) for e in _events(store))


@pytest.mark.parametrize("at_sleep", [40, 41])  # at a step boundary, and mid-step
def test_a_cancel_is_acted_on_within_one_step(store: EventStore, at_sleep: int) -> None:
    node = FakeNode(task=TASK, goal_stop=("goal_cancel_requested", at_sleep))

    run_goal(node, TASK, store)

    assert node.stop_sim_s is not None  # the sim time at which the cancel happened
    samples = [e for e in _events(store) if isinstance(e.payload, SignalSampled)]
    # Relative to the goal's start (the node's clock started at 100 s).
    cancel_at = node.stop_sim_s - 100.0
    assert samples[-1].ts <= cancel_at + 0.05 + 1e-9


def test_no_goal_handle_fails_fast_without_moving(store: EventStore) -> None:
    node = FakeNode(task=TASK, goal_handle_available=False)

    assert run_goal(node, TASK, store) is False

    assert (_end(store).outcome, _end(store).reason) == ("aborted", "aic_goal_handle_unavailable")
    assert node.sent == []
    assert not any(isinstance(e.payload, SignalSampled) for e in _events(store))


# --- confirming the stop at run end ----------------------------------------------------------


def _hold(store: EventStore) -> HoldChecked:
    (hold,) = [e.payload for e in _events(store) if isinstance(e.payload, HoldChecked)]
    return hold


def test_a_still_arm_is_confirmed_after_three_slow_samples(store: EventStore) -> None:
    node = FakeNode(task=TASK)

    assert run_goal(node, TASK, store) is True

    hold = _hold(store)
    assert (hold.result, hold.samples, hold.max_speed_mm_s) == ("hold_confirmed", 3, 0.0)
    assert hold.waited_s == pytest.approx(0.15)


def test_an_arm_that_slows_down_late_is_confirmed_when_it_does(
    store: EventStore, tmp_path: Path
) -> None:
    # 8 mm/s until 10 polls (5 steps) into the check, then still. A first run counts the polls
    # the task itself takes.
    probe = FakeNode(task=TASK)
    with EventStore(tmp_path / "probe.sqlite") as probe_store:
        run_goal(probe, TASK, probe_store)
    probe.sleeps -= 2 * 3  # less the 3 steps of the probe's own hold check
    node = FakeNode(task=TASK, tcp_speed_mm_s=8.0, slow_after_sleeps=probe.sleeps + 10)

    run_goal(node, TASK, store)

    hold = _hold(store)
    assert hold.result == "hold_confirmed"
    assert hold.max_speed_mm_s == 8.0
    assert hold.samples == 4 + 3  # 4 samples at 8 mm/s, then 3 still


def test_an_arm_still_moving_at_the_deadline_is_unconfirmed(store: EventStore) -> None:
    node = FakeNode(task=TASK, tcp_speed_mm_s=12.5)

    assert run_goal(node, TASK, store) is True  # the task itself succeeded

    hold = _hold(store)
    assert (hold.result, hold.max_speed_mm_s) == ("hold_unconfirmed", 12.5)
    assert hold.waited_s == pytest.approx(1.0)  # the deadline, in sim time
    assert hold.samples == 20


def test_after_the_sim_clock_stopped_the_hold_cannot_be_confirmed(store: EventStore) -> None:
    node = FakeNode(task=TASK, sim_per_sleep_s=0.0)

    run_goal(node, TASK, store, stall_wall_s=0.5)

    hold = _hold(store)
    assert (hold.result, hold.samples) == ("hold_unconfirmed", 0)
    assert node.wall_s <= 0.5 + 0.01  # no second wait on a clock known to be stopped


def test_a_sim_clock_that_stops_during_the_check_ends_it_within_the_stall_bound(
    store: EventStore,
) -> None:
    # The goal is cancelled at sleep 40; the sim stops 2 sleeps later, inside the check.
    node = FakeNode(task=TASK, goal_stop=("goal_cancel_requested", 40), stall_after_sleeps=44)

    run_goal(node, TASK, store, stall_wall_s=0.5)

    hold = _hold(store)
    assert hold.result == "hold_unconfirmed"
    assert hold.samples < 3
    wall_at_cancel = 40 * 0.002
    assert node.wall_s < wall_at_cancel + 0.5 + 0.1  # one stall bound and a few polls, no more


def test_a_run_stopped_for_missing_ground_truth_still_confirms_the_hold(
    store: EventStore,
) -> None:
    node = FakeNode(task=TASK, hide_frames_after_sleeps=40)

    assert run_goal(node, TASK, store) is False

    assert _end(store).reason == "ground_truth_unavailable"
    _assert_held(node)
    assert (_hold(store).result, _hold(store).samples) == ("hold_confirmed", 3)


def test_after_a_runtime_crash_a_live_skill_records_nothing_after_run_finished(
    store: EventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cellrt.executor.runner as core_runner

    original = core_runner._record_signal

    def failing(env: Any, step: int) -> None:
        if step == 5:
            raise RuntimeError(SECRET)
        original(env, step)

    monkeypatch.setattr(core_runner, "_record_signal", failing)
    monkeypatch.setattr(aic_run, "AicApproach", _StubbornApproach)
    node = FakeNode(task=TASK)

    assert run_goal(node, TASK, store) is False

    events = _events(store)
    assert isinstance(events[-1].payload, RunFinished)
    assert events[-1].payload.reason == "runtime raised RuntimeError"
    assert any(isinstance(e.payload, CommandDropped) for e in events)  # dropped before the end


def test_the_duration_leaves_out_the_hold_check(store: EventStore) -> None:
    node = FakeNode(task=TASK, tcp_speed_mm_s=12.5)  # the check runs its full 1.0 s

    run_goal(node, TASK, store)

    end = _end(store)
    last_sample = [e for e in _events(store) if isinstance(e.payload, SignalSampled)][-1]
    assert end.duration == pytest.approx(last_sample.ts)


def test_the_hold_check_comes_before_run_finished(store: EventStore) -> None:
    node = FakeNode(task=TASK, goal_stop=("goal_cancel_requested", 40))
    run_goal(node, TASK, store)

    types = [type(e.payload).__name__ for e in _events(store)]
    assert types[-2:] == ["HoldChecked", "RunFinished"]
    assert _hold(store).result == "hold_confirmed"  # a cancelled goal still checks the stop


def test_replay_reads_a_cell_run_to_its_end(store: EventStore) -> None:
    node = FakeNode(task=TASK, goal_stop=("goal_cancel_requested", 40))
    run_goal(node, TASK, store)

    lines = list(replay_lines(_events(store), signals=True))

    assert len(lines) == len(_events(store))
    assert "hold_checked" in lines[-2] and "hold_confirmed (max 0.0mm/s, 3 samples" in lines[-2]
    assert lines[-1].split()[2:4] == ["run_finished", "aborted:"]
