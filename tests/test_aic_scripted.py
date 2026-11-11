"""CheatCode's two phases as Phase 0 skills (approach, align, insert_scripted, verify, retract)
and the full setting A tree on a fake cell whose arm follows the commanded targets."""

from __future__ import annotations

import itertools
from dataclasses import dataclass

import py_trees
import pytest
from aic_fakes import PLUG_BELOW_TCP_M, PORT, SFP_SEAT_M, FakeNode

from adapters.aic import run as aic_run
from adapters.aic.convert import MotionCommand, Pose
from adapters.aic.geometry import XYIntegrator, gripper_target
from adapters.aic.run import run_goal
from adapters.aic.skills import (
    ALIGN_STEPS,
    DESCENT_STEP_M,
    ENTRY_HOLD_ABOVE_MM,
    ENTRY_HOLD_S,
    ENTRY_SAMPLES,
    ENTRY_XY_MM,
    INSERT_STEPS,
    PORT_PROFILES,
    RETRACT_M,
    AicAlign,
    AicApproach,
    AicRetract,
    UnknownPortType,
    descent_offsets,
    seated,
)
from adapters.aic.world import AicWorld, Geometry
from cellrt.executor.behaviours import ExecEnv, SkillBehaviour
from cellrt.runtime.events import (
    Event,
    MonitorVerdict,
    RunFinished,
    RunStarted,
    SignalSampled,
    SkillFinished,
    SkillProgress,
    SkillStarted,
)
from cellrt.runtime.store import EventStore
from cellrt.runtime.task import Task

TASK = Task(id="trial_1_task_1", time_limit=180.0)  # an SFP task, as the default Task
SC_TASK = Task(
    id="trial_3_task_1", time_limit=180.0, cable_type="sc", plug_type="sc", port_type="sc"
)
SKILLS = ["approach", "align", "insert_scripted", "verify"]
SFP = PORT_PROFILES["sfp"]


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
    # insert_scripted ends on its first step. Plus align's stop above the entrance, two samples
    # that confirm the plug is on the axis (ADR 024), and the tick on which each of the four
    # skills' finish is recorded.
    expected = 5.0 + ALIGN_STEPS * 0.05 + 0.05 * (ENTRY_SAMPLES - 1) + 5.0 + 4 * 0.05
    assert _end(store).duration == pytest.approx(expected, abs=0.01)


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
SEATED_MM = [pytest.param(TASK, -0.98, id="sfp"), pytest.param(SC_TASK, 0.07, id="sc")]


@pytest.mark.parametrize(("task", "seat_mm"), SEATED_MM)
def test_a_plug_seated_before_insertion_ends_it_on_its_first_step(
    store: EventStore, task: Task, seat_mm: float
) -> None:
    # #28: the plug was seated during align, insertion kept pressing it into the seat at
    # 12-13 N with no depth progress, and the stuck rule called that INSERT_STUCK.
    node = FakeNode(task=task, seat_height_m=seat_mm / 1000, raw_force=(0.0, 0.0, 13.5))

    assert run_goal(node, task, store) is True

    assert not [e for e in _events(store) if isinstance(e.payload, MonitorVerdict)]
    started, finished = _skill(store, "insert_scripted")
    # It sends nothing: the finish is recorded on the next tick, one step later.
    assert finished.payload.ok and finished.ts - started.ts == pytest.approx(0.05)
    assert finished.payload.detail == {"height_mm": seat_mm, "seated": True}
    _, verify = _skill(store, "verify")
    assert verify.payload.ok and verify.payload.detail["height_mm"] == seat_mm


def test_insertion_pushes_a_plug_on_until_it_rests_on_its_seat(store: EventStore) -> None:
    # The tip hangs 2.1 mm under the TCP; with CheatCode's quirk it is at 10 - 2 x 2.1 = 5.8 mm
    # when align ends, then 0.5 mm lower per step. It is in the SFP seated band at step 13, at
    # -0.7 mm, but still going down, so insertion goes on: it reaches the seat (-0.98 mm) at
    # step 14 and stays there; at step 15 it has not moved, so insertion ends, recorded a tick
    # later.
    node = FakeNode(task=TASK, plug_below_tcp_m=0.0021, seat_height_m=-0.00098)

    assert run_goal(node, TASK, store) is True

    started, finished = _skill(store, "insert_scripted")
    assert finished.ts - started.ts == pytest.approx(16 * 0.05)
    assert finished.payload.detail == {"height_mm": -0.98, "seated": True}


