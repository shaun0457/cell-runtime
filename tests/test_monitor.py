"""The Monitor classifies injected faults from signals alone and preempts the running skill."""

from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import Path

import pytest

from cellrt.eval.runner import run_unit
from cellrt.eval.scenarios import Scenario, load_scenarios
from cellrt.executor.samples import sampled, signals_of
from cellrt.monitor.classifier import Monitor, MonitorConfig
from cellrt.monitor.profiles import MonitorProfile
from cellrt.runtime.events import SCHEMA_VERSION, Event, RunStarted
from cellrt.runtime.state import RunState, apply, state_at
from cellrt.runtime.store import EventStore
from cellrt.runtime.task import Task
from cellrt.skills.mock import (
    Align,
    Approach,
    InsertScripted,
    Retract,
    Verify,
    WaitSensor,
    mock_registry,
)
from cellrt.skills.registry import SkillRegistry
from cellrt.world.base import Signals
from cellrt.world.mock_world import MockParams

PHASE0 = Path(__file__).resolve().parents[1] / "eval" / "scenarios" / "phase0.yaml"
SCENARIOS = {s.id: s for s in load_scenarios(PHASE0)}
SEEDS = range(8)


class _Fed:
    """A Monitor fed as the runtime feeds it: each sample is logged, and so folded, before the
    Monitor sees it; the Monitor reads the time over the force limit from the folded state."""

    def __init__(self, period_s: float = 0.05, config: MonitorConfig | None = None) -> None:
        self._period_s = period_s
        self._seq = 1
        started = RunStarted(
            scenario_id="s",
            setting="A",
            seed=0,
            attempt=1,
            task=Task(),
            config_hash="h",
            schema_version=SCHEMA_VERSION,
        )
        self.state = apply(
            RunState(), Event(run_id="r", seq=1, ts=0.0, wall_ts=0.0, payload=started)
        )
        self.monitor = Monitor(period_s, config, force_time=lambda: self.state.over_limit_s)

    def log(self, s: Signals) -> None:
        """A sample reaches the log only (e.g. while the planner runs and no skill does)."""
        self._seq += 1
        ts = round((self._seq - 1) * self._period_s, 9)
        event = Event(run_id="r", seq=self._seq, ts=ts, wall_ts=0.0, payload=sampled(self._seq, s))
        self.state = apply(self.state, event)

    def observe(self, s: Signals):
        self.log(s)
        return self.monitor.observe(s)

    def reset(self, profile: MonitorProfile) -> None:
        self.monitor.reset(profile)

    def buffered(self) -> int:
        return self.monitor.buffered()


def _run(tmp_path: Path, scenario: Scenario, seed: int) -> list:
    with EventStore(tmp_path / "events.sqlite") as store:
        return store.events(run_unit(scenario, "A", seed=seed, store=store).run_id)


@pytest.mark.parametrize("seed", SEEDS)
def test_a_sensor_dropout_is_classified_and_preempts_the_skill(tmp_path: Path, seed: int) -> None:
    events = _run(tmp_path, SCENARIOS["sensor_dropout"], seed)

    verdicts = [e.payload for e in events if e.type == "monitor_verdict"]
    cancelled = [e.payload for e in events if e.type == "skill_cancelled"]
    assert [(v.failure_class, v.rule) for v in verdicts] == [("SENSOR_DROPOUT", "wrench_invalid")]
    assert [(c.skill, c.reason) for c in cancelled] == [(verdicts[0].skill, "preempted")]
    assert verdicts[0].evidence["invalid_s"] == 0.2
    assert events[-1].payload.outcome == "failed"
    assert events[-1].payload.reason == f"{verdicts[0].skill} failed: SENSOR_DROPOUT"


