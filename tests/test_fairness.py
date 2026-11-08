"""Phase 0 acceptance: settings A, B and C are compared fairly, as read from the event log.

Same problem (identical faults, identical run up to the first failure), same judge (one Monitor
configuration), and every extra success explained by a recovery or a planner call.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pytest

from cellrt.eval.metrics import compute_metrics
from cellrt.eval.runner import run_eval
from cellrt.eval.scenarios import load_scenarios
from cellrt.runtime.events import Event, RunStarted
from cellrt.runtime.store import EventStore

PHASE0 = Path(__file__).resolve().parent.parent / "eval" / "scenarios" / "phase0.yaml"
SEEDS = 3
# What the world did and what the Monitor saw: the same in every setting until one recovers.
WORLD_AND_MONITOR = {
    "signal_sampled", "fault_injected", "fault_cleared", "command_dropped", "skill_started",
    "skill_progress", "skill_finished", "skill_cancelled", "precondition_failed",
    "monitor_verdict",
}  # fmt: skip
# Configuration parts that define the problem and the judge, not the recovery.
SHARED_PARTS = ("scenario", "world", "monitor", "skills", "code")

Logs = dict[tuple[str, int], dict[str, list[Event]]]  # (scenario, seed) -> setting -> events


@pytest.fixture(scope="module")
def logs(tmp_path_factory: pytest.TempPathFactory) -> Logs:
    db = tmp_path_factory.mktemp("fairness") / "e.sqlite"
    by_unit: Logs = defaultdict(dict)
    with EventStore(db) as store:
        runs = run_eval(load_scenarios(PHASE0), ["A", "B", "C"], SEEDS, store)
        for run_id in runs.run_ids:
            events = store.events(run_id)
            started = events[0].payload
            assert isinstance(started, RunStarted)
            by_unit[(started.scenario_id, started.seed)][started.setting] = events
    return dict(by_unit)


def _until_first_verdict(events: list[Event]) -> list[tuple[str, float, dict[str, object]]]:
    """The world-and-Monitor events up to and including the first verdict (all if none)."""
    prefix = []
    for event in events:
        if event.type in WORLD_AND_MONITOR:
            prefix.append((event.type, event.ts, event.payload.model_dump()))
        if event.type == "monitor_verdict":
            break
    return prefix


def _faults_drawn(events: list[Event]) -> list[dict[str, object]]:
    return [e.payload.model_dump() for e in events if e.type == "fault_injected"]


def test_every_setting_faces_the_same_faults_and_runs_the_same_until_its_first_failure(
    logs: Logs,
) -> None:
    assert len(logs) == len(load_scenarios(PHASE0)) * SEEDS
    for unit, by_setting in logs.items():
        a, b, c = (by_setting[s] for s in "ABC")
        if unit[0] != "baseline":
            assert _faults_drawn(a), unit  # the fault was injected before A gave up
        assert _faults_drawn(a) == _faults_drawn(b) == _faults_drawn(c), unit
        assert _until_first_verdict(a) == _until_first_verdict(b) == _until_first_verdict(c), unit


def test_every_setting_is_judged_by_the_same_monitor_and_differs_only_in_recovery(
    logs: Logs,
) -> None:
    for unit, by_setting in logs.items():
        parts = {s: events[0].payload.config_parts for s, events in by_setting.items()}
        for name in SHARED_PARTS:
            assert parts["A"][name] == parts["B"][name] == parts["C"][name], (unit, name)
        assert parts["A"]["recovery"] != parts["B"]["recovery"] == parts["C"]["recovery"], unit
        assert parts["A"]["planner"] == parts["B"]["planner"] != parts["C"]["planner"], unit


def test_every_win_over_a_shows_the_recovery_or_planner_call_that_made_it(logs: Logs) -> None:
    table = compute_metrics((e for by_setting in logs.values() for e in by_setting.values()), 5)

    # How many runs B and C win is an observation, not a pass condition: only require that
    # there are gains to check, and that each one is explained.
    assert {g.setting for g in table.gains} == {"B", "C"}
    assert sum(g.won for g in table.gains) > 0
    assert all(g.unexplained == () for g in table.gains)
