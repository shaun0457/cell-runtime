"""The Monitor classifies injected faults from signals alone and preempts the running skill."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from cellrt.eval.runner import run_unit
from cellrt.eval.scenarios import Scenario, load_scenarios
from cellrt.monitor.classifier import Monitor, MonitorConfig
from cellrt.runtime.store import EventStore
from cellrt.world.base import Signals

PHASE0 = Path(__file__).resolve().parents[1] / "eval" / "scenarios" / "phase0.yaml"
SCENARIOS = {s.id: s for s in load_scenarios(PHASE0)}
SEEDS = range(8)


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
        lambda ev: ev["min_force_n"] > 20 and ev["over_s"] > 1.0,
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


def _sample(
    height_mm: float = -2.0, force_n: float = 5.0, xy_mm: float = 0.0, valid: bool = True
) -> Signals:
    return Signals(
        height_mm=height_mm,
        xy_offset_mm=(xy_mm, 0.0),
        force_n=force_n,
        torque_nm=0.0,
        wrench_valid=valid,
    )


# Settings that switch one rule off, to show the runner-up matches on the same sample.
DISABLE = {
    "SENSOR_DROPOUT": MonitorConfig(dropout_s=1e6),
    "FORCE_LIMIT": MonitorConfig(force_limit_n=1e9),
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
        [_sample(height_mm=-min(i, 12), force_n=25.0) for i in range(1, 23)],
        ("FORCE_LIMIT", "INSERT_STUCK"),
    ),
    "force limit over pose error": (
        [_sample(height_mm=-i, force_n=25.0, xy_mm=2.0 if i > 17 else 0.0) for i in range(1, 23)],
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
    monitor = Monitor(period_s=0.05, config=config)
    monitor.reset("insert_scripted")
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
        (MonitorConfig(), 22),  # force over 20 N for more than 1.0 s: 1.05 s of samples at 20 Hz
        (MonitorConfig(stall_window_s=3.0), 61),
    ],
)
def test_the_signal_window_is_bounded_by_the_longest_rule(
    config: MonitorConfig, longest_samples: int
) -> None:
    monitor = Monitor(period_s=0.05, config=config)
    monitor.reset("insert_scripted")

    for i in range(1000):
        monitor.observe(_sample(height_mm=-0.01 * i))

    assert monitor.buffered() == longest_samples


def test_rule_durations_are_never_shorter_than_specified_when_the_period_does_not_divide_them() -> (
    None
):
    monitor = Monitor(period_s=0.09)  # 0.2 s is not a whole number of 0.09 s steps
    monitor.reset("approach")

    verdicts = [monitor.observe(_sample(valid=False)) for _ in range(4)]

    assert verdicts[:3] == [None, None, None]  # 3 samples span only 0.18 s
    assert verdicts[3] is not None
    assert verdicts[3].evidence["invalid_s"] == 0.27
