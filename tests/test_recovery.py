"""Setting B: known failure classes are recovered by the behaviour tree within budgets."""

from __future__ import annotations

from pathlib import Path

import pytest

from cellrt.eval.runner import run_unit
from cellrt.eval.scenarios import Scenario
from cellrt.executor.recovery import Plan, RecoveryConfig
from cellrt.runtime.state import fold, state_at
from cellrt.runtime.store import EventStore


def _run(tmp_path: Path, scenario: Scenario, setting: str, seed: int, **kwargs) -> list:
    with EventStore(tmp_path / f"{setting}.sqlite") as store:
        return store.events(run_unit(scenario, setting, seed=seed, store=store, **kwargs).run_id)


def _recoveries(events: list) -> list[tuple[str, str, int, int]]:
    return [
        (
            e.payload.failure_class,
            e.payload.action,
            e.payload.count_for_class,
            e.payload.count_total,
        )
        for e in events
        if e.type == "recovery_started"
    ]


def _scenario(scenario_id: str, fault: dict) -> Scenario:
    return Scenario.model_validate({"id": scenario_id, "faults": [fault]})


POSE_TWICE = _scenario(
    "pose_twice",
    {
        "kind": "pose_shift",
        "trigger_height_mm": [0.5, 3.0],
        "shift_mm": [2.0, 4.0],
        "attempts": [2, 2],
    },
)


@pytest.mark.parametrize("seed", range(5))
def test_a_pose_error_is_recovered_by_realigning(tmp_path: Path, seed: int) -> None:
    a = _run(tmp_path, POSE_TWICE, "A", seed)
    b = _run(tmp_path, POSE_TWICE, "B", seed)

    assert a[-1].payload.outcome == "failed"
    assert b[-1].payload.outcome == "success"
    assert _recoveries(b) == [
        ("POSE_ERROR", "realign", 1, 1),
        ("POSE_ERROR", "realign", 2, 2),
    ]


OBSTACLE_TWICE = _scenario(
    "obstacle_twice", {"kind": "obstacle", "depth_mm": [2.0, 6.0], "attempts": [2, 2]}
)


@pytest.mark.parametrize("seed", range(5))
def test_a_stuck_insertion_is_recovered_by_retracting_and_realigning(
    tmp_path: Path, seed: int
) -> None:
    a = _run(tmp_path, OBSTACLE_TWICE, "A", seed)
    b = _run(tmp_path, OBSTACLE_TWICE, "B", seed)

    assert a[-1].payload.outcome == "failed"
    assert b[-1].payload.outcome == "success"
    assert _recoveries(b) == [
        ("INSERT_STUCK", "retract_realign", 1, 1),
        ("INSERT_STUCK", "retract_realign", 2, 2),
    ]


TIGHT = _scenario(
    "tight", {"kind": "tight_fit", "onset_depth_mm": [1.0, 3.0], "force_n_per_mm_s": [5.5, 7.5]}
)


@pytest.mark.parametrize("seed", range(5))
def test_a_force_limit_is_recovered_by_retracting_and_inserting_at_half_speed(
    tmp_path: Path, seed: int
) -> None:
    a = _run(tmp_path, TIGHT, "A", seed)
    b = _run(tmp_path, TIGHT, "B", seed)

    assert a[-1].payload.outcome == "failed"
    assert b[-1].payload.outcome == "success"
    assert _recoveries(b) == [("FORCE_LIMIT", "retract_slow", 1, 1)]
    reinsert = [e.payload for e in b if e.type == "skill_started"][-2]
    assert (reinsert.skill, reinsert.params) == ("insert_scripted", {"speed_scale": 0.5})


DROPOUT = _scenario(
    "dropout", {"kind": "sensor_dropout", "onset_s": [1.0, 4.0], "duration_s": [0.5, 1.0]}
)


@pytest.mark.parametrize("seed", range(5))
def test_a_sensor_dropout_is_recovered_by_waiting_for_the_signal(tmp_path: Path, seed: int) -> None:
    a = _run(tmp_path, DROPOUT, "A", seed)
    b = _run(tmp_path, DROPOUT, "B", seed)

    assert a[-1].payload.outcome == "failed"
    assert b[-1].payload.outcome == "success"
    assert _recoveries(b) == [("SENSOR_DROPOUT", "wait_sensor", 1, 1)]