@dataclass
class _SpringsBack(FakeNode):
    """While the TCP holds the tip 1.4-1.52 mm under the port frame (pressed past its seat;
    the bounds sit off the 0.5 mm grid of the steps), the tip reads `lift_m` above the seat:
    the connector pushing back against the insertion for one step."""

    lift_m: float = 0.00015

    @property
    def plug(self) -> Pose:
        plug = super().plug
        x, y, _ = plug.position
        pressed_to = self.tcp.position[2] - self.plug_below_tcp_m - self.port.position[2]
        if self.seat_height_m is not None and -0.00152 < pressed_to <= -0.0014:
            z = self.port.position[2] + self.seat_height_m + self.lift_m
            return Pose(position=(x, y, z), orientation=plug.orientation)
        return plug


def test_a_plug_that_rose_since_the_last_step_is_not_resting_on_its_seat(
    store: EventStore,
) -> None:
    # As in the test above, the tip reaches the SFP seat (-0.98 mm) at step 14. At step 15 it
    # springs back to -0.83 mm: in the band, but 0.15 mm higher than a step ago, so still
    # moving, where the test above ends. Insertion goes on: the tip is back on the seat at
    # step 16 (0.15 mm lower, still moving) and unmoved at step 17, so insertion ends there,
    # recorded a tick later: two steps after the test above.
    node = _SpringsBack(task=TASK, plug_below_tcp_m=0.0021, seat_height_m=-0.00098)

    assert run_goal(node, TASK, store) is True

    started, finished = _skill(store, "insert_scripted")
    assert finished.payload.detail == {"height_mm": -0.98, "seated": True}
    sprung = [
        e.payload.height_mm
        for e in _events(store)
        if isinstance(e.payload, SignalSampled) and started.ts < e.ts <= finished.ts
    ]
    assert -0.83 in [round(h, 2) for h in sprung]
    assert finished.ts - started.ts == pytest.approx(18 * 0.05)


