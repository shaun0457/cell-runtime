"""The mock `insert_act`: a stand-in for the learned ACT insert policy, with a different failure
profile from `insert_scripted` (it twists the plug to follow the torque, and it is slower)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cellrt.eval.runner import run_unit
from cellrt.eval.scenarios import Scenario, load_scenarios
from cellrt.runtime.events import Event
from cellrt.runtime.store import EventStore
from cellrt.skills.mock import Align, Approach, InsertAct, Retract, Verify, WaitSensor
from cellrt.skills.registry import SkillRegistry

PHASE0 = Path(__file__).resolve().parents[1] / "eval" / "scenarios" / "phase0.yaml"
SCENARIOS = {s.id: s for s in load_scenarios(PHASE0)}
SEEDS = range(8)

# The insert step of the task runs insert_act instead of insert_scripted.
ACT_INSERTS = SkillRegistry(
    [Approach(), Align(), InsertAct(name="insert_scripted"), Verify(), Retract(), WaitSensor()]
)


def _run(store: EventStore, scenario_id: str, seed: int) -> list[Event]:
    run = run_unit(SCENARIOS[scenario_id], "A", seed=seed, store=store, registry=ACT_INSERTS)
    return store.events(run.run_id)


@pytest.mark.parametrize("seed", SEEDS)
def test_insert_act_twists_a_wedged_plug_free_and_seats_it(store: EventStore, seed: int) -> None:
    events = _run(store, "yaw_mismatch", seed)

    assert [e.payload for e in events if e.type == "monitor_verdict"] == []
    assert [e.payload.kind for e in events if e.type == "fault_injected"] == ["yaw_mismatch"]
    assert [e.payload.kind for e in events if e.type == "fault_cleared"] == ["yaw_mismatch"]
    assert events[-1].payload.outcome == "success"


@pytest.mark.parametrize("seed", SEEDS)
def test_insert_act_is_slow_enough_to_stay_under_the_force_limit_in_a_tight_fit(
    store: EventStore, seed: int
) -> None:
    events = _run(store, "tight_fit", seed)

    assert [e.payload for e in events if e.type == "monitor_verdict"] == []
    assert max(e.payload.force_n for e in events if e.type == "signal_sampled") < 20.0
    assert events[-1].payload.outcome == "success"


@pytest.mark.parametrize("seed", SEEDS)
def test_insert_act_does_not_correct_a_pose_shift(store: EventStore, seed: int) -> None:
    events = _run(store, "pose_shift", seed)

    verdicts = [e.payload.failure_class for e in events if e.type == "monitor_verdict"]
    assert verdicts == ["POSE_ERROR"]
    assert events[-1].payload.outcome == "failed"


def test_a_slow_insert_meets_an_obstacle_as_a_stall_not_as_stuck(store: EventStore) -> None:
    # At 2 mm/s the push builds 0.5 N per step: the 1 s stall rule fires before force reaches the
    # 10 N stuck threshold. An accepted misread (ADR 006): a learned, slow insert reports an
    # obstacle as UNKNOWN.
    events = _run(store, "obstacle", seed=0)

    verdicts = [
        (e.payload.failure_class, e.payload.rule) for e in events if e.type == "monitor_verdict"
    ]
    assert verdicts == [("UNKNOWN", "insert_stalled")]


def test_a_yaw_mismatch_too_large_to_twist_out_within_the_stall_window_is_a_stall(
    store: EventStore,
) -> None:
    # The limit of the mock: 30° takes 1 s of twisting at 30°/s, as long as the stall window.
    spec = {
        "kind": "yaw_mismatch",
        "stall_depth_mm": [4.0, 4.0],
        "force_n": [7.0, 7.0],
        "torque_nm": [0.5, 0.5],
        "yaw_deg": [30.0, 30.0],
    }
    scenario = Scenario.model_validate({"id": "yaw_30", "faults": [spec]})
    run = run_unit(scenario, "A", seed=0, store=store, registry=ACT_INSERTS)
    events = store.events(run.run_id)

    verdicts = [e.payload.rule for e in events if e.type == "monitor_verdict"]
    assert verdicts == ["insert_stalled"]