SHORT_STALL = _scenario(
    "short_stall", {"kind": "controller_stall", "onset_s": [0.5, 1.0], "duration_s": [4.0, 6.0]}
)


@pytest.mark.parametrize("seed", range(5))
def test_a_timeout_is_recovered_by_retrying_the_skill(tmp_path: Path, seed: int) -> None:
    a = _run(tmp_path, SHORT_STALL, "A", seed)
    b = _run(tmp_path, SHORT_STALL, "B", seed)

    assert a[-1].payload.outcome == "failed"
    assert b[-1].payload.outcome == "success"
    assert _recoveries(b) == [("TIMEOUT", "retry", 1, 1)]
    started = [e.payload.skill for e in b if e.type == "skill_started"]
    assert started[:2] == ["approach", "approach"]


def test_an_unknown_failure_is_not_recovered(tmp_path: Path) -> None:
    yaw = _scenario(
        "yaw",
        {
            "kind": "yaw_mismatch",
            "stall_depth_mm": [3.0, 5.0],
            "force_n": [6.0, 9.0],
            "torque_nm": [0.4, 0.8],
        },
    )

    b = _run(tmp_path, yaw, "B", seed=0)

    assert _recoveries(b) == []
    assert b[-1].payload.reason == "insert_scripted failed: UNKNOWN"


# Faults that outlast each class budget: (scenario, recoveries made, failure reason).
OUTLAST_BUDGET = {
    "POSE_ERROR": (
        _scenario(
            "pose_thrice",
            {
                "kind": "pose_shift",
                "trigger_height_mm": [0.5, 3.0],
                "shift_mm": [2.0, 4.0],
                "attempts": [3, 3],
            },
        ),
        2,
        "insert_scripted failed: POSE_ERROR (POSE_ERROR recovery budget used up)",
    ),
    "INSERT_STUCK": (
        _scenario(
            "obstacle_thrice", {"kind": "obstacle", "depth_mm": [2.0, 6.0], "attempts": [3, 3]}
        ),
        2,
        "insert_scripted failed: INSERT_STUCK (INSERT_STUCK recovery budget used up)",
    ),
    "FORCE_LIMIT": (
        _scenario(
            "very_tight",
            {"kind": "tight_fit", "onset_depth_mm": [1.0, 3.0], "force_n_per_mm_s": [11.0, 12.0]},
        ),
        1,
        "insert_scripted failed: FORCE_LIMIT (FORCE_LIMIT recovery budget used up)",
    ),
    "TIMEOUT": (
        _scenario(
            "long_stall",
            {"kind": "controller_stall", "onset_s": [0.5, 0.5], "duration_s": [20.0, 20.0]},
        ),
        1,
        "approach failed: TIMEOUT (TIMEOUT recovery budget used up)",
    ),
    "SENSOR_DROPOUT": (
        Scenario.model_validate(
            {
                "id": "two_dropouts",
                "faults": [
                    {"kind": "sensor_dropout", "onset_s": [1.0, 1.0], "duration_s": [0.5, 0.5]},
                    {"kind": "sensor_dropout", "onset_s": [4.0, 4.0], "duration_s": [0.5, 0.5]},
                ],
            }
        ),
        1,
        "(SENSOR_DROPOUT recovery budget used up)",
    ),
}


@pytest.mark.parametrize("failure_class", OUTLAST_BUDGET)
def test_the_run_fails_once_a_class_budget_is_used_up(tmp_path: Path, failure_class: str) -> None:
    scenario, made, reason = OUTLAST_BUDGET[failure_class]

    b = _run(tmp_path, scenario, "B", seed=0)

    assert [(c, n) for c, _, n, _ in _recoveries(b)] == [
        (failure_class, i) for i in range(1, made + 1)
    ]
    assert b[-1].payload.outcome == "failed"
    assert b[-1].payload.reason.endswith(reason)


def test_the_run_fails_once_the_task_budget_is_used_up(tmp_path: Path) -> None:
    b = _run(tmp_path, POSE_TWICE, "B", seed=0, recovery=RecoveryConfig(total_budget=1))

    assert _recoveries(b) == [("POSE_ERROR", "realign", 1, 1)]
    assert (
        b[-1].payload.reason == "insert_scripted failed: POSE_ERROR (task recovery budget used up)"
    )


