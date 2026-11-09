"""CheatCode's two phases as Phase 0 skills (approach, align, insert_scripted, verify, retract)
and the full setting A tree on a fake cell whose arm follows the commanded targets."""

from __future__ import annotations

import itertools

import py_trees
import pytest
from aic_fakes import FakeNode

from adapters.aic import run as aic_run
from adapters.aic.convert import Pose
from adapters.aic.geometry import XYIntegrator, gripper_target
from adapters.aic.run import run_goal
from adapters.aic.skills import (
    ALIGN_STEPS,
    INSERT_STEPS,
    AicAlign,
    AicApproach,
    AicInsertScripted,
    AicRetract,
    AicVerify,
    descent_offsets,
)
from adapters.aic.world import AicWorld
from cellrt.executor.behaviours import ExecEnv, SkillBehaviour
from cellrt.runtime.events import (
    Event,
    MonitorVerdict,
    RunFinished,
    SkillFinished,
    SkillProgress,
    SkillStarted,
)
from cellrt.runtime.store import EventStore
from cellrt.runtime.task import Task

TASK = Task(id="trial_1_task_1", time_limit=180.0)
SKILLS = ["approach", "align", "insert_scripted", "verify"]


def _events(store: EventStore) -> list[Event]:
    (run,) = store.runs()
    return store.events(run.run_id)


def _end(store: EventStore) -> RunFinished:
    end = _events(store)[-1].payload
    assert isinstance(end, RunFinished)
    return end


# --- the ported pose computation -------------------------------------------------------------


def _cheatcode_descent() -> list[float]:
    """CheatCode.py:217, :239-252, verbatim but for the commands: what it would send."""
    z_offset = 0.2
    sent = []
    while True:
        if z_offset < -0.015:
            break
        z_offset -= 0.0005
        sent.append(z_offset)
    return sent


def test_the_descent_is_cheatcodes_430_offsets_split_at_10_mm() -> None:
    align, insert = descent_offsets()

    assert align + insert == _cheatcode_descent()
    assert (len(align), len(insert)) == (ALIGN_STEPS, INSERT_STEPS) == (380, 50)
    assert align[0] == pytest.approx(0.1995) and align[-1] == pytest.approx(0.010)
    assert insert[0] == pytest.approx(0.0095) and insert[-1] == pytest.approx(-0.015)
    assert all(b == pytest.approx(a - 0.0005) for a, b in itertools.pairwise(align + insert))


def test_a_descent_target_matches_cheatcodes_formula_on_recorded_inputs() -> None:
    # Worked by hand from CheatCode.py:133-176 with the integrator carried over:
    # tip error (0.5 - 0.502, 0.1 - 0.099) = (-0.002, 0.001); integrator (0.01, -0.02) + error
    # = (0.008, -0.019); target x = 0.5 + 0.15 * 0.008 = 0.5012, y = 0.1 + 0.15 * -0.019
    # = 0.09715, z = 0.05 + 0.1 - (0.204 - 0.2) = 0.146.
    port = Pose(position=(0.5, 0.1, 0.05))
    plug = Pose(position=(0.502, 0.099, 0.2))
    gripper = Pose(position=(0.502, 0.099, 0.204))

    target, acc = gripper_target(
        port, plug, gripper, z_offset_m=0.1, integrator=XYIntegrator(x=0.01, y=-0.02)
    )

    assert target.position == pytest.approx((0.5012, 0.09715, 0.146))
    assert (acc.x, acc.y) == pytest.approx((0.008, -0.019))


# --- the full setting A run ------------------------------------------------------------------