# (scenario, class, rule, check on the evidence against the thresholds in the spec)
INSERT_FAULTS = [
    (
        "obstacle",
        "INSERT_STUCK",
        "stuck_under_force",
        lambda ev: ev["force_n"] > 10 and ev["progress_mm"] < 0.2 and ev["xy_error_mm"] <= 1.0,
    ),
    (
        "tight_fit",
        "FORCE_LIMIT",
        "force_over_limit",
        lambda ev: ev["force_n"] > 20 and ev["over_s"] == 0.5 and ev["budget_s"] == 1.0,
    ),
    (
        "pose_shift",
        "POSE_ERROR",
        "xy_off_during_insert",
        lambda ev: ev["xy_error_mm"] > 1.0 and ev["held_s"] == 0.2,
    ),
    (
        "yaw_mismatch",
        "UNKNOWN",
        "insert_stalled",
        lambda ev: ev["progress_mm"] < 0.2 and ev["window_s"] == 1.0 and ev["torque_nm"] > 0.3,
    ),
]


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize(("scenario_id", "failure_class", "rule", "evidence_ok"), INSERT_FAULTS)
def test_an_insertion_fault_is_classified_and_preempts_the_insert(
    tmp_path: Path,
    scenario_id: str,
    failure_class: str,
    rule: str,
    evidence_ok: Callable[[dict], bool],
    seed: int,
) -> None:
    events = _run(tmp_path, SCENARIOS[scenario_id], seed)

    verdicts = [e for e in events if e.type == "monitor_verdict"]
    cancelled = [e.payload for e in events if e.type == "skill_cancelled"]
    assert [(v.payload.skill, v.payload.failure_class, v.payload.rule) for v in verdicts] == [
        ("insert_scripted", failure_class, rule)
    ]
    assert evidence_ok(verdicts[0].payload.evidence), verdicts[0].payload.evidence
    injected = next(e for e in events if e.type == "fault_injected")
    assert injected.ts <= verdicts[0].ts  # never before the fault takes effect
    assert [(c.skill, c.reason) for c in cancelled] == [("insert_scripted", "preempted")]
    assert events[-1].payload.reason == f"insert_scripted failed: {failure_class}"


@pytest.mark.parametrize("seed", range(20))
def test_the_baseline_raises_no_verdict(tmp_path: Path, seed: int) -> None:
    events = _run(tmp_path, SCENARIOS["baseline"], seed)

    assert [e.payload for e in events if e.type == "monitor_verdict"] == []
    assert events[-1].payload.outcome == "success"


def test_the_force_is_tared_so_a_sensor_bias_over_the_limit_raises_no_verdict(
    tmp_path: Path,
) -> None:
    biased = MockParams(tare_offset_n=30.0)  # like AIC: raw F/T reading, offset published apart
    with EventStore(tmp_path / "events.sqlite") as store:
        events = store.events(
            run_unit(SCENARIOS["baseline"], "A", seed=0, store=store, params=biased).run_id
        )

    samples = [e.payload for e in events if e.type == "signal_sampled"]
    assert [e.payload for e in events if e.type == "monitor_verdict"] == []
    assert events[-1].payload.outcome == "success"
    assert all(s.tare_offset_n == (0.0, 0.0, 30.0) for s in samples)
    assert min(math.hypot(*s.raw_force_n) for s in samples) > 25.0  # every raw reading > 20 N
    assert max(s.force_n for s in samples) < 20.0


def test_force_is_tared_per_axis_before_taking_the_magnitude() -> None:
    # 6 N of weight along z, 25 N of side contact along x: |raw| - |offset| would read 19.7 N.
    side_push = Signals(
        height_mm=-2.0,
        xy_offset_mm=(0.0, 0.0),
        raw_force_n=(25.0, 0.0, 6.0),
        tare_offset_n=(0.0, 0.0, 6.0),
        torque_nm=0.0,
        wrench_valid=True,
    )

    assert side_push.force_n == 25.0


def _sample(
    height_mm: float = -2.0, force_n: float = 5.0, xy_mm: float = 0.0, valid: bool = True
) -> Signals:
    return Signals(
        height_mm=height_mm,
        xy_offset_mm=(xy_mm, 0.0),
        raw_force_n=(0.0, 0.0, force_n),
        tare_offset_n=(0.0, 0.0, 0.0),
        torque_nm=0.0,
        wrench_valid=valid,
    )


# Settings that switch one rule off, to show the runner-up matches on the same sample.
DISABLE = {
    "SENSOR_DROPOUT": MonitorConfig(dropout_s=1e6),
    "FORCE_LIMIT": MonitorConfig(force_budget_s=1e9, force_trigger_s=1e9),
    "INSERT_STUCK": MonitorConfig(stuck_force_n=1e9),
    "POSE_ERROR": MonitorConfig(pose_tolerance_mm=1e9),
}

