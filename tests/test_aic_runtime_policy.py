"""`RuntimePolicy.insert_cable`, the entry point aic_model calls, on any OS: `aic_model.policy`
and `adapters.aic.ros_port` (the ROS-only parts) are replaced by stubs, so the glue around the
policy body is tested here: the store, closing the port, and failures turned into False with
only the exception type logged."""

from __future__ import annotations

import importlib
import json
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from aic_fakes import FakeNode

from adapters.aic.faults import SPEC_REFUSED
from adapters.aic.manifest import (
    MANIFEST_INVALID,
    TASK_FIELDS,
    TASK_MISMATCH,
    TASK_NOT_IN_MANIFEST,
    scene_digest,
)
from cellrt.runtime.events import RunFinished
from cellrt.runtime.store import EventStore
from cellrt.runtime.task import Task

TASK = Task(id="trial_1_task_1", time_limit=180.0)
SECRET = "upstream text with a key: sk-123"


class _Logger:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def info(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        self.errors.append(msg)


class _Node:
    def __init__(self) -> None:
        self.logger = _Logger()

    def get_logger(self) -> _Logger:
        return self.logger


class _Policy:
    """Stands in for `aic_model.policy.Policy` (policy.py:71-79)."""

    def __init__(self, parent_node: Any) -> None:
        self._parent_node = parent_node

    def get_logger(self) -> _Logger:
        return self._parent_node.get_logger()


class _Port(FakeNode):
    closed = 0
    fail_on_init = False
    fail_on_close = False

    def __init__(self, node: Any, move_robot: Any, send_feedback: Any) -> None:
        if type(self).fail_on_init:
            raise RuntimeError(SECRET)
        super().__init__(task=TASK)

    def close(self) -> None:
        _Port.closed += 1  # on the base class, whichever subclass closes
        if type(self).fail_on_close:
            raise RuntimeError(SECRET)


class _RaisingPort(_Port):
    def sim_now_s(self) -> float:
        raise RuntimeError(SECRET)


@pytest.fixture
def policy_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[types.ModuleType]:
    aic_model = types.ModuleType("aic_model")
    policy = types.ModuleType("aic_model.policy")
    policy.Policy = _Policy  # type: ignore[attr-defined]
    ros_port = types.ModuleType("adapters.aic.ros_port")
    ros_port.RosNodePort = _Port  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "aic_model", aic_model)
    monkeypatch.setitem(sys.modules, "aic_model.policy", policy)
    monkeypatch.setitem(sys.modules, "adapters.aic.ros_port", ros_port)
    monkeypatch.delitem(sys.modules, "adapters.aic.RuntimePolicy", raising=False)
    monkeypatch.setenv("CELLRT_STORE", str(tmp_path / "store" / "aic.sqlite"))
    monkeypatch.delenv("CELLRT_SESSION", raising=False)
    monkeypatch.delenv("CELLRT_FAULT", raising=False)
    monkeypatch.delenv("CELLRT_MANIFEST", raising=False)
    _Port.closed, _Port.fail_on_init, _Port.fail_on_close = 0, False, False
    module = importlib.import_module("adapters.aic.RuntimePolicy")
    yield module
    sys.modules.pop("adapters.aic.RuntimePolicy", None)


def _insert(module: types.ModuleType, node: _Node) -> object:
    policy = module.RuntimePolicy(node)
    return policy.insert_cable(TASK, None, None, None)


def test_insert_cable_runs_the_goal_into_the_store_and_closes_the_port(
    policy_module: types.ModuleType, tmp_path: Path
) -> None:
    node = _Node()

    assert _insert(policy_module, node) is True

    assert _Port.closed == 1
    with EventStore(tmp_path / "store" / "aic.sqlite") as store:
        (run,) = store.runs()
        end = store.events(run.run_id)[-1].payload
    assert isinstance(end, RunFinished) and end.outcome == "success"
    assert node.logger.errors == []


def test_a_failing_goal_returns_false_logs_the_type_only_and_closes_the_port(
    policy_module: types.ModuleType,
) -> None:
    node = _Node()
    policy = policy_module.RuntimePolicy(node)

    # A task message without its fields: the goal fails before a run starts.
    assert policy.insert_cable(object(), None, None, None) is False

    assert _Port.closed == 1
    assert node.logger.errors == ["cell-runtime goal failed: AttributeError"]