def test_after_a_retract_insertion_steps_once_before_it_calls_the_plug_seated(
    store: EventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A retract is not a descent step: a height measured before it says nothing about whether
    # the plug is still going down under insertion. The plug is pressed onto its seat far under
    # the TCP, so it stays there while the arm rises 5 mm; the second insertion sends one step,
    # sees the plug has not moved, and ends, recorded a tick later.
    def insert_retract_insert(
        world: AicWorld, env: ExecEnv, goal: object
    ) -> py_trees.behaviour.Behaviour:
        registry = aic_run.aic_registry(world.geometry, SFP)
        names = ["approach", "align", "insert_scripted", "retract", "insert_scripted"]
        children = [SkillBehaviour(registry.get(name), env) for name in names]
        return py_trees.composites.Sequence(name="insert", memory=True, children=children)

    monkeypatch.setattr(aic_run, "_tree", insert_retract_insert)
    node = FakeNode(task=TASK)

    assert run_goal(node, TASK, store) is True

    events = _events(store)
    started = [e for e in events if isinstance(e.payload, SkillStarted)][-1]
    finished = [e for e in events if isinstance(e.payload, SkillFinished)][-1]
    assert started.payload.skill == finished.payload.skill == "insert_scripted"
    assert finished.payload.detail == {"height_mm": -0.98, "seated": True}
    assert finished.ts - started.ts == pytest.approx(2 * 0.05)


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
    # CheatCode's 430 descent steps, align's two confirming samples above the entrance and one
    # recording tick per skill, as in the full run above.
    expected = 5.0 + 430 * 0.05 + 0.05 * (ENTRY_SAMPLES - 1) + 5.0 + 4 * 0.05
    assert _end(store).duration == pytest.approx(expected, abs=0.01)


@dataclass
class _FallsShort(FakeNode):
    """The arm stops `short_m` above every position target, as the sim's arm does when
    something holds it back: what was sent and where the arm is part by that much."""

    short_m: float = 0.001

    def send(self, motion: MotionCommand) -> None:
        super().send(motion)
        if motion.mode == "position":
            x, y, z = motion.position_m
            self.tcp = Pose(position=(x, y, z + self.short_m), orientation=motion.orientation)


@pytest.mark.parametrize("short_m", [0.0, 0.001], ids=["arm-on-target", "arm-1mm-short"])
def test_a_descent_nothing_interrupts_sends_cheatcodes_targets_unchanged(
    store: EventStore, short_m: float
) -> None:
    # Only a run after a broken descent, and align after its hold above the entrance (ADR 024),
    # go on from the measured pose (F1): with no seat to stop the plug, align and
    # insert_scripted send CheatCode's targets, each its z_offset above the port less the
    # TCP-to-tip offset, even when the arm does not reach them (a run that started from the
    # measured pose would start 1 mm higher). With the arm on its targets the hold commands the
    # target last sent again, so all 430 are CheatCode's; with the arm 1 mm short, those down to
    # the hold point and insertion's are, and the hold and the rest of align go on from the arm
    # (`test_the_hold_stops_an_arm_that_trails_its_targets_above_the_entrance`). HOLDs, a target
    # at the measured pose, are left out, and repeats within float error count once.
    node = _FallsShort(task=TASK, seat_height_m=None, short_m=short_m)

    assert run_goal(node, TASK, store) is False

    sent = zip(node.tcp_at_send, node.sent, strict=True)
    z = [m.position_m[2] for at, m in sent if m.mode == "position" and m.position_m != at.position]
    moved = [b for a, b in itertools.pairwise([None, *z]) if a is None or abs(b - a) > 1e-12]
    align, insert = descent_offsets()
    expected = [PORT.position[2] + off - PLUG_BELOW_TCP_M for off in [*align, *insert]]
    start = next(i for i, v in enumerate(moved) if abs(v - expected[0]) < 1e-9)
    descent = moved[start:]
    if short_m == 0.0:
        assert descent == pytest.approx(expected, abs=1e-9)
        return
    # The tip hangs 40 mm under the TCP, so with CheatCode's quirk it is 80 mm under each
    # z_offset, here 1 mm higher. Align checks the height each offset reached before it sends
    # the next, so it sends those that leave the tip above 48.8 mm and the first that does not.
    hold_at_m = SFP.hold_at_mm / 1000
    before = 1 + sum(1 for off in align if off - 2 * PLUG_BELOW_TCP_M + short_m > hold_at_m)
    assert descent[:before] == pytest.approx(expected[:before], abs=1e-9)
    assert descent[-len(insert) :] == pytest.approx(expected[-len(insert) :], abs=1e-9)


def test_a_plug_jammed_at_the_sc_entrance_during_insertion_is_still_insert_stuck(
    store: EventStore,
) -> None:
    # #28's true positive: the SC plug stuck at its entrance (13.46 mm), pressed at 12.5 N.
    # It is not seated, so insertion pushes on and the stuck rule still fires.
    node = FakeNode(task=SC_TASK, seat_height_m=0.01346, raw_force=(0.0, 0.0, 13.5))

    assert run_goal(node, SC_TASK, store) is False

    verdicts = [e.payload for e in _events(store) if isinstance(e.payload, MonitorVerdict)]
    assert [(v.skill, v.failure_class) for v in verdicts] == [("insert_scripted", "INSERT_STUCK")]
    assert _end(store).reason == "insert_scripted failed: INSERT_STUCK"


def test_every_port_types_profile_is_in_the_config_whichever_the_task_has() -> None:
    # Around the SFP seat (-0.97 to -0.98 mm, #28 and the reruns), under the -0.24 mm the
    # scorer called a partial insertion (#70); around the SC seat (+0.05 to +0.07 mm), far
    # under the 5.15 mm where an SC plug stopped in the bore and the rim at 13.5 mm. The whole
    # table, the same for every task: which profile a run uses follows from its port type.
    ports = {
        "sc": {"entrance_mm": 13.64, "seated_min_mm": -0.5, "seated_max_mm": 0.5},
        "sfp": {"entrance_mm": 45.8, "seated_min_mm": -1.5, "seated_max_mm": -0.6},
    }
    names = ("approach", "align", "insert_scripted", "verify", "retract")
    configs = [
        {name: reg.get(name).config() for name in names}
        for reg in (aic_run.aic_registry(_unread_geometry, p) for p in PORT_PROFILES.values())
    ]

    assert configs[0] == configs[1]
    for name in ("align", "insert_scripted", "verify"):
        assert configs[0][name]["ports"] == ports, name
    assert configs[0]["insert_scripted"]["still_mm"] == 0.1
    assert configs[0]["verify"]["seated_max_xy_mm"] == 2.0


def test_the_goals_of_an_engine_run_with_both_port_types_are_one_configuration(
    store: EventStore,
) -> None:
    # The sample config sends SFP goals and an SC goal into one store; the agent report pools a
    # store's runs only if every config part but the scenario is the same.
    run_goal(FakeNode(task=TASK), TASK, store, scenario_id="aic/goal1/task_1")
    run_goal(
        FakeNode(task=SC_TASK, seat_height_m=0.00007), SC_TASK, store, scenario_id="aic/goal3/t"
    )

    parts = [
        {k: v for k, v in e.payload.config_parts.items() if k != "scenario"}
        for run in store.runs()
        for e in store.events(run.run_id)
        if isinstance(e.payload, RunStarted)
    ]
    assert len(parts) == 2 and parts[0] == parts[1]


def _geometry_at(height_mm: float, xy_mm: float = 0.0) -> Geometry:
    px, py, pz = PORT.position
    plug = Pose(position=(px + xy_mm / 1000, py, pz + height_mm / 1000))
    return Geometry(port=PORT, plug=plug, gripper=Pose(position=(px, py, pz + 0.1)))


@pytest.mark.parametrize(
    ("port_type", "height_mm", "expected"),
    [
        pytest.param("sfp", -0.98, True, id="sfp-on-its-seat"),
        pytest.param("sfp", -0.24, False, id="sfp-short-of-its-seat"),  # #70
        pytest.param("sfp", -2.0, False, id="sfp-under-its-seat"),
        pytest.param("sc", 0.07, True, id="sc-on-its-seat"),
        pytest.param("sc", 5.15, False, id="sc-stopped-in-the-bore"),
        pytest.param("sc", 13.46, False, id="sc-on-the-entrance-rim"),
    ],
)
@pytest.mark.parametrize(
    ("xy_mm", "near_axis"),
    [pytest.param(0.0, True, id="on-axis"), pytest.param(3.0, False, id="3mm-off")],
)
def test_a_plug_is_seated_only_in_its_port_types_band_and_near_the_axis(
    port_type: str, height_mm: float, expected: bool, xy_mm: float, near_axis: bool
) -> None:
    on_seat, height = seated(_geometry_at(height_mm, xy_mm), PORT_PROFILES[port_type])

    assert on_seat is (expected and near_axis)
    assert height == pytest.approx(height_mm)


def test_an_sfp_plug_that_stops_short_of_its_seat_is_never_called_seated(
    store: EventStore,
) -> None:
    # #70: in the 4096-token rerun an SFP plug settled at -0.24 mm; one band for both port
    # types called it seated, verify passed, and the scorer called it a partial insertion (50).
    node = FakeNode(task=TASK, seat_height_m=-0.00024)

    assert run_goal(node, TASK, store) is False

    details = [
        e.payload.detail
        for e in _events(store)
        if isinstance(e.payload, SkillFinished) and e.payload.skill in ("insert_scripted", "verify")
    ]
    assert not [d for d in details if d.get("seated")]
    assert _end(store).outcome == "failed"


def test_a_port_type_with_no_measured_seat_is_refused_before_anything_runs(
    store: EventStore,
) -> None:
    # No seat or entrance was measured for it, so neither insertion nor verify could judge it.
    task = Task(id="trial_1_task_1", time_limit=180.0, port_type="usb")
    node = FakeNode(task=task)

    with pytest.raises(UnknownPortType, match=r"^port_type must be one of: sc, sfp$"):
        run_goal(node, task, store)

    assert node.sent == []
    assert store.runs() == []


@pytest.mark.parametrize(
    ("task", "seat_m", "height_m"),
    [
        pytest.param(TASK, SFP_SEAT_M, 0.002, id="sfp-sprung-back-2mm"),
        pytest.param(SC_TASK, 0.00007, 0.01346, id="sc-jammed-at-its-entrance"),  # #28's jam
    ],
)
def test_a_plug_that_is_not_seated_at_the_end_makes_verify_fail(
    store: EventStore, task: Task, seat_m: float, height_m: float
) -> None:
    # The connector springs back out 1 s into the settle. (A plug stopped short during the
    # descent trips the Monitor's insertion stall rule first.)
    node = FakeNode(
        task=task,
        seat_height_m=seat_m,
        plug_pops_out_after_s=5.0 + ALIGN_STEPS * 0.05 + 1.0,
        plug_pops_out_to_m=height_m,
    )

    assert run_goal(node, task, store) is False

    end = _end(store)
    assert end.outcome == "failed"
    assert end.reason.startswith("verify failed")
    assert not [e for e in _events(store) if isinstance(e.payload, MonitorVerdict)]


def test_a_plug_on_its_seat_height_but_off_the_port_axis_fails_verify(
    store: EventStore,
) -> None:
    # At the SFP seat's height, 3 mm to the side (in a neighbouring port, say): not seated.
    node = FakeNode(
        task=TASK,
        plug_pops_out_after_s=5.0 + ALIGN_STEPS * 0.05 + 1.0,
        plug_pops_out_to_m=SFP_SEAT_M,
        plug_pops_out_xy_m=(0.003, 0.0),
    )

    assert run_goal(node, TASK, store) is False

    _, verify = _skill(store, "verify")
    assert verify.payload.detail == {"height_mm": -0.98, "reason": "not seated"}


# --- timeouts --------------------------------------------------------------------------------


def _unread_geometry() -> Geometry:
    raise AssertionError("not read for a timeout")


@pytest.mark.parametrize(
    ("name", "full_speed_s", "half_speed_s"),
    [
        ("approach", 10.0, 20.0),
        ("align", 38.0, 76.0),
        ("insert_scripted", 5.0, 10.0),
        ("verify", 10.0, 10.0),
        ("retract", 1.0, 1.0),
    ],
)
def test_each_skills_timeout_is_twice_its_nominal_duration_at_the_speed_it_runs(
    name: str, full_speed_s: float, half_speed_s: float
) -> None:
    # At full speed approach takes 5 s, align 19 s and insertion 2.5 s, as CheatCode; a
    # recovery may run them at half speed (#59), which takes twice as long. Verify only waits,
    # and retract never runs slowed. A full-speed run does not wait out the slow timeout.
    skill = aic_run.aic_registry(_unread_geometry, SFP).get(name)

    assert skill.timeout_s == pytest.approx(full_speed_s)
    assert skill.timeout_for({}) == pytest.approx(full_speed_s)
    assert skill.timeout_for({"speed_scale": 0.5}) == pytest.approx(half_speed_s)
    assert skill.config()["timeout_s"] == skill.timeout_s
    assert skill.config()["timeout_scales_with_speed"] is (half_speed_s != full_speed_s)


def test_insertion_runs_under_the_monitors_insertion_rules() -> None:
    registry = aic_run.aic_registry(_unread_geometry, SFP)

    assert registry.get("insert_scripted").config()["monitor_profile"] == "insertion"
    assert registry.get("align").config()["monitor_profile"] == "motion"


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


def test_a_slowed_align_gets_twice_the_time_before_it_times_out(
    store: EventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The same plug that never comes within 1 mm, under an align a recovery slowed to half
    # speed: it times out at 76 s, not at the full-speed 38 s.
    def slow_align(world: AicWorld, env: ExecEnv, goal: object) -> py_trees.behaviour.Behaviour:
        registry = aic_run.aic_registry(world.geometry, SFP)
        children = [
            SkillBehaviour(registry.get("approach"), env),
            SkillBehaviour(registry.get("align"), env, params={"speed_scale": 0.5}),
        ]
        return py_trees.composites.Sequence(name="slow", memory=True, children=children)

    monkeypatch.setattr(aic_run, "_tree", slow_align)
    node = FakeNode(task=TASK, plug_xy_offset_m=(0.02, 0.0))

    assert run_goal(node, TASK, store) is False

    end = _end(store)
    assert (end.outcome, end.reason) == ("failed", "align failed: TIMEOUT")
    (verdict,) = [e.payload for e in _events(store) if isinstance(e.payload, MonitorVerdict)]
    assert verdict.evidence["timeout_s"] == pytest.approx(76.0)
    (align,) = [
        e
        for e in _events(store)
        if isinstance(e.payload, SkillStarted) and e.payload.skill == "align"
    ]
    assert end.duration - align.ts == pytest.approx(76.0, abs=0.1)


# --- the hold above the entrance -------------------------------------------------------------


@dataclass
class _OffAxisNearTheEntrance(FakeNode):
    """While the tip is more than `low_m` and at most `high_m` above the port frame, it hangs
    `off_m` further off the TCP axis in x: an XY error the descent meets just before the hold
    point, as the sim's plug swings 0-0.9 mm off the axis on its way down (the F1 rerun)."""

    off_m: float = 0.0005
    low_m: float = 0.040
    high_m: float = 0.050

    @property
    def plug(self) -> Pose:
        plug = super().plug
        x, y, z = plug.position
        if self.low_m < z - self.port.position[2] <= self.high_m + 1e-9:
            return Pose(position=(x + self.off_m, y, z), orientation=plug.orientation)
        return plug


def _align_detail(store: EventStore) -> list[dict[str, object]]:
    return [
        e.payload.detail
        for e in _events(store)
        if isinstance(e.payload, SkillFinished) and e.payload.skill == "align"
    ]


@pytest.mark.parametrize(
    ("task", "seat_m", "low_m", "high_m"),
    [
        # SFP: the hold point is 48.8 mm (3 mm over the 45.8 mm entrance); off the axis from
        # 50 mm, so three descent steps see the error before the first step at 48.5 mm.
        pytest.param(TASK, SFP_SEAT_M, 0.040, 0.050, id="sfp"),
        # SC, the port this is for: 16.64 mm (3 mm over 13.64 mm); off the axis from 17.5 mm,
        # two steps before the first at 16.5 mm.
        pytest.param(SC_TASK, 0.00007, 0.010, 0.0175, id="sc"),
    ],
)
def test_align_holds_above_the_entrance_until_the_plug_is_on_the_port_axis(
    store: EventStore, task: Task, seat_m: float, low_m: float, high_m: float
) -> None:
    # The tip hangs 40 mm under the TCP, so with CheatCode's quirk it is 80 mm under each
    # z_offset and steps down 0.5 mm at a time. Near the hold point it hangs 0.5 mm off the
    # axis; the integrator takes that to about 0.3 mm by the first step at or under the hold
    # point, so align holds there, correcting XY, until the plug has been within 0.15 mm for 3
    # samples, then goes on down. The hold adds its time, nothing else.
    node = _OffAxisNearTheEntrance(task=task, seat_height_m=seat_m, low_m=low_m, high_m=high_m)
    hold_at_mm = PORT_PROFILES[task.port_type].hold_at_mm

    assert run_goal(node, task, store) is True

    (detail,) = _align_detail(store)
    held_s = detail["entry_held_s"]
    assert isinstance(held_s, float) and 0.05 * ENTRY_SAMPLES <= held_s < ENTRY_HOLD_S
    assert detail["entry_converged"] is True
    assert isinstance(detail["entry_xy_mm"], float) and detail["entry_xy_mm"] < ENTRY_XY_MM
    height = detail["entry_height_mm"]
    assert isinstance(height, float) and hold_at_mm - 0.5 < height <= hold_at_mm
    # Every target while it holds has the same height: the z_offset reached, not the next one.
    assert _longest_run_of_one_height(node) == round(held_s / 0.05) + 1
    expected = 5.0 + ALIGN_STEPS * 0.05 + held_s + 5.0 + 4 * 0.05  # a recording tick per skill
    assert _end(store).duration == pytest.approx(expected, abs=0.01)


def _longest_run_of_one_height(node: FakeNode) -> int:
    """The most descent targets in a row with one height: the step that reached the hold point,
    then each of the hold's. Only the targets sent with the tip under 100 mm: the approach's
    last ones settle on one height too."""
    sent = zip(node.tcp_at_send, node.sent, strict=True)
    z = [
        m.position_m[2]
        for at, m in sent
        if m.mode == "position" and at.position[2] - PLUG_BELOW_TCP_M - PORT.position[2] < 0.1
    ]
    longest = run = 1
    for a, b in itertools.pairwise(z):
        run = run + 1 if abs(b - a) < 1e-12 else 1
        longest = max(longest, run)
    return longest


@dataclass
class _DipsOnceWhileHeld(_OffAxisNearTheEntrance):
    """As `_OffAxisNearTheEntrance`, but on the 5th step the arm holds still in that band the
    tip reads on the port axis, for that step only: one sample under the threshold."""

    still_steps: int = 0

    def send(self, motion: MotionCommand) -> None:
        before = self.tcp.position
        super().send(motion)
        still = motion.mode == "position" and self.tcp.position == before
        self.still_steps = self.still_steps + 1 if still else 0

    @property
    def plug(self) -> Pose:
        plug = super().plug
        _, _, z = plug.position
        in_band = self.low_m < z - self.port.position[2] <= self.high_m + 1e-9
        if in_band and self.still_steps == 5:
            px, py, _ = self.port.position
            return Pose(position=(px, py, z), orientation=plug.orientation)
        return plug


def test_one_sample_on_the_axis_does_not_end_the_hold(store: EventStore) -> None:
    # 20 mm off, as below, except for one sample on the axis while align holds: the hold needs
    # 3 in a row, so it goes on to its limit.
    node = _DipsOnceWhileHeld(task=TASK, off_m=0.02)

    assert run_goal(node, TASK, store) is True

    (detail,) = _align_detail(store)
    assert detail["entry_converged"] is False
    assert detail["entry_held_s"] == pytest.approx(ENTRY_HOLD_S)
    readings = [
        e.payload.xy_error_mm
        for e in _events(store)
        if isinstance(e.payload, SignalSampled) and 40.0 < e.payload.height_mm <= 50.0
    ]
    assert min(readings) < ENTRY_XY_MM  # the dip was seen


def test_the_hold_above_the_entrance_ends_after_its_limit_and_the_descent_goes_on(
    store: EventStore,
) -> None:
    # 20 mm off the axis: the integrator's correction is clipped at 7.5 mm, so the plug never
    # comes within 0.15 mm. Align holds for 3 s, then goes on down; under 40 mm the plug hangs
    # straight again, the integrator brings it back, and the task succeeds. The hold judges
    # nothing: what the plug meets at the entrance is the Monitor's.
    node = _OffAxisNearTheEntrance(task=TASK, off_m=0.02)

    assert run_goal(node, TASK, store) is True

    (detail,) = _align_detail(store)
    assert detail["entry_held_s"] == pytest.approx(ENTRY_HOLD_S)
    assert detail["entry_converged"] is False
    assert isinstance(detail["entry_xy_mm"], float) and detail["entry_xy_mm"] >= ENTRY_XY_MM


@dataclass
class _TrailsItsTargets(_OffAxisNearTheEntrance):
    """As `_OffAxisNearTheEntrance`, on an arm whose height has momentum: each position target
    moves it by `gain` of its distance to the target's height plus `carry` of its last move.
    In a steady descent of 0.5 mm a step it trails its target by 0.5 x (1 - carry) / gain mm,
    6.25 mm by default, as the sim's arm did (6-7 mm, #73's first clean run), and once the
    targets stop it goes on down about 0.5 mm before it comes back. XY follows each target
    exactly."""

    gain: float = 0.04
    carry: float = 0.5
    _moved_m: float = 0.0

    def send(self, motion: MotionCommand) -> None:
        before = self.tcp.position
        super().send(motion)
        if motion.mode == "position":
            x, y, z = self.tcp.position
            self._moved_m = self.gain * (z - before[2]) + self.carry * self._moved_m
            height = before[2] + self._moved_m
            self.tcp = Pose(position=(x, y, height), orientation=motion.orientation)


def test_the_hold_stops_an_arm_that_trails_its_targets_above_the_entrance(
    store: EventStore,
) -> None:
    # The SC plug meets the hold point (16.64 mm) with its arm 6.25 mm above the target last
    # sent. Holding that target would let the arm slide on into the port while XY converges;
    # the hold commands where the arm is when it stops, once, so the plug stays above the
    # entrance (13.64 mm) after the arm's overshoot, and the descent then goes on from there,
    # 0.5 mm a step. A hold that measured the arm again at every sample would follow the
    # overshoot down and send no target twice.
    node = _TrailsItsTargets(task=SC_TASK, seat_height_m=0.00007, low_m=0.010, high_m=0.0175)
    sc = PORT_PROFILES["sc"]

    assert run_goal(node, SC_TASK, store) is True

    (detail,) = _align_detail(store)
    start, end = detail["entry_start_mm"], detail["entry_height_mm"]
    assert isinstance(start, float) and isinstance(end, float)
    assert sc.hold_at_mm - 0.6 < start <= sc.hold_at_mm
    assert detail["entry_held_s"] >= 0.05 * ENTRY_SAMPLES  # it held while XY converged
    assert start - 1.0 < end <= start and end > sc.entrance_mm
    sent = zip(node.tcp_at_send, node.sent, strict=True)
    z = [
        m.position_m[2]
        for at, m in sent
        if m.mode == "position" and at.position[2] - PLUG_BELOW_TCP_M - PORT.position[2] < 0.1
    ]
    # The first repeated target is the hold's (align's wait at 10 mm and insertion's settle
    # come later); the target after the hold's last is one 0.5 mm step under it.
    held = next(k for k, (a, b) in enumerate(itertools.pairwise(z)) if abs(b - a) < 1e-12)
    last = next(k for k in range(held, len(z) - 1) if abs(z[k + 1] - z[k]) >= 1e-12)
    assert last - held + 1 >= ENTRY_SAMPLES - 1
    assert z[last + 1] - z[last] == pytest.approx(-DESCENT_STEP_M, abs=1e-9)


def test_the_holds_limit_is_on_the_runs_clock_not_a_count_of_steps(store: EventStore) -> None:
    # The same plug that never comes within 0.15 mm, in a sim whose steps come back after
    # 0.08 s of sim time (two 0.04 s polls for each 0.05 s period). The hold ends on the run's
    # clock, after the 38 steps that first reach 3 s; 3 s / 0.05 s = 60 steps would have held
    # it for 4.8 s.
    node = _OffAxisNearTheEntrance(task=TASK, off_m=0.02, sim_per_sleep_s=0.04)

    assert run_goal(node, TASK, store) is True

    (detail,) = _align_detail(store)
    assert detail["entry_converged"] is False
    assert detail["entry_held_s"] == pytest.approx(38 * 0.08)
    assert _longest_run_of_one_height(node) == 38 + 1


def test_an_align_that_starts_under_the_hold_point_does_not_hold(
    store: EventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The first align crosses the SFP hold point (48.8 mm) on its way down, so it stops there,
    # though the plug is on the axis: two more samples confirm it (0.1 s). After a retract the
    # tip is at 10.8 mm, already under the hold point (in the port's guide), so the second
    # align does not stop there.
    def align_retract_align(
        world: AicWorld, env: ExecEnv, goal: object
    ) -> py_trees.behaviour.Behaviour:
        registry = aic_run.aic_registry(world.geometry, SFP)
        names = ["approach", "align", "retract", "align"]
        children = [SkillBehaviour(registry.get(name), env) for name in names]
        return py_trees.composites.Sequence(name="align", memory=True, children=children)

    monkeypatch.setattr(aic_run, "_tree", align_retract_align)
    node = FakeNode(task=TASK, plug_below_tcp_m=0.0021)

    assert run_goal(node, TASK, store) is True

    first, second = _align_detail(store)
    assert first["entry_held_s"] == pytest.approx(0.05 * (ENTRY_SAMPLES - 1))
    assert first["entry_converged"] is True
    assert not [key for key in second if key.startswith("entry_")]


def test_the_hold_is_in_aligns_config() -> None:
    config = aic_run.aic_registry(_unread_geometry, SFP).get("align").config()

    assert config["entry"] == {
        "above_entrance_mm": ENTRY_HOLD_ABOVE_MM,
        "xy_mm": ENTRY_XY_MM,
        "samples": ENTRY_SAMPLES,
        "max_hold_s": ENTRY_HOLD_S,
    }
    for profile in PORT_PROFILES.values():
        assert profile.hold_at_mm == pytest.approx(profile.entrance_mm + ENTRY_HOLD_ABOVE_MM)


# --- retract ---------------------------------------------------------------------------------


def test_retract_raises_the_tcp_5_mm_straight_up_from_where_it_is(
    store: EventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def retract_only(world: AicWorld, env: ExecEnv, goal: object) -> py_trees.behaviour.Behaviour:
        retract = SkillBehaviour(AicRetract(world.geometry), env)
        return py_trees.composites.Sequence(name="retract", memory=True, children=[retract])

    monkeypatch.setattr(aic_run, "_tree", retract_only)
    node = FakeNode(task=TASK)
    x, y, z = node.tcp.position

    assert run_goal(node, TASK, store) is True

    assert len(node.sent) == 10 + 1  # 10 steps of 0.5 mm, then HOLD where it ended
    assert node.tcp.position == pytest.approx((x, y, z + 0.005))


def test_a_retract_after_a_finished_align_makes_the_next_align_step_down_from_the_arm(
    store: EventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A safety verdict can land on the tick a descent run finishes (FORCE_LIMIT: a retract,
    # then the run again, slowed). That run ended by itself, so only the retract tells the next
    # one to start from where the arm is. The skills come from the cell's own registry, so the
    # retract shares the descent as the policy wires it. The plug hangs free, 2.1 mm under the
    # TCP and 5.8 mm above the port when align ends: 5 mm up, the next align steps back down
    # 0.5 mm at a time, instead of dropping 5 mm at once to align's last target.
    def align_retract_align(
        world: AicWorld, env: ExecEnv, goal: object
    ) -> py_trees.behaviour.Behaviour:
        registry = aic_run.aic_registry(world.geometry, SFP)
        names = ["approach", "align", "retract", "align"]
        children = [SkillBehaviour(registry.get(name), env) for name in names]
        return py_trees.composites.Sequence(name="align", memory=True, children=children)

    monkeypatch.setattr(aic_run, "_tree", align_retract_align)
    node = FakeNode(task=TASK, plug_below_tcp_m=0.0021)

    assert run_goal(node, TASK, store) is True

    # (TCP z, target z) per motion. The second align's targets follow the retract's last step
    # up; HOLDs, a target at the arm, are left out.
    sent = zip(node.tcp_at_send, node.sent, strict=True)
    moves = [(at.position[2], m.position_m[2]) for at, m in sent]
    up = max(i for i, (at, to) in enumerate(moves) if to > at + 1e-12)
    second = [(at, to) for at, to in moves[up + 1 :] if abs(to - at) > 1e-9]
    top = moves[up][1]  # where the retract ended, 5 mm above where align did
    assert len(second) == 10
    assert all(at - to == pytest.approx(DESCENT_STEP_M) for at, to in second)
    assert second[-1][1] == pytest.approx(top - RETRACT_M)


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
    def approach_then_retract(
        world: AicWorld, env: ExecEnv, goal: object
    ) -> py_trees.behaviour.Behaviour:
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


def test_every_aic_skill_tells_the_planner_what_it_does() -> None:
    # The model planner chooses by these descriptions; in the sim it was sent empty ones.
    def no_geometry() -> Geometry:
        raise AssertionError("not read when the registry is built")

    registry = aic_run.aic_registry(no_geometry, SFP)

    for name in registry.names():
        description = getattr(registry.get(name), "description", "")
        assert isinstance(description, str), name
        assert description.strip(), name