# Each sequence makes two rules match for the first time on its last sample: (winner, runner-up).
PRIORITY_CASES = {
    "dropout over pose error": (
        [_sample(xy_mm=2.0, valid=False) for _ in range(5)],
        ("SENSOR_DROPOUT", "POSE_ERROR"),
    ),
    "force limit over stuck": (
        [_sample(height_mm=-min(i, 12), force_n=25.0 if i > 12 else 5.0) for i in range(1, 23)],
        ("FORCE_LIMIT", "INSERT_STUCK"),
    ),
    "force limit over pose error": (
        [
            _sample(height_mm=-i, force_n=25.0 if i > 12 else 5.0, xy_mm=2.0 if i > 17 else 0.0)
            for i in range(1, 23)
        ],
        ("FORCE_LIMIT", "POSE_ERROR"),
    ),
    "stuck over stalled": (
        [_sample(force_n=11.0 if i == 21 else 5.0) for i in range(1, 22)],
        ("INSERT_STUCK", "UNKNOWN"),
    ),
    "pose error over stalled": (
        [_sample(xy_mm=2.0 if i > 16 else 0.0) for i in range(1, 22)],
        ("POSE_ERROR", "UNKNOWN"),
    ),
}


def _classes(samples: list[Signals], config: MonitorConfig | None = None) -> list[str | None]:
    monitor = _Fed(0.05, config)
    monitor.reset("insertion")
    return [v.failure_class if (v := monitor.observe(s)) else None for s in samples]


@pytest.mark.parametrize("case", PRIORITY_CASES)
def test_when_several_rules_match_the_highest_priority_class_wins(case: str) -> None:
    samples, (winner, runner_up) = PRIORITY_CASES[case]
    quiet = [None] * (len(samples) - 1)

    assert _classes(samples) == [*quiet, winner]
    assert _classes(samples, DISABLE[winner]) == [*quiet, runner_up]


@pytest.mark.parametrize(
    ("config", "longest_samples"),
    [
        # Sensor window: invalid for 0.2 s, 5 samples at 20 Hz (force is a running sum).
        # Insertion window: stalled for 1.0 s, 21 samples (61 for 3.0 s).
        (MonitorConfig(), 5 + 21),
        (MonitorConfig(stall_window_s=3.0), 5 + 61),
    ],
)
def test_the_signal_window_is_bounded_by_the_longest_rule(
    config: MonitorConfig, longest_samples: int
) -> None:
    monitor = _Fed(0.05, config)
    monitor.reset("insertion")

    for i in range(1000):
        monitor.observe(_sample(height_mm=-0.01 * i))

    assert monitor.buffered() == longest_samples


def test_rule_durations_are_never_shorter_than_specified_when_the_period_does_not_divide_them() -> (
    None
):
    monitor = _Fed(0.09)  # 0.2 s is not a whole number of 0.09 s steps
    monitor.reset("motion")

    verdicts = [monitor.observe(_sample(valid=False)) for _ in range(4)]

    assert verdicts[:3] == [None, None, None]  # 3 samples span only 0.18 s
    assert verdicts[3] is not None
    assert verdicts[3].evidence["invalid_s"] == 0.27


def _observe_across(
    profiles_and_samples: list[tuple[MonitorProfile, list[Signals]]],
) -> list[str | None]:
    """Classes per sample while the running skill changes (each change resets the Monitor);
    each skill is given by the Monitor profile it declares."""
    monitor = _Fed(0.05)
    classes: list[str | None] = []
    for profile, samples in profiles_and_samples:
        monitor.reset(profile)
        classes += [v.failure_class if (v := monitor.observe(s)) else None for s in samples]
    return classes


def test_a_sensor_dropout_is_timed_across_a_skill_boundary() -> None:
    invalid = _sample(valid=False)

    classes = _observe_across([("motion", [invalid] * 3), ("motion", [invalid] * 2)])

    assert classes == [None, None, None, None, "SENSOR_DROPOUT"]  # 5 samples span 0.2 s


PUSHING, LIGHT = _sample(force_n=25.0), _sample(force_n=5.0)


def _verdicts_across(profiles_and_samples: list[tuple[MonitorProfile, list[Signals]]]) -> list:
    monitor = _Fed(0.05)
    verdicts = []
    for profile, samples in profiles_and_samples:
        monitor.reset(profile)
        verdicts += [monitor.observe(s) for s in samples]
    return verdicts