def test_a_full_setting_a_run_on_the_fake_cell_succeeds_without_verdicts(
    store: EventStore,
) -> None:
    node = FakeNode(task=TASK)

    assert run_goal(node, TASK, store) is True

    events = _events(store)
    assert not [e for e in events if isinstance(e.payload, MonitorVerdict)]
    started = [e.payload.skill for e in events if isinstance(e.payload, SkillStarted)]
    finished = [e.payload for e in events if isinstance(e.payload, SkillFinished)]
    assert started == SKILLS
    assert [(f.skill, f.ok) for f in finished] == [(s, True) for s in SKILLS]
    assert (_end(store).outcome, _end(store).reason) == ("success", "task complete")
    # CheatCode's 32 s of sim time (5 s approach, 430 x 0.05 s descent, 5 s settle) less the
    # insertion: with CheatCode's quirk the plug reaches its seat during align, as in #28, so
    # insert_scripted ends on its first step.
    assert _end(store).duration == pytest.approx(5.0 + ALIGN_STEPS * 0.05 + 5.0, abs=0.2)


def _skill(store: EventStore, name: str) -> tuple[Event, Event]:
    """The skill's start and finish events."""
    events = _events(store)
    (started,) = [
        e for e in events if isinstance(e.payload, SkillStarted) and e.payload.skill == name
    ]
    (finished,) = [
        e for e in events if isinstance(e.payload, SkillFinished) and e.payload.skill == name
    ]
    return started, finished


# Seated heights measured in #28: an SFP plug on its seat reads -0.98 mm, an SC plug +0.07 mm.
SEATED_MM = [pytest.param(-0.98, id="sfp"), pytest.param(0.07, id="sc")]


@pytest.mark.parametrize("seat_mm", SEATED_MM)
def test_a_plug_seated_before_insertion_ends_it_on_its_first_step(
    store: EventStore, seat_mm: float
) -> None:
    # #28: the plug was seated during align, insertion kept pressing it into the seat at
    # 12-13 N with no depth progress, and the stuck rule called that INSERT_STUCK.
    node = FakeNode(task=TASK, seat_height_m=seat_mm / 1000, raw_force=(0.0, 0.0, 13.5))

    assert run_goal(node, TASK, store) is True

    assert not [e for e in _events(store) if isinstance(e.payload, MonitorVerdict)]
    started, finished = _skill(store, "insert_scripted")
    # It sends nothing: the finish is recorded on the next tick, one step later.
    assert finished.payload.ok and finished.ts - started.ts == pytest.approx(0.05)
    assert finished.payload.detail == {"height_mm": seat_mm, "seated": True}
    _, verify = _skill(store, "verify")
    assert verify.payload.ok and verify.payload.detail["height_mm"] == seat_mm


def test_insertion_pushes_a_plug_on_until_it_rests_on_its_seat(store: EventStore) -> None:
    # The tip hangs 2.1 mm under the TCP; with CheatCode's quirk it is at 10 - 2 x 2.1 = 5.8 mm
    # when align ends, then 0.5 mm lower per step. It is in the seated band from 0.8 mm (10
    # steps) but still going down, so insertion goes on: it reaches the SFP seat at step 14
    # and stays there; at step 15 it has not moved, so insertion ends, recorded a tick later.
    node = FakeNode(task=TASK, plug_below_tcp_m=0.0021, seat_height_m=-0.00098)

    assert run_goal(node, TASK, store) is True

    started, finished = _skill(store, "insert_scripted")
    assert finished.ts - started.ts == pytest.approx(16 * 0.05)
    assert finished.payload.detail == {"height_mm": -0.98, "seated": True}


def test_a_plug_that_never_rests_on_a_seat_gets_the_whole_descent_and_fails_verify(
    store: EventStore,
) -> None:
    # No seat: the tip goes on down through the port frame, 70 mm under it when align ends
    # and 95 mm at CheatCode's last offset, under the seated band all the way.
    node = FakeNode(task=TASK, seat_height_m=None)

    assert run_goal(node, TASK, store) is False

    started, finished = _skill(store, "insert_scripted")
    assert finished.ts - started.ts == pytest.approx((INSERT_STEPS + 1) * 0.05)
    assert finished.payload.ok
    assert finished.payload.detail == {"height_mm": -95.0, "seated": False}
    assert _end(store).reason.startswith("verify failed")
    assert _end(store).duration == pytest.approx(5.0 + 430 * 0.05 + 5.0, abs=0.2)


