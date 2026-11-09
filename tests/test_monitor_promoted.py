"""Promoted rules in the Monitor (ADR 021, ticket T2): they fire after the known-class rules and
before `insert_stalled`, only in the insertion profile and only for the skill they name, and they
change the config hash only when there are any."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cellrt.eval.runner import config_parts, run_unit
from cellrt.eval.scenarios import Scenario, load_scenarios
from cellrt.executor.recovery import RecoveryConfig
from cellrt.monitor.classifier import FailureClass, Monitor, MonitorConfig
from cellrt.monitor.promoted import PromotedRule
from cellrt.runtime.events import Event, MonitorVerdict
from cellrt.runtime.store import EventStore
from cellrt.skills.mock import mock_registry
from cellrt.world.base import Signals
from cellrt.world.mock_world import MockParams

PHASE3 = Path(__file__).resolve().parents[1] / "eval" / "scenarios" / "phase3.yaml"
SCENARIOS = {s.id: s for s in load_scenarios(PHASE3)}
SEEDS = range(4)


def _rule(skill: str = "insert_scripted", **over: Any) -> PromotedRule:
    data: dict[str, Any] = {
        "name": "PLUG_ROTATED",
        "skill": skill,
        "window_s": 0.5,
        "conditions": [
            {"field": "torque_nm", "op": ">=", "value": 0.3},
            {"field": "progress_mm", "op": "<", "value": 0.2},
        ],
    }
    data.update(over)
    return PromotedRule.model_validate_json(json.dumps(data))


def _verdicts(events: list[Event]) -> list[tuple[float, MonitorVerdict]]:
    return [(e.ts, e.payload) for e in events if isinstance(e.payload, MonitorVerdict)]


def _run(scenario: Scenario, setting: str, seed: int, tmp_path: Path, monitor: MonitorConfig):
    with EventStore(tmp_path / f"{scenario.id}-{setting}-{seed}.sqlite") as store:
        result = run_unit(scenario, setting, seed, store, monitor=monitor)  # type: ignore[arg-type]
        return result, store.events(result.run_id)


@pytest.mark.parametrize("seed", SEEDS)
def test_a_promoted_rule_names_the_rotated_plug_before_the_stall_rule_would(
    tmp_path: Path, seed: int
) -> None:
    scenario = SCENARIOS["yaw_mismatch"]
    _, before = _run(scenario, "A", seed, tmp_path / "before", MonitorConfig())
    _, after = _run(scenario, "A", seed, tmp_path / "after", MonitorConfig(promoted=(_rule(),)))
    [(t_stalled, stalled)] = _verdicts(before)
    [(t_promoted, promoted)] = _verdicts(after)
    assert stalled.rule == "insert_stalled"
    assert (promoted.failure_class, promoted.rule) == ("UNKNOWN", "promoted:PLUG_ROTATED")
    assert promoted.skill == "insert_scripted"
    assert t_promoted < t_stalled
    assert promoted.evidence["window_s"] == 0.5
    assert promoted.evidence["torque_nm"] >= 0.3


@pytest.mark.parametrize("seed", SEEDS)
def test_a_promoted_rule_raises_nothing_on_the_baseline(tmp_path: Path, seed: int) -> None:
    config = MonitorConfig(promoted=(_rule(),))
    result, events = _run(SCENARIOS["baseline"], "A", seed, tmp_path, config)
    assert result.outcome == "success"
    assert _verdicts(events) == []


@pytest.mark.parametrize("seed", SEEDS)
def test_a_rule_naming_another_skill_leaves_the_run_as_it_was(tmp_path: Path, seed: int) -> None:
    scenario = SCENARIOS["yaw_mismatch"]
    _, before = _run(scenario, "A", seed, tmp_path / "before", MonitorConfig())
    config = MonitorConfig(promoted=(_rule(skill="insert_act"),))
    _, other = _run(scenario, "A", seed, tmp_path / "other", config)
    assert _verdicts(other) == _verdicts(before)


@pytest.mark.parametrize("seed", SEEDS)
def test_in_c_the_planner_still_handles_the_promoted_unknown(tmp_path: Path, seed: int) -> None:
    # No plan is adopted yet (T3): the verdict is UNKNOWN, so C asks the planner, which switches
    # to insert_act; the rule names insert_scripted and stays silent there.
    config = MonitorConfig(promoted=(_rule(),))
    result, events = _run(SCENARIOS["yaw_mismatch"], "C", seed, tmp_path, config)
    assert result.outcome == "success"
    assert [(v.skill, v.rule) for _, v in _verdicts(events)] == [
        ("insert_scripted", "promoted:PLUG_ROTATED")
    ]


def test_without_promoted_rules_the_monitor_config_and_its_hash_are_unchanged() -> None:
    # The keys as they were before promoted rules existed: every earlier run keeps its hash.
    assert MonitorConfig().as_dict() == {
        "dropout_s": 0.2,
        "force_budget_s": 1.0,
        "force_trigger_s": 0.5,
        "stuck_force_n": 10.0,
        "stuck_progress_mm": 0.2,
        "stuck_window_s": 0.5,
        "pose_tolerance_mm": 1.0,
        "pose_error_s": 0.2,
        "stall_progress_mm": 0.2,
        "stall_window_s": 1.0,
    }


def test_a_promoted_rule_is_part_of_the_monitor_hash() -> None:
    scenario = SCENARIOS["yaw_mismatch"]

    def monitor_part(config: MonitorConfig) -> str:
        return config_parts(scenario, "B", MockParams(), RecoveryConfig(), config, mock_registry())[
            "monitor"
        ]

    plain = monitor_part(MonitorConfig())
    with_rule = monitor_part(MonitorConfig(promoted=(_rule(),)))
    other_rule = monitor_part(MonitorConfig(promoted=(_rule(window_s=0.6),)))
    assert len({plain, with_rule, other_rule}) == 3
    assert MonitorConfig(promoted=(_rule(),)).as_dict()["promoted"][0]["name"] == "PLUG_ROTATED"


def test_two_promoted_rules_may_not_share_a_name() -> None:
    with pytest.raises(ValueError, match="names"):
        MonitorConfig(promoted=(_rule(), _rule(window_s=0.6)))


# Monitor-level: samples fed by hand, as the runtime feeds them.

PERIOD_S = 0.05


def _signals(
    height_mm: float = -2.0,
    force_n: float = 3.0,
    torque_nm: float = 0.0,
    xy_mm: float = 0.0,
    valid: bool = True,
) -> Signals:
    return Signals(
        height_mm=height_mm,
        xy_offset_mm=(xy_mm, 0.0),
        raw_force_n=(0.0, 0.0, force_n),
        tare_offset_n=(0.0, 0.0, 0.0),
        torque_nm=torque_nm,
        wrench_valid=valid,
    )


def _monitor(*rules: PromotedRule) -> Monitor:
    return Monitor(PERIOD_S, MonitorConfig(promoted=rules), force_time=lambda: 0.0)


def _feed(monitor: Monitor, samples: list[Signals]):  # type: ignore[no-untyped-def]
    return [monitor.observe(s) for s in samples]


def test_a_promoted_rule_waits_for_its_window_to_fill() -> None:
    monitor = _monitor(_rule())
    monitor.reset("insertion", skill="insert_scripted")
    verdicts = _feed(monitor, [_signals(torque_nm=0.5)] * 11)  # 0.5 s needs 11 samples
    assert verdicts[:10] == [None] * 10
    assert verdicts[10] is not None and verdicts[10].rule == "promoted:PLUG_ROTATED"


def test_a_promoted_rule_compares_the_torque_magnitude() -> None:
    monitor = _monitor(_rule())
    monitor.reset("insertion", skill="insert_scripted")
    verdicts = _feed(monitor, [_signals(torque_nm=-0.5)] * 11)
    assert verdicts[10] is not None and verdicts[10].rule == "promoted:PLUG_ROTATED"
    assert verdicts[10].evidence["torque_nm"] == -0.5  # recorded as read


def test_a_promoted_rule_is_silent_outside_the_insertion_profile_and_its_skill() -> None:
    # 20 samples: the rule's 0.5 s window fills at 11, the built-in stall rule's 1.0 s at 21.
    for profile, skill in [
        ("motion", "insert_scripted"),
        ("insertion", "insert_act"),
        ("insertion", None),  # the hold during a live planner call
    ]:
        monitor = _monitor(_rule())
        monitor.reset(profile, skill=skill)  # type: ignore[arg-type]
        assert _feed(monitor, [_signals(torque_nm=0.5)] * 20) == [None] * 20


def test_a_promoted_rule_never_reads_an_invalid_wrench() -> None:
    monitor = _monitor(_rule(conditions=[{"field": "progress_mm", "op": "<", "value": 0.2}]))
    monitor.reset("insertion", skill="insert_scripted")
    samples = [_signals(valid=True)] * 10 + [_signals(valid=False)]
    assert _feed(monitor, samples)[-1] is None


def test_a_known_class_wins_over_a_promoted_rule_on_the_same_sample() -> None:
    # 12 N with no progress and no XY error: stuck under force (0.5 s) and the promoted rule
    # (force over 11 N, 0.5 s) match on the same sample; the known class wins.
    rule = _rule(conditions=[{"field": "force_n", "op": ">", "value": 11.0}])
    monitor = _monitor(rule)
    monitor.reset("insertion", skill="insert_scripted")
    verdicts = _feed(monitor, [_signals(force_n=12.0)] * 11)
    verdict = verdicts[10]
    assert verdict is not None
    assert verdict.failure_class is FailureClass.INSERT_STUCK
    assert verdict.evidence["also_matched"] == ["promoted:PLUG_ROTATED"]


def test_a_promoted_rule_wins_over_the_stall_rule_on_the_same_sample() -> None:
    rule = _rule(window_s=1.0, conditions=[{"field": "torque_nm", "op": ">=", "value": 0.3}])
    monitor = _monitor(rule)
    monitor.reset("insertion", skill="insert_scripted")
    verdict = _feed(monitor, [_signals(torque_nm=0.5)] * 21)[20]
    assert verdict is not None and verdict.rule == "promoted:PLUG_ROTATED"
    assert verdict.evidence["also_matched"] == ["insert_stalled"]


def test_the_window_holds_the_longest_promoted_rule() -> None:
    monitor = _monitor(_rule(window_s=2.0))
    monitor.reset("insertion", skill="insert_scripted")
    _feed(monitor, [_signals()] * 200)
    assert monitor.buffered() == 41 + 5  # the 2.0 s window and the dropout window, no more