def test_time_over_the_force_limit_is_summed_over_the_task_like_the_aic_penalty() -> None:
    # 0.3 s over, 0.5 s under, 0.2 s over in the next skill: never 0.5 s in a row.
    verdicts = _verdicts_across(
        [("motion", [PUSHING] * 6 + [LIGHT] * 10), ("motion", [PUSHING] * 4)]
    )

    assert [v and v.failure_class for v in verdicts] == [None] * 19 + ["FORCE_LIMIT"]
    assert verdicts[-1].evidence["over_s"] == 0.5  # the trigger: half the 1 s AIC budget


def test_after_the_trigger_any_further_force_over_the_limit_is_stopped_at_once() -> None:
    verdicts = _verdicts_across([("motion", [PUSHING] * 10), ("motion", [LIGHT] * 3 + [PUSHING])])

    assert [v and v.failure_class for v in verdicts] == [None] * 9 + ["FORCE_LIMIT"] + [
        None
    ] * 3 + ["FORCE_LIMIT"]
    assert verdicts[-1].evidence["over_s"] == 0.55


def test_an_unloading_skill_is_not_preempted_for_force_but_its_time_still_counts() -> None:
    verdicts = _verdicts_across(
        [("motion", [PUSHING] * 10), ("unload", [PUSHING] * 5), ("motion", [PUSHING])]
    )

    assert [v and v.failure_class for v in verdicts][10:] == [None] * 5 + ["FORCE_LIMIT"]
    assert verdicts[-1].evidence["over_s"] == 0.8


def test_the_monitor_judges_a_sample_exactly_as_it_was_logged() -> None:
    s = Signals(
        height_mm=-1.25,
        xy_offset_mm=(0.3, -0.4),
        raw_force_n=(1.0, -2.0, 21.5),
        tare_offset_n=(0.5, 0.5, 0.5),
        torque_nm=0.25,
        wrench_valid=True,
    )

    assert signals_of(sampled(7, s)) == s


def test_the_logged_xy_error_is_the_value_the_monitor_compared() -> None:
    # Just over the 1 mm tolerance before rounding; the log keeps 6 decimals.
    edge = _sample(xy_mm=1.0000001)
    fed = _Fed()
    fed.reset("insertion")

    verdicts = [fed.observe(edge) for _ in range(10)]  # pose error needs 0.2 s = 5 samples
    logged = fed.state.last_signal

    assert logged is not None
    assert (logged.xy_offset_mm, logged.xy_error_mm) == ((1.0000001, 0.0), 1.0)
    # 1.0 mm is within tolerance: the Monitor agrees with what a reader of the log sees.
    assert verdicts == [None] * 10


def test_time_over_the_limit_while_no_skill_runs_still_counts_toward_the_trigger() -> None:
    # While the planner runs, samples reach the log but not the Monitor; the AIC counts them.
    fed = _Fed()
    fed.reset("motion")

    seen = [fed.observe(PUSHING) for _ in range(8)]  # 0.40 s
    fed.log(PUSHING)
    fed.log(PUSHING)  # 0.50 s, no skill watching
    verdict = fed.observe(PUSHING)

    assert seen == [None] * 8
    assert verdict is not None and verdict.failure_class == "FORCE_LIMIT"
    assert verdict.evidence["over_s"] == 0.55


def test_the_monitor_judges_force_by_the_time_in_the_folded_run_state(tmp_path: Path) -> None:
    events = _run(tmp_path, SCENARIOS["tight_fit"], seed=0)
    [verdict] = [e for e in events if e.type == "monitor_verdict"]

    assert verdict.payload.failure_class == "FORCE_LIMIT"
    # Rebuilt from the log at the verdict: the same time the Monitor acted on.
    assert state_at(events, verdict.seq).over_limit_s == verdict.payload.evidence["over_s"]


def test_after_a_dropout_verdict_the_next_skill_gets_the_full_duration_again() -> None:
    invalid = _sample(valid=False)

    classes = _observe_across([("motion", [invalid] * 5), ("motion", [invalid] * 5)])

    assert classes == [None] * 4 + ["SENSOR_DROPOUT"] + [None] * 4 + ["SENSOR_DROPOUT"]


def test_insertion_rules_never_see_samples_from_before_the_skill_started() -> None:
    still = _sample(height_mm=5.0)

    classes = _observe_across([("motion", [still] * 30), ("insertion", [still])])

    assert classes == [None] * 31


def test_a_skill_waiting_for_the_sensor_is_not_preempted_for_a_dropout() -> None:
    invalid = _sample(valid=False)

    classes = _observe_across([("sensor_wait", [invalid] * 10), ("motion", [invalid] * 5)])

    # The wait itself is never preempted; the dropout window still runs across skills, so the
    # next skill is preempted on its first sample if the sensor is still out.
    assert classes == [None] * 10 + ["SENSOR_DROPOUT"] + [None] * 4


