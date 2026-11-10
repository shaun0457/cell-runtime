"""Grouping recurring UNKNOWN verdicts by the evidence the Monitor recorded (spec step 1).

The signature reads only the evidence; the injected fault kinds are referee labels and are used
here only to check that a signature separates failures that need different handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from cellrt.eval.runner import run_eval
from cellrt.eval.scenarios import load_scenarios
from cellrt.promotion.groups import (
    SurveyedRun,
    SurveyTooLarge,
    band,
    gate_0,
    group_unknowns,
    render_survey,
    survey,
)
from cellrt.promotion.replay import ReplayConfig, RunReplay
from cellrt.runtime.store import EventStore

SCENARIOS = Path(__file__).resolve().parents[1] / "eval" / "scenarios"
SEEDS = 4

Runs = list[SurveyedRun]


@pytest.fixture(scope="module")
def phase3_store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("phase3") / "store.sqlite"
    with EventStore(path) as store:
        run_eval(load_scenarios(SCENARIOS / "phase3.yaml"), ["A", "B", "C"], SEEDS, store)
    return path


@pytest.fixture(scope="module")
def phase3_runs(phase3_store: Path) -> Runs:
    with EventStore(phase3_store) as store:
        runs = [store.events(r.run_id) for r in store.runs()]
    return survey(runs, ReplayConfig.mock())


def test_a_survey_keeps_every_run_it_replayed(phase3_runs: Runs) -> None:
    assert len(phase3_runs) == 3 * 3 * SEEDS  # scenarios x settings x seeds
    assert {r.replay.status for r in phase3_runs} == {"ok"}


def test_a_survey_stops_at_its_run_limit(phase3_store: Path) -> None:
    with EventStore(phase3_store) as store:
        runs = [store.events(r.run_id) for r in store.runs()[:3]]
    assert len(survey(runs, ReplayConfig.mock(), max_runs=3)) == 3
    with pytest.raises(SurveyTooLarge):
        survey(runs, ReplayConfig.mock(), max_runs=2)


def test_bands_name_the_interval_a_value_falls_in() -> None:
    edges = (10.0, 20.0)
    assert band(3.0, edges) == "<10"
    assert band(10.0, edges) == "10-20"
    assert band(19.99, edges) == "10-20"
    assert band(20.0, edges) == ">=20"
    assert band(None, edges) == "n/a"


def test_each_signature_holds_one_kind_of_injected_failure(phase3_runs: Runs) -> None:
    groups = group_unknowns(phase3_runs)
    assert groups
    for group in groups:
        assert len(group.injected) == 1, (group.signature, group.injected)


def test_the_rotated_plug_and_the_damaged_port_get_different_signatures(
    phase3_runs: Runs,
) -> None:
    groups = group_unknowns(phase3_runs)
    by_kind = {kind: g.signature for g in groups for kind in g.injected}
    rotated, damaged = by_kind["yaw_mismatch"], by_kind["damaged_port"]
    assert rotated != damaged
    assert (rotated.skill, rotated.rule, rotated.torque_band) == (
        "insert_scripted",
        "insert_stalled",
        ">=0.1",
    )
    assert damaged.torque_band == "<0.1"


def test_a_group_counts_every_verdict_and_every_run(phase3_runs: Runs) -> None:
    # yaw_mismatch stalls insert_scripted once per run in A, B and C (C then switches skill).
    [rotated] = [g for g in group_unknowns(phase3_runs) if "yaw_mismatch" in g.injected]
    assert len(rotated.members) == 3 * SEEDS
    assert len(rotated.runs) == 3 * SEEDS
    assert rotated.injected == {"yaw_mismatch": 3 * SEEDS}


def test_groups_come_largest_first(phase3_runs: Runs) -> None:
    sizes = [len(g.members) for g in group_unknowns(phase3_runs)]
    assert sizes == sorted(sizes, reverse=True)


def test_a_store_with_nothing_replayable_does_not_pass_gate_0() -> None:
    runs = [SurveyedRun(RunReplay("r1", "unfinished"), frozenset())]
    lines, passed = render_survey(runs, [], min_size=3)
    assert not passed
    assert lines == [
        "replayed 1 runs: ok 0, unfinished 1",
        "gate 0 not run: no run in the store could be replayed",
    ]


def test_gate_0_hands_back_its_summary_apart_from_its_line() -> None:
    # The validation report shows the summary on its own; it is not parsed back from the line.
    runs = [SurveyedRun(RunReplay("r1", "ok", timeouts=1), frozenset())]
    result = gate_0(runs)
    assert result.passed
    assert result.summary == (
        "every recorded verdict reproduced (0 verdicts in 1 runs; "
        "1 skill timeouts, decided by the clock)"
    )
    assert result.lines[-1] == f"gate 0 passed: {result.summary}"


def test_gate_0_counts_the_skill_timeouts_apart() -> None:
    runs = [
        SurveyedRun(RunReplay("r1", "ok", timeouts=2), frozenset()),
        SurveyedRun(RunReplay("r2", "ok", timeouts=1), frozenset()),
    ]
    lines, passed = render_survey(runs, [], min_size=3)
    assert passed
    assert lines[1] == (
        "gate 0 passed: every recorded verdict reproduced (0 verdicts in 2 runs; "
        "3 skill timeouts, decided by the clock)"
    )
