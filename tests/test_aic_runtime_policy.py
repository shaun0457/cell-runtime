"""`RuntimePolicy.insert_cable`, the entry point aic_model calls, on any OS: `aic_model.policy`
and `adapters.aic.ros_port` (the ROS-only parts) are replaced by stubs, so the glue around the
policy body is tested here: the store, closing the port, and failures turned into False with
only the exception type logged."""

from __future__ import annotations

import importlib
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from aic_fakes import FakeNode

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

    def __init__(self, node: Any, move_robot: Any) -> None:
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
    policy_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(policy_module, "RosNodePort", _RaisingPort)
    node = _Node()

    assert _insert(policy_module, node) is False

    assert _Port.closed == 1
    assert node.logger.errors == ["cell-runtime goal failed: RuntimeError"]


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