def test_a_plug_jammed_at_the_sc_entrance_during_insertion_is_still_insert_stuck(
    store: EventStore,
) -> None:
    # #28's true positive: the SC plug stuck at its entrance (13.46 mm), pressed at 12.5 N.
    # It is not seated, so insertion pushes on and the stuck rule still fires.
    node = FakeNode(task=TASK, seat_height_m=0.01346, raw_force=(0.0, 0.0, 13.5))

    assert run_goal(node, TASK, store) is False

    verdicts = [e.payload for e in _events(store) if isinstance(e.payload, MonitorVerdict)]
    assert [(v.skill, v.failure_class) for v in verdicts] == [("insert_scripted", "INSERT_STUCK")]
    assert _end(store).reason == "insert_scripted failed: INSERT_STUCK"


def test_the_seated_band_is_in_the_config() -> None:
    # 1 mm over the SC seat (+0.07 mm), 2 mm under the SFP seat (-0.98 mm), far under the SC
    # entrance (13.64 mm, #28), and 2 mm off the port axis.
    band = {"max_height_mm": 1.0, "min_height_mm": -3.0, "max_xy_mm": 2.0}
    assert AicInsertScripted.config()["seated"] == band
    assert AicInsertScripted.config()["still_mm"] == 0.1
    assert AicVerify.config()["seated"] == band


@pytest.mark.parametrize(
    "height_m",
    [
        pytest.param(0.002, id="sprung-back-2mm"),
        pytest.param(0.01346, id="jammed-at-the-sc-entrance"),  # #28's SC jam
    ],
)
def test_a_plug_that_is_not_seated_at_the_end_makes_verify_fail(
    store: EventStore, height_m: float
) -> None:
    # The connector springs back out 1 s into the settle. (A plug stopped short during the
    # descent trips the Monitor's insertion stall rule first.)
    node = FakeNode(
        task=TASK,
        plug_pops_out_after_s=5.0 + ALIGN_STEPS * 0.05 + 1.0,
        plug_pops_out_to_m=height_m,
    )

    assert run_goal(node, TASK, store) is False

    end = _end(store)
    assert end.outcome == "failed"
    assert end.reason.startswith("verify failed")
    assert not [e for e in _events(store) if isinstance(e.payload, MonitorVerdict)]


def test_a_plug_on_its_seat_height_but_off_the_port_axis_fails_verify(
    store: EventStore,
) -> None:
    # At the seat's height, 3 mm to the side (in a neighbouring port, say): not seated.
    node = FakeNode(
        task=TASK,
        plug_pops_out_after_s=5.0 + ALIGN_STEPS * 0.05 + 1.0,
        plug_pops_out_to_m=0.0,
        plug_pops_out_xy_m=(0.003, 0.0),
    )

    assert run_goal(node, TASK, store) is False

    _, verify = _skill(store, "verify")
    assert verify.payload.detail == {"height_mm": 0.0, "reason": "not seated"}


# --- timeouts --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("skill", "nominal_s"),
    [
        (AicApproach, 100 * 0.05),
        (AicAlign, 380 * 0.05),
        (AicInsertScripted, 50 * 0.05),
        (AicVerify, 5.0),
        (AicRetract, 10 * 0.05),
    ],
)
def test_each_skills_timeout_is_twice_its_nominal_duration(skill: type, nominal_s: float) -> None:
    assert skill.timeout_s == pytest.approx(2 * nominal_s)
    assert skill.config()["timeout_s"] == skill.timeout_s


def test_insertion_runs_under_the_monitors_insertion_rules() -> None:
    assert AicInsertScripted.config()["monitor_profile"] == "insertion"
    assert AicAlign.config()["monitor_profile"] == "motion"


