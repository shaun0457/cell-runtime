"""CheatCode's two phases as Phase 0 skills (approach, align, insert_scripted, verify, retract)
and the full setting A tree on a fake cell whose arm follows the commanded targets."""

from __future__ import annotations

import itertools

import py_trees
import pytest
from aic_fakes import PLUG_BELOW_TCP_M, FakeNode

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
    # About CheatCode's 32 s of sim time: 5 s approach, 430 x 0.05 s descent, 5 s settle.
    assert _end(store).duration == pytest.approx(5.0 + 430 * 0.05 + 5.0, abs=0.2)


def test_the_plug_ends_inserted_to_cheatcodes_depth(store: EventStore) -> None:
    node = FakeNode(task=TASK)
    run_goal(node, TASK, store)

    (verify,) = [
        e.payload
        for e in _events(store)
        if isinstance(e.payload, SkillFinished) and e.payload.skill == "verify"
    ]
    # The last target is z_offset -0.015; with CheatCode's quirk the tip sits twice the
    # TCP-to-tip offset lower (geometry.py).
    assert verify.detail["depth_mm"] == pytest.approx((0.015 + 2 * PLUG_BELOW_TCP_M) * 1000)


def test_a_plug_that_is_not_seated_at_the_end_makes_verify_fail(store: EventStore) -> None:
    # The connector springs back out during the settle: the tip reads 2 mm above the port. (A
    # plug stopped short during the descent trips the Monitor's insertion stall rule first.)
    node = FakeNode(task=TASK, plug_pops_out_after_s=5.0 + 430 * 0.05 + 1.0)

    assert run_goal(node, TASK, store) is False

    end = _end(store)
    assert end.outcome == "failed"
    assert end.reason.startswith("verify failed")
    assert not [e for e in _events(store) if isinstance(e.payload, MonitorVerdict)]


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
    # off for several steps and the insertion rules would call POSE_ERROR.
    node = FakeNode(task=TASK, plug_xy_offset_m=(0.003, 0.0))

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


def test_progress_feedback_is_sent_at_most_once_per_sim_second(store: EventStore) -> None:
    node = FakeNode(task=TASK)
    run_goal(node, TASK, store)

    progress = [(t, m) for t, m in node.feedback if "%" in m]
    logged = [e for e in _events(store) if isinstance(e.payload, SkillProgress)]
    gaps = [b[0] - a[0] for a, b in itertools.pairwise(progress)]
    assert progress and min(gaps) >= 1.0 - 1e-9
    # The bound is hit: insert_scripted logs progress every 0.64 s, so some is not sent.
    assert len(progress) < len(logged)


def test_a_failing_feedback_call_does_not_stop_the_run(store: EventStore) -> None:
    node = FakeNode(task=TASK, fail_feedback=True)

    assert run_goal(node, TASK, store) is True
