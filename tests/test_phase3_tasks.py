"""Task scenarios (ADR 023, `eval/scenarios/phase3-tasks.yaml`): a task whose right plan is not the
fixed one, so the tree a task gets matters. The SC task and its fault are ours."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from cellrt.eval.runner import run_unit
from cellrt.eval.scenarios import Scenario, load_scenarios
from cellrt.executor.trees import Setting
from cellrt.planner.task_plan import TablePlanner
from cellrt.runtime.events import Event, FaultInjected, PlannerCalled, RunFinished, SkillStarted
from cellrt.runtime.store import EventStore
from cellrt.runtime.task import Task
from cellrt.skills.registry import SkillRegistry

SCENARIOS = Path(__file__).resolve().parent.parent / "eval" / "scenarios"
SEEDS = range(5)


def _scenarios(name: str = "phase3-tasks.yaml") -> dict[str, Scenario]:
    return {s.id: s for s in load_scenarios(SCENARIOS / name)}


def _finished(events: Sequence[Event]) -> RunFinished:
    finished = events[-1].payload
    assert isinstance(finished, RunFinished)
    return finished


def _count(events: Sequence[Event], kind: str) -> int:
    return sum(e.type == kind for e in events)


def _skills(events: Sequence[Event]) -> list[str]:
    return [e.payload.skill for e in events if isinstance(e.payload, SkillStarted)]


def _faults(events: Sequence[Event]) -> list[tuple[str, str, str, dict[str, Any]]]:
    return [
        (e.payload.fault_id, e.payload.kind, e.payload.label, e.payload.params)
        for e in events
        if isinstance(e.payload, FaultInjected)
    ]


@dataclass
class SpyTable:
    """The table planner, recording everything it was handed."""

    seen: list[str] = field(default_factory=list)
    inner: TablePlanner = field(default_factory=TablePlanner)

    @property
    def name(self) -> str:
        return self.inner.name

    async def plan(self, task: Task, registry: SkillRegistry) -> Iterable[str]:
        self.seen.append(task.model_dump_json() + json.dumps(sorted(registry.names())))
        return await self.inner.plan(task, registry)

    def describe(self) -> Mapping[str, object]:
        return self.inner.describe()


def test_the_task_file_holds_an_sfp_task_and_an_sc_task_with_phase3s_rotated_plug() -> None:
    scenarios = _scenarios()

    assert set(scenarios) == {"sfp", "sc"}
    assert (scenarios["sfp"].task.plug_type, scenarios["sfp"].faults) == ("sfp", [])
    assert scenarios["sc"].task.plug_type == "sc"
    assert scenarios["sc"].faults == _scenarios("phase3.yaml")["yaw_mismatch"].faults


@pytest.mark.parametrize("seed", SEEDS)
def test_on_sc_b_fails_with_the_fixed_tree_and_succeeds_with_the_table_without_a_recovery(
    store: EventStore, seed: int
) -> None:
    sc = _scenarios()["sc"]

    fixed = store.events(run_unit(sc, "B", seed, store).run_id)
    table = store.events(run_unit(sc, "B", seed, store, tree=TablePlanner()).run_id)

    assert (_finished(fixed).outcome, _finished(fixed).reason) == (
        "failed",
        "insert_scripted failed: UNKNOWN",
    )
    assert _finished(table).outcome == "success"
    assert _count(table, "recovery_started") == 0
    assert _skills(table) == ["approach", "align", "insert_act", "verify"]


@pytest.mark.parametrize("seed", SEEDS)
def test_every_tree_faces_the_same_labelled_fault_and_the_table_clears_it_before_a_verdict(
    store: EventStore, seed: int
) -> None:
    sc = _scenarios()["sc"]

    fixed = store.events(run_unit(sc, "B", seed, store).run_id)
    table = store.events(run_unit(sc, "B", seed, store, tree=TablePlanner()).run_id)

    assert _faults(fixed) == _faults(table)
    [(fault_id, kind, label, _)] = _faults(table)
    assert (fault_id, kind, label) == ("yaw_mismatch#0", "yaw_mismatch", "UNKNOWN")
    # insert_act twists the plug free before the Monitor sees a stall, so the fault has no
    # verdict and the eval's confusion matrix counts it as missed (ADR 023).
    assert (_count(table, "fault_cleared"), _count(table, "monitor_verdict")) == (1, 0)


@pytest.mark.parametrize("seed", SEEDS)
def test_on_sc_c_asks_the_planner_with_the_fixed_tree_and_not_with_the_table(
    store: EventStore, seed: int
) -> None:
    sc = _scenarios()["sc"]

    fixed = store.events(run_unit(sc, "C", seed, store).run_id)
    table = store.events(run_unit(sc, "C", seed, store, tree=TablePlanner()).run_id)

    assert (_finished(fixed).outcome, _count(fixed, "planner_called")) == ("success", 1)
    assert (_finished(table).outcome, _count(table, "planner_called")) == ("success", 0)
    # In the mock a planner call takes no sim time (the frozen wait, ADR 016), so this is only
    # insert_act's half-speed descent, which the table's plan uses from the start; the fixed
    # steps descend at full speed until the stall.
    assert _finished(table).duration > _finished(fixed).duration


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("setting", ["A", "B", "C"])
def test_sfp_runs_the_same_skills_to_the_same_success_under_either_tree(
    store: EventStore, setting: Setting, seed: int
) -> None:
    sfp = _scenarios()["sfp"]

    fixed = store.events(run_unit(sfp, setting, seed, store).run_id)
    table = store.events(run_unit(sfp, setting, seed, store, tree=TablePlanner()).run_id)

    assert _skills(fixed) == _skills(table) == ["approach", "align", "insert_scripted", "verify"]
    assert _finished(fixed).outcome == _finished(table).outcome == "success"
    assert _finished(fixed).duration == _finished(table).duration


def test_no_planner_is_handed_the_sc_fault(store: EventStore) -> None:
    sc = _scenarios()["sc"]
    spy = SpyTable()

    planned = store.events(run_unit(sc, "C", 0, store, tree=spy).run_id)
    fixed = store.events(run_unit(sc, "C", 0, store).run_id)

    [(fault_id, kind, _, params)] = _faults(fixed)
    [handed] = spy.seen
    assert kind not in handed and fault_id not in handed
    assert all(str(value) not in handed for value in params.values())
    assert _faults(planned) == _faults(fixed)
    [call] = [e.payload for e in fixed if isinstance(e.payload, PlannerCalled)]
    context = json.dumps(call.context)
    # The UNKNOWN planner sees the readings (the torque it reads is the fault's torque, as a
    # sensor would show it), never the fault's kind, id or drawn yaw.
    assert kind not in context and fault_id not in context
    assert str(params["yaw_deg"]) not in context
