"""An adopted promoted rule in a run (ADR 021, ticket T3): its plan runs in B and C without the
planner, inside the UNKNOWN class budget and the task budget."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cellrt.eval.runner import run_unit
from cellrt.eval.scenarios import Scenario, load_scenarios
from cellrt.executor.recovery import RecoveryConfig
from cellrt.monitor.classifier import FailureClass, MonitorConfig
from cellrt.monitor.promoted import PromotedPlan, PromotedRule
from cellrt.runtime.events import (
    Event,
    MonitorVerdict,
    PlannerCalled,
    RecoveryStarted,
    RunFinished,
    SkillStarted,
)
from cellrt.runtime.store import EventStore

PHASE3 = Path(__file__).resolve().parents[1] / "eval" / "scenarios" / "phase3.yaml"
SCENARIOS = {s.id: s for s in load_scenarios(PHASE3)}
SEEDS = range(3)
UNKNOWN = FailureClass.UNKNOWN.value

ROTATED = PromotedRule.model_validate_json(
    json.dumps(
        {
            "name": "PLUG_ROTATED",
            "skill": "insert_scripted",
            "window_s": 0.5,
            "conditions": [
                {"field": "torque_nm", "op": ">=", "value": 0.3},
                {"field": "progress_mm", "op": "<", "value": 0.2},
            ],
        }
    )
)


def _plan(action: str, skill: str | None = None) -> PromotedPlan:
    return PromotedPlan.model_validate_json(json.dumps({"action": action, "skill": skill}))


SWITCH = _plan("switch_skill", "insert_act")


def _run(
    tmp_path: Path,
    setting: str,
    seed: int,
    plan: PromotedPlan | None = SWITCH,
    scenario: Scenario | None = None,
    unknown_budget: int = 2,
    total_budget: int = RecoveryConfig().total_budget,
) -> list[Event]:
    monitor = MonitorConfig(promoted=(ROTATED,) if plan else ())
    budgets: dict[str, int] = {**RecoveryConfig().budgets}
    if plan:
        budgets[UNKNOWN] = unknown_budget
    recovery = RecoveryConfig(
        budgets=budgets,
        promoted={"PLUG_ROTATED": plan} if plan else {},
        total_budget=total_budget,
    )
    scenario = scenario or SCENARIOS["yaw_mismatch"]
    with EventStore(tmp_path / f"{setting}-{seed}.sqlite") as store:
        result = run_unit(scenario, setting, seed, store, recovery=recovery, monitor=monitor)  # type: ignore[arg-type]
        return store.events(result.run_id)


def _of(events: list[Event], kind: type) -> list[Any]:
    return [e.payload for e in events if isinstance(e.payload, kind)]


def _end(events: list[Event]) -> RunFinished:
    end = events[-1].payload
    assert isinstance(end, RunFinished)
    return end


@pytest.mark.parametrize("setting", ["B", "C"])
@pytest.mark.parametrize("seed", SEEDS)
def test_the_rotated_plug_is_handled_without_the_planner(
    tmp_path: Path, setting: str, seed: int
) -> None:
    events = _run(tmp_path, setting, seed)
    assert _end(events).outcome == "success"
    assert _of(events, PlannerCalled) == []
    [verdict] = _of(events, MonitorVerdict)
    assert (verdict.failure_class, verdict.rule) == (UNKNOWN, "promoted:PLUG_ROTATED")
    [recovery] = _of(events, RecoveryStarted)
    assert recovery.action == "promoted:PLUG_ROTATED:switch_skill:insert_act"
    assert (recovery.failure_class, recovery.count_for_class, recovery.count_total) == (
        UNKNOWN,
        1,
        1,
    )
    assert [s.skill for s in _of(events, SkillStarted)][-2:] == ["insert_act", "verify"]


@pytest.mark.parametrize("seed", SEEDS)
def test_without_the_rule_b_still_fails_and_c_still_asks(tmp_path: Path, seed: int) -> None:
    b = _run(tmp_path / "b", "B", seed, plan=None)
    assert _end(b).outcome == "failed"
    c = _run(tmp_path / "c", "C", seed, plan=None)
    assert len(_of(c, PlannerCalled)) == 1


def test_setting_a_never_recovers(tmp_path: Path) -> None:
    events = _run(tmp_path, "A", 0)
    assert _end(events).outcome == "failed"
    assert _of(events, RecoveryStarted) == []


@pytest.mark.parametrize("action", ["abort", "escalate"])
def test_an_abort_or_escalate_plan_ends_the_run_at_once(tmp_path: Path, action: str) -> None:
    events = _run(tmp_path, "C", 0, plan=_plan(action))
    end = _end(events)
    assert (end.outcome, end.reason) == (
        "aborted",
        f"insert_scripted failed: UNKNOWN (promoted PLUG_ROTATED: {action})",
    )
    assert _of(events, PlannerCalled) == []
    assert _of(events, RecoveryStarted) == []


def test_a_retry_plan_runs_within_the_unknown_budget(tmp_path: Path) -> None:
    # The rotated plug stalls insert_scripted every time: two retries, then the budget is out.
    events = _run(tmp_path, "B", 0, plan=_plan("retry"), unknown_budget=2)
    end = _end(events)
    assert end.outcome == "failed"
    assert end.reason == "insert_scripted failed: UNKNOWN (UNKNOWN recovery budget used up)"
    assert [r.count_for_class for r in _of(events, RecoveryStarted)] == [1, 2]
    assert len(_of(events, MonitorVerdict)) == 3


@pytest.mark.parametrize("total, recoveries", [(1, 1), (0, 0)])
def test_a_retry_plan_stops_at_the_task_budget_too(
    tmp_path: Path, total: int, recoveries: int
) -> None:
    # The UNKNOWN budget (2) would allow more: the task budget is the tighter bound.
    events = _run(tmp_path, "B", 0, plan=_plan("retry"), unknown_budget=2, total_budget=total)
    assert len(_of(events, RecoveryStarted)) == recoveries
    assert _end(events).reason == "insert_scripted failed: UNKNOWN (task recovery budget used up)"


def test_with_no_unknown_budget_the_plan_does_not_run(tmp_path: Path) -> None:
    events = _run(tmp_path, "C", 0, unknown_budget=0)
    assert _end(events).reason == (
        "insert_scripted failed: UNKNOWN (UNKNOWN recovery budget used up)"
    )
    assert _of(events, PlannerCalled) == []  # a promoted verdict never goes to the planner


@pytest.mark.parametrize("seed", SEEDS)
def test_the_clean_run_is_unchanged_by_the_rule(tmp_path: Path, seed: int) -> None:
    events = _run(tmp_path, "B", seed, scenario=SCENARIOS["baseline"])
    assert _end(events).outcome == "success"
    assert _of(events, MonitorVerdict) == []


def test_without_promoted_plans_the_recovery_hash_is_unchanged() -> None:
    # As it was before promoted plans existed, in JSON as the hash reads it: every earlier run
    # keeps its hash.
    assert json.loads(json.dumps(RecoveryConfig().as_dict())) == {
        "budgets": {
            "FORCE_LIMIT": 1,
            "INSERT_STUCK": 2,
            "POSE_ERROR": 2,
            "SENSOR_DROPOUT": 1,
            "TIMEOUT": 1,
        },
        "plans": {
            "FORCE_LIMIT": {"action": "retract_slow", "before": ["retract"], "slow": True},
            "INSERT_STUCK": {
                "action": "retract_realign",
                "before": ["retract", "align"],
                "slow": False,
            },
            "POSE_ERROR": {"action": "realign", "before": ["align"], "slow": False},
            "SENSOR_DROPOUT": {"action": "wait_sensor", "before": ["wait_sensor"], "slow": False},
            "TIMEOUT": {"action": "retry", "before": [], "slow": False},
        },
        "retract_to_mm": 5.0,
        "sensor_valid_samples": 3,
        "sensor_wait_s": 2.0,
        "slow_speed_scale": 0.5,
        "total_budget": 4,
    }
    with_plan = RecoveryConfig(promoted={"PLUG_ROTATED": SWITCH}).as_dict()
    assert with_plan["promoted"] == {
        "PLUG_ROTATED": {"action": "switch_skill", "skill": "insert_act"}
    }


def test_promoted_plans_are_keyed_by_rule_name() -> None:
    with pytest.raises(ValueError, match="rule name"):
        RecoveryConfig(promoted={"not a name": SWITCH})