def test_a_node_that_raises_is_a_recorded_run_not_a_crash(
    policy_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(policy_module, "RosNodePort", _RaisingPort)
    node = _Node()

    assert _insert(policy_module, node) is False

    assert _Port.closed == 1
    assert node.logger.errors == []
    with EventStore(tmp_path / "store" / "aic.sqlite") as store:
        (run,) = store.runs()
        end = store.events(run.run_id)[-1].payload
    assert isinstance(end, RunFinished)
    assert (end.outcome, end.reason) == ("aborted", "node_error")


def test_a_port_that_cannot_be_built_returns_false_without_raising(
    policy_module: types.ModuleType,
) -> None:
    _Port.fail_on_init = True
    node = _Node()

    assert _insert(policy_module, node) is False

    assert _Port.closed == 0  # nothing to close
    assert node.logger.errors == ["cell-runtime goal failed: RuntimeError"]


def test_a_failing_close_does_not_change_the_result(policy_module: types.ModuleType) -> None:
    _Port.fail_on_close = True
    node = _Node()

    assert _insert(policy_module, node) is True

    assert node.logger.errors == ["cell-runtime close failed: RuntimeError"]
    assert all(SECRET not in e for e in node.logger.errors)


# --- one unit per goal (#24 F7) -----------------------------------------------------------------
# aic_engine sends no trial id: each goal carries only its Task, and in the sample config every
# trial's task is `task_1` (sample_config.yaml:142, :236, :318). One RuntimePolicy lives for
# the whole engine run (created on the node's first configure, aic_engine.cpp:1081-1090), so it
# numbers its goals; CELLRT_SESSION names the engine config they come from.


def _units(tmp_path: Path) -> list[tuple[str, int]]:
    with EventStore(tmp_path / "store" / "aic.sqlite") as store:
        return [(r.scenario_id, r.attempt) for r in store.runs()]


def test_each_goal_of_an_engine_run_is_its_own_unit(
    policy_module: types.ModuleType, tmp_path: Path
) -> None:
    policy = policy_module.RuntimePolicy(_Node())

    for _ in range(3):  # three trials, each with the task `task_1`
        assert policy.insert_cable(TASK, None, None, None) is True

    assert sorted(_units(tmp_path)) == [
        ("aic/goal1/trial_1_task_1", 1),
        ("aic/goal2/trial_1_task_1", 1),
        ("aic/goal3/trial_1_task_1", 1),
    ]


def test_the_session_names_the_engine_config_and_a_rerun_is_a_new_attempt(
    policy_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CELLRT_SESSION", "sample_config")

    for _ in range(2):  # the same engine config run twice: a new policy each time
        assert _insert(policy_module, _Node()) is True

    assert sorted(_units(tmp_path)) == [
        ("sample_config/goal1/trial_1_task_1", 1),
        ("sample_config/goal1/trial_1_task_1", 2),
    ]


def test_a_goal_that_fails_before_its_run_still_takes_its_number(
    policy_module: types.ModuleType, tmp_path: Path
) -> None:
    policy = policy_module.RuntimePolicy(_Node())

    assert policy.insert_cable(object(), None, None, None) is False
    assert policy.insert_cable(TASK, None, None, None) is True

    assert _units(tmp_path) == [("aic/goal2/trial_1_task_1", 1)]


@pytest.mark.parametrize("label", ["", "a" * 65, "../up", "has space", "-lead", "x/y"])
def test_a_session_label_that_is_not_a_plain_name_is_refused_at_start(
    policy_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch, label: str
) -> None:
    monkeypatch.setenv("CELLRT_SESSION", label)

    with pytest.raises(ValueError, match=r"^CELLRT_SESSION must be"):
        policy_module.RuntimePolicy(_Node())


def test_a_session_label_of_64_characters_is_accepted(
    policy_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    label = "a" * 64
    monkeypatch.setenv("CELLRT_SESSION", label)

    assert _insert(policy_module, _Node()) is True

    assert _units(tmp_path) == [(f"{label}/goal1/trial_1_task_1", 1)]


def test_the_fault_goes_to_the_goals_cellrt_fault_names(
    policy_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CELLRT_FAULT", '{"kind": "command_dropout", "duration_s": 3, "goals": [2]}')
    policy = policy_module.RuntimePolicy(_Node())

    for _ in range(3):
        assert policy.insert_cable(TASK, None, None, None) is True

    with EventStore(tmp_path / "store" / "aic.sqlite") as store:
        faulted = [
            r.scenario_id
            for r in store.runs()
            if any(e.payload.type == "fault_injected" for e in store.events(r.run_id))
        ]
    assert faulted == ["aic/goal2/trial_1_task_1"]


# --- units named by a manifest (ADR 027) ---------------------------------------------------------
# With CELLRT_MANIFEST, the task id names the unit: the manifest maps it to its scene and repeat.

SCENE_TASK = Task(id="nominal_sfp_r1", time_limit=180.0)


def _manifest(
    path: Path, task_ids: list[str], scene: str = "nominal_sfp", **task_change: object
) -> Path:
    task = {k: v for k, v in SCENE_TASK.model_dump(mode="json").items() if k in TASK_FIELDS}
    task["time_limit"] = int(SCENE_TASK.time_limit)  # whole seconds, as in an engine config
    trial = {"scene": {}, "task": {**task, **task_change}}
    data = {
        "name": "tracer-v1",
        "version": 1,
        "scenes": {scene: {"family": "nominal", "digest": scene_digest(trial), "trial": trial}},
        "units": {t: {"scene": scene, "seed": int(t.rsplit("_r", 1)[1])} for t in task_ids},
    }
    path.write_text(json.dumps(data), "utf-8")
    return path


def test_a_manifest_names_each_goal_by_its_scene_and_repeat(
    policy_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CELLRT_MANIFEST", str(_manifest(tmp_path / "m.json", ["nominal_sfp_r1"])))
    policy = policy_module.RuntimePolicy(_Node())

    assert policy.insert_cable(SCENE_TASK, None, None, None) is True

    with EventStore(tmp_path / "store" / "aic.sqlite") as store:
        (run,) = store.runs()
    assert run.run_id == "tracer-v1/nominal_sfp/A/1/a1"


def test_with_a_manifest_an_unknown_task_id_is_refused_before_the_arm_moves(
    policy_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _manifest(tmp_path / "m.json", ["other_r0"], scene="other")
    monkeypatch.setenv("CELLRT_MANIFEST", str(path))
    node = _Node()

    assert policy_module.RuntimePolicy(node).insert_cable(SCENE_TASK, None, None, None) is False

    assert node.logger.errors == [f"cell-runtime goal refused: {TASK_NOT_IN_MANIFEST}"]
    assert _Port.closed == 1
    assert not (tmp_path / "store" / "aic.sqlite").exists()  # no run, so no command


def test_with_a_manifest_a_task_that_differs_from_its_scene_is_refused(
    policy_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _manifest(tmp_path / "m.json", ["nominal_sfp_r1"], port_name="sfp_port_1")
    monkeypatch.setenv("CELLRT_MANIFEST", str(path))
    node = _Node()

    assert policy_module.RuntimePolicy(node).insert_cable(SCENE_TASK, None, None, None) is False

    assert node.logger.errors == [f"cell-runtime goal refused: {TASK_MISMATCH}"]
    assert not (tmp_path / "store" / "aic.sqlite").exists()


def test_a_manifest_that_does_not_load_refuses_every_goal(
    policy_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "m.json"
    path.write_text('{"name": "sk-123"', "utf-8")
    monkeypatch.setenv("CELLRT_MANIFEST", str(path))
    node = _Node()
    policy = policy_module.RuntimePolicy(node)

    assert [policy.insert_cable(SCENE_TASK, None, None, None) for _ in range(2)] == [False] * 2
    assert node.logger.errors == [f"cell-runtime goal refused: {MANIFEST_INVALID}"] * 2
    assert not (tmp_path / "store" / "aic.sqlite").exists()


def test_a_blank_manifest_variable_keeps_the_goal_counting(
    policy_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CELLRT_MANIFEST", "  ")

    assert _insert(policy_module, _Node()) is True

    assert _units(tmp_path) == [("aic/goal1/trial_1_task_1", 1)]


def test_a_bad_fault_refuses_every_goal_with_the_fixed_message(
    policy_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(
        "CELLRT_FAULT", '{"kind": "command_dropout", "duration_s": 1, "x": "sk-123"}'
    )
    node = _Node()
    policy = policy_module.RuntimePolicy(node)

    assert [policy.insert_cable(TASK, None, None, None) for _ in range(2)] == [False, False]
    assert node.logger.errors == [f"cell-runtime goal refused: {SPEC_REFUSED}"] * 2
    assert not (tmp_path / "store" / "aic.sqlite").exists()  # the arm never moved
