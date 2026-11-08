"""Primary seam: run one (scenario, setting, seed) against a store and read the event log back."""

from __future__ import annotations

from cellrt.eval.runner import run_unit
from cellrt.eval.scenarios import Scenario
from cellrt.runtime.state import fold
from cellrt.runtime.store import EventStore


def test_baseline_run_in_setting_a_succeeds(store: EventStore, baseline: Scenario) -> None:
    result = run_unit(baseline, "A", seed=0, store=store)

    state = fold(store.events(result.run_id))
    assert state.status == "succeeded"
    assert result.outcome == "success"


def test_baseline_run_records_every_step(store: EventStore, baseline: Scenario) -> None:
    events = store.events(run_unit(baseline, "A", seed=0, store=store).run_id)
    types = [e.type for e in events]

    assert types[0] == "run_started"
    assert types[-1] == "run_finished"
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    skill_events = [
        (e.type, e.payload.skill) for e in events if e.type in ("skill_started", "skill_finished")
    ]
    assert skill_events == [
        ("skill_started", "approach"),
        ("skill_finished", "approach"),
        ("skill_started", "align"),
        ("skill_finished", "align"),
        ("skill_started", "insert_scripted"),
        ("skill_finished", "insert_scripted"),
        ("skill_started", "verify"),
        ("skill_finished", "verify"),
    ]
    assert all(e.payload.ok for e in events if e.type == "skill_finished")
    assert {e.payload.skill for e in events if e.type == "skill_progress"} == {
        "approach",
        "align",
        "insert_scripted",
        "verify",
    }


def test_one_signal_sample_per_world_step_in_simulated_time(
    store: EventStore, baseline: Scenario
) -> None:
    events = store.events(run_unit(baseline, "A", seed=0, store=store).run_id)
    signals = [e for e in events if e.type == "signal_sampled"]

    assert [s.payload.step for s in signals] == list(range(1, len(signals) + 1))
    assert [s.ts for s in signals] == [round(0.05 * n, 9) for n in range(1, len(signals) + 1)]
    finished = events[-1].payload
    assert finished.duration == signals[-1].ts
    # Simulated: a run of several seconds of cell time is recorded at 20 Hz.
    assert 2.0 < finished.duration < 20.0


def test_tree_status_changes_are_recorded(store: EventStore, baseline: Scenario) -> None:
    events = store.events(run_unit(baseline, "A", seed=0, store=store).run_id)
    changes = [(e.payload.node, e.payload.status) for e in events if e.type == "bt_node_changed"]

    assert ("setting_A", "RUNNING") in changes
    assert changes[-1] == ("setting_A", "SUCCESS")
    for skill in ("approach", "align", "insert_scripted", "verify"):
        assert (skill, "RUNNING") in changes
        assert (skill, "SUCCESS") in changes