def test_recovery_parameters_are_part_of_the_config_hash(tmp_path: Path) -> None:
    hashes = []
    for i, config in enumerate((RecoveryConfig(), RecoveryConfig(total_budget=3))):
        with EventStore(tmp_path / f"{i}.sqlite") as store:
            events = store.events(
                run_unit(POSE_TWICE, "B", seed=0, store=store, recovery=config).run_id
            )
        hashes.append(events[0].payload.config_hash)

    assert hashes[0] != hashes[1]


def test_a_recovered_failure_still_shows_as_a_failed_node(tmp_path: Path) -> None:
    b = _run(tmp_path, POSE_TWICE, "B", seed=0)

    changes = [(e.payload.node, e.payload.status) for e in b if e.type == "bt_node_changed"]
    assert ("insert_scripted", "FAILURE") in changes


def test_waiting_for_the_sensor_gives_up_at_its_limit(tmp_path: Path) -> None:
    long_dropout = _scenario(
        "long_dropout", {"kind": "sensor_dropout", "onset_s": [1.0, 1.0], "duration_s": [5.0, 5.0]}
    )

    b = _run(tmp_path, long_dropout, "B", seed=0)

    waits = [
        e.payload for e in b if e.type == "skill_finished" and e.payload.skill == "wait_sensor"
    ]
    assert [(w.ok, w.detail["waited_s"]) for w in waits] == [(False, 2.0)]
    assert b[-1].payload.reason == "wait_sensor failed: sensor still invalid"


def test_the_recovery_plans_are_part_of_the_config_hash(tmp_path: Path) -> None:
    no_slowdown = dict(RecoveryConfig().plans)
    no_slowdown["FORCE_LIMIT"] = Plan("retract", ("retract",))
    hashes = []
    for i, config in enumerate((RecoveryConfig(), RecoveryConfig(plans=no_slowdown))):
        with EventStore(tmp_path / f"{i}.sqlite") as store:
            events = store.events(
                run_unit(POSE_TWICE, "B", seed=0, store=store, recovery=config).run_id
            )
        hashes.append(events[0].payload.config_hash)

    assert hashes[0] != hashes[1]


def test_setting_a_ignores_the_recovery_config(tmp_path: Path) -> None:
    hashes = []
    for i, config in enumerate((RecoveryConfig(), RecoveryConfig(total_budget=3))):
        with EventStore(tmp_path / f"{i}.sqlite") as store:
            events = store.events(
                run_unit(POSE_TWICE, "A", seed=0, store=store, recovery=config).run_id
            )
        hashes.append(events[0].payload.config_hash)

    assert hashes[0] == hashes[1]


@pytest.mark.parametrize(
    "bad",
    [
        {"budgets": {"POSE_EROR": 2}},
        {"sensor_wait_s": 30.0},
        {"retract_to_mm": 40.0},
        {"total_budget": -1},
    ],
)
def test_recovery_configs_that_cannot_work_are_rejected(bad: dict) -> None:
    with pytest.raises(ValueError):
        RecoveryConfig(**bad)


DROPOUT_THEN_OBSTACLE = Scenario.model_validate(
    {
        "id": "dropout_then_obstacle",
        "faults": [
            {"kind": "sensor_dropout", "onset_s": [1.0, 1.0], "duration_s": [0.5, 0.5]},
            {"kind": "obstacle", "depth_mm": [3.0, 3.0], "attempts": [2, 2]},
        ],
    }
)


def test_recoveries_used_so_far_are_part_of_the_folded_state(tmp_path: Path) -> None:
    events = _run(tmp_path, DROPOUT_THEN_OBSTACLE, "B", 0)
    recoveries = [e for e in events if e.type == "recovery_started"]

    # Each recovery's own counts are what the fold says right after it.
    for e in recoveries:
        state = state_at(events, e.seq)
        assert (state.recoveries_by_class[e.payload.failure_class], state.recoveries_total) == (
            e.payload.count_for_class,
            e.payload.count_total,
        )
    final = fold(events)
    assert dict(final.recoveries_by_class) == {"SENSOR_DROPOUT": 1, "INSERT_STUCK": 2}
    assert final.recoveries_total == 3