def test_only_insertion_skills_get_the_insertion_rules() -> None:
    stalled = [_sample(xy_mm=2.0)] * 21  # off in XY and not descending for 1 s

    assert _observe_across([("motion", stalled)]) == [None] * 21
    assert _observe_across([("insertion", stalled)])[4] == "POSE_ERROR"


def test_the_profile_a_skill_declares_is_used_by_the_run_and_is_part_of_the_config_hash(
    tmp_path: Path,
) -> None:
    # The same insert skill, declared as plain motion: the Monitor no longer applies insertion
    # rules to it.
    as_motion = SkillRegistry(
        [Approach(), Align(), InsertScripted(monitor_profile="motion"), Verify(), Retract()]
    )
    with EventStore(tmp_path / "events.sqlite") as store:
        default = store.events(run_unit(SCENARIOS["yaw_mismatch"], "A", seed=0, store=store).run_id)
        custom_run = run_unit(
            SCENARIOS["yaw_mismatch"], "A", seed=0, store=store, registry=as_motion
        )
        custom = store.events(custom_run.run_id)

    def classes(events: list) -> list[str]:
        return [e.payload.failure_class for e in events if e.type == "monitor_verdict"]

    assert classes(default) == ["UNKNOWN"]
    assert classes(custom) == ["TIMEOUT"]  # without insertion rules the stall runs into the timeout
    assert default[0].payload.config_hash != custom[0].payload.config_hash


RULES = {
    "SENSOR_DROPOUT": "wrench_invalid",
    "FORCE_LIMIT": "force_over_limit",
    "INSERT_STUCK": "stuck_under_force",
    "POSE_ERROR": "xy_off_during_insert",
    "UNKNOWN": "insert_stalled",
}


@pytest.mark.parametrize("case", PRIORITY_CASES)
def test_the_evidence_names_the_other_rules_that_matched_on_the_same_sample(case: str) -> None:
    samples, (winner, runner_up) = PRIORITY_CASES[case]
    monitor = _Fed(0.05)
    monitor.reset("insertion")

    verdict = [monitor.observe(s) for s in samples][-1]

    assert verdict is not None and verdict.failure_class == winner
    assert verdict.evidence["also_matched"] == [RULES[runner_up]]


def test_a_verdict_with_no_other_match_has_no_also_matched_entry() -> None:
    monitor = _Fed(0.05)
    monitor.reset("motion")

    verdict = [monitor.observe(_sample(valid=False)) for _ in range(5)][-1]

    assert verdict is not None and "also_matched" not in verdict.evidence


@pytest.mark.parametrize("trigger_s", [0.0, 1.5])
def test_the_force_trigger_must_fall_inside_the_aic_budget(trigger_s: float) -> None:
    with pytest.raises(ValueError, match="force_trigger_s"):
        _Fed(0.05, MonitorConfig(force_trigger_s=trigger_s))


def test_a_misspelt_monitor_profile_is_refused_when_the_skill_is_registered() -> None:
    with pytest.raises(ValueError, match="monitor_profile"):
        SkillRegistry([InsertScripted(monitor_profile="insert")])  # type: ignore[arg-type]


def test_the_registry_names_its_insertion_skills_by_profile_in_name_order() -> None:
    # Registered as insert_scripted, then insert_act; the profile decides, not the name.
    assert mock_registry().insertion_skills() == ("insert_act", "insert_scripted")
    as_motion = SkillRegistry([Approach(), Align(), InsertScripted(monitor_profile="motion")])
    assert as_motion.insertion_skills() == ()


def test_skill_parameters_are_part_of_the_config_hash(tmp_path: Path) -> None:
    slow = SkillRegistry(
        [Approach(), Align(), InsertScripted(speed_mm_s=1.0), Verify(), Retract(), WaitSensor()]
    )  # the default registry but for one skill parameter
    with EventStore(tmp_path / "events.sqlite") as store:
        default = store.events(run_unit(SCENARIOS["baseline"], "A", seed=0, store=store).run_id)
        custom = store.events(
            run_unit(SCENARIOS["baseline"], "A", seed=0, store=store, registry=slow).run_id
        )

    assert default[0].payload.config_hash != custom[0].payload.config_hash