def test_the_xy_integrator_corrects_an_offset_plug_and_carries_into_insertion(
    store: EventStore,
) -> None:
    # The plug hangs 3 mm off the TCP axis. align's integrator must bring it within 1 mm, and
    # insert_scripted must carry that correction on: started from zero, the plug would sit 3 mm
    # off for several steps and the insertion rules would call POSE_ERROR. The tip hangs 2.1 mm
    # under the TCP, so insertion runs 10 steps before the plug is seated.
    node = FakeNode(task=TASK, plug_xy_offset_m=(0.003, 0.0), plug_below_tcp_m=0.0021)

    assert run_goal(node, TASK, store) is True

    assert not [e for e in _events(store) if isinstance(e.payload, MonitorVerdict)]
    (align,) = [
        e.payload
        for e in _events(store)
        if isinstance(e.payload, SkillFinished) and e.payload.skill == "align"
    ]
    assert align.detail["xy_error_mm"] < 1.0


def test_align_that_never_gets_within_1_mm_times_out(store: EventStore) -> None:
    # The plug hangs 20 mm off the TCP in x: the integrator's correction is clipped at
    # 0.15 x 0.05 m = 7.5 mm, so |xy| never drops under 1 mm.
    node = FakeNode(task=TASK, plug_xy_offset_m=(0.02, 0.0))

    assert run_goal(node, TASK, store) is False

    end = _end(store)
    assert (end.outcome, end.reason) == ("failed", "align failed: TIMEOUT")
    (align,) = [
        e
        for e in _events(store)
        if isinstance(e.payload, SkillStarted) and e.payload.skill == "align"
    ]
    assert end.duration - align.ts == pytest.approx(AicAlign.timeout_s, abs=0.1)


# --- retract ---------------------------------------------------------------------------------


def test_retract_raises_the_tcp_5_mm_straight_up_from_where_it_is(
    store: EventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def retract_only(world: AicWorld, env: ExecEnv) -> py_trees.behaviour.Behaviour:
        retract = SkillBehaviour(AicRetract(world.geometry), env)
        return py_trees.composites.Sequence(name="retract", memory=True, children=[retract])

    monkeypatch.setattr(aic_run, "_tree", retract_only)
    node = FakeNode(task=TASK)
    x, y, z = node.tcp.position

    assert run_goal(node, TASK, store) is True

    assert len(node.sent) == 10 + 1  # 10 steps of 0.5 mm, then HOLD where it ended
    assert node.tcp.position == pytest.approx((x, y, z + 0.005))


# --- feedback --------------------------------------------------------------------------------


def test_feedback_reports_each_skills_start_and_finish(store: EventStore) -> None:
    node = FakeNode(task=TASK)
    run_goal(node, TASK, store)

    messages = [m for _, m in node.feedback]
    for skill in SKILLS:
        assert f"{skill} started" in messages
        assert f"{skill} finished" in messages
    assert messages.index("approach started") < messages.index("approach finished")


def test_progress_feedback_is_sent_at_most_once_per_sim_second(
    store: EventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # approach logs progress every 1.25 s, and retract right after it every 0.125 s, so the bound
    # is hit. (Setting A no longer hits it: insert_scripted ends once the plug is seated.)
    def approach_then_retract(world: AicWorld, env: ExecEnv) -> py_trees.behaviour.Behaviour:
        children = [
            SkillBehaviour(AicApproach(world.geometry), env),
            SkillBehaviour(AicRetract(world.geometry), env),
        ]
        return py_trees.composites.Sequence(name="approach", memory=True, children=children)

    monkeypatch.setattr(aic_run, "_tree", approach_then_retract)
    node = FakeNode(task=TASK)
    run_goal(node, TASK, store)

    progress = [(t, m) for t, m in node.feedback if "%" in m]
    logged = [e for e in _events(store) if isinstance(e.payload, SkillProgress)]
    gaps = [b[0] - a[0] for a, b in itertools.pairwise(progress)]
    assert progress and min(gaps) >= 1.0 - 1e-9
    # The bound is hit: some progress is logged but not sent.
    assert len(progress) < len(logged)


def test_a_failing_feedback_call_does_not_stop_the_run(store: EventStore) -> None:
    node = FakeNode(task=TASK, fail_feedback=True)

    assert run_goal(node, TASK, store) is True
