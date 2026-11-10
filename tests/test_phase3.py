"""Phase 3 scenarios (ADR 019): two UNKNOWN failures whose right answers differ.

`yaw_mismatch` is fixed by switching to insert_act. `damaged_port` is fixed by nothing, so the
right decision is to stop early. Tests go through `run_unit` and read the event log it leaves.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

import cellrt.cli
from cellrt.eval.faults import DamagedPortSpec
from cellrt.eval.runner import run_unit
from cellrt.eval.scenarios import Scenario, load_scenarios
from cellrt.executor.recovery import RecoveryConfig
from cellrt.monitor.classifier import MonitorConfig
from cellrt.planner.fixed_planner import ABORT_REASON, FixedPlanner
from cellrt.runtime.events import Event
from cellrt.runtime.store import EventStore
from cellrt.skills.mock import InsertAct

REPO = Path(__file__).resolve().parents[1]
PHASE0 = {s.id: s for s in load_scenarios(REPO / "eval" / "scenarios" / "phase0.yaml")}
PHASE3_PATH = REPO / "eval" / "scenarios" / "phase3.yaml"
PHASE3 = {s.id: s for s in load_scenarios(PHASE3_PATH)}
DAMAGED_PORT_RANGES = {
    "stall_depth_mm": (0.5, 1.5),
    "force_n": (2.0, 4.0),
    "torque_nm": (0.0, 0.05),
}


def _run(
    store: EventStore, scenario: Scenario, setting: str, seed: int = 0, **kwargs: object
) -> list[Event]:
    run = run_unit(scenario, setting, seed=seed, store=store, **kwargs)  # type: ignore[arg-type]
    return store.events(run.run_id)


def _of(events: list[Event], event_type: str) -> list[object]:
    return [e.payload for e in events if e.type == event_type]


def _verdicts(events: list[Event]) -> list[tuple[str, str, str]]:
    return [(v.skill, v.failure_class, v.rule) for v in _of(events, "monitor_verdict")]  # type: ignore[attr-defined]


def _recoveries(events: list[Event]) -> list[tuple[str, str, str]]:
    return [(r.skill, r.failure_class, r.action) for r in _of(events, "recovery_started")]  # type: ignore[attr-defined]


def _end(events: list[Event]) -> tuple[str, str]:
    return events[-1].payload.outcome, events[-1].payload.reason  # type: ignore[union-attr]


def test_the_phase3_file_holds_baseline_yaw_mismatch_and_damaged_port() -> None:
    assert list(PHASE3) == ["baseline", "yaw_mismatch", "damaged_port"]
    # Phase 0's baseline and yaw mismatch, ranges and all, so results compare across files.
    assert PHASE3["baseline"] == PHASE0["baseline"]
    assert PHASE3["yaw_mismatch"] == PHASE0["yaw_mismatch"]
    [spec] = PHASE3["damaged_port"].faults
    assert isinstance(spec, DamagedPortSpec)
    assert {name: getattr(spec, name) for name in DAMAGED_PORT_RANGES} == DAMAGED_PORT_RANGES


def test_the_damaged_port_ranges_stay_below_the_stuck_force_and_the_twist_torque() -> None:
    # Above 10 N the Monitor would call it INSERT_STUCK; above 0.1 Nm insert_act would twist,
    # as it does for a yaw mismatch. Either would change what the scenario tests.
    [spec] = PHASE3["damaged_port"].faults
    assert isinstance(spec, DamagedPortSpec)
    assert spec.force_n[1] < MonitorConfig().stuck_force_n
    assert spec.torque_nm[1] < InsertAct().torque_threshold_nm


def test_damaged_port_parameters_are_drawn_per_seed_within_their_ranges(
    store: EventStore,
) -> None:
    drawn = []
    for seed in range(6):
        [injected] = _of(_run(store, PHASE3["damaged_port"], "B", seed), "fault_injected")
        assert (injected.kind, injected.label) == ("damaged_port", "UNKNOWN")  # type: ignore[attr-defined]
        params = injected.params  # type: ignore[attr-defined]
        for name, (low, high) in DAMAGED_PORT_RANGES.items():
            assert low <= params[name] <= high
        drawn.append(tuple(params[name] for name in DAMAGED_PORT_RANGES))

    assert len(set(drawn)) == len(drawn)  # each seed draws its own values


@pytest.mark.parametrize("seed", range(4))
def test_a_damaged_port_reads_unknown_by_the_stall_rule_and_is_lost_in_b(
    store: EventStore, seed: int
) -> None:
    events = _run(store, PHASE3["damaged_port"], "B", seed)

    [verdict] = _of(events, "monitor_verdict")
    assert (verdict.skill, verdict.failure_class, verdict.rule) == (  # type: ignore[attr-defined]
        "insert_scripted",
        "UNKNOWN",
        "insert_stalled",
    )
    assert "also_matched" not in verdict.evidence  # type: ignore[attr-defined]
    assert verdict.evidence["force_n"] < MonitorConfig().stuck_force_n  # type: ignore[attr-defined]
    assert _end(events) == ("failed", "insert_scripted failed: UNKNOWN")
    assert _of(events, "planner_called") == []


def _deepest_mm(events: list[Event]) -> float:
    return max(s.depth_mm for s in _of(events, "signal_sampled"))  # type: ignore[attr-defined]


@pytest.mark.parametrize("seed", range(4))
def test_on_a_damaged_port_the_rule_planner_switches_then_retries_and_fails_on_its_budget(
    store: EventStore, seed: int
) -> None:
    events = _run(store, PHASE3["damaged_port"], "C", seed)

    called = [(p.skill, p.call) for p in _of(events, "planner_called")]  # type: ignore[attr-defined]
    assert called == [("insert_scripted", 1), ("insert_act", 2)]
    assert [p.decision for p in _of(events, "planner_decided")] == [  # type: ignore[attr-defined]
        {"kind": "switch_skill", "skill": "insert_act"},
        {"kind": "retry"},
    ]
    assert _recoveries(events) == [
        ("insert_scripted", "UNKNOWN", "switch_skill:insert_act"),
        ("insert_act", "UNKNOWN", "retry"),
    ]
    stalled = ("UNKNOWN", "insert_stalled")
    assert _verdicts(events) == [(s, *stalled) for s in ("insert_scripted", *["insert_act"] * 2)]
    assert _end(events) == ("failed", "insert_act failed: UNKNOWN (planner budget used up)")
    # Nothing freed the plug: it never went deeper than the stall depth.
    [injected] = _of(events, "fault_injected")
    assert _deepest_mm(events) <= injected.params["stall_depth_mm"] + 1e-9  # type: ignore[attr-defined]


@pytest.mark.parametrize("seed", range(4))
def test_on_the_phase3_yaw_mismatch_the_rule_planner_still_wins(
    store: EventStore, seed: int
) -> None:
    events = _run(store, PHASE3["yaw_mismatch"], "C", seed)

    assert _recoveries(events) == [("insert_scripted", "UNKNOWN", "switch_skill:insert_act")]
    assert _end(events)[0] == "success"


EDGES = {
    "low": {name: low for name, (low, _) in DAMAGED_PORT_RANGES.items()},
    "high": {name: high for name, (_, high) in DAMAGED_PORT_RANGES.items()},
}


@pytest.mark.parametrize("edge", EDGES)
def test_a_damaged_port_at_either_end_of_its_ranges_stalls_both_insert_skills(
    store: EventStore, edge: str
) -> None:
    values = EDGES[edge]
    scenario = Scenario.model_validate(
        {
            "id": f"damaged_port_{edge}",
            "faults": [{"kind": "damaged_port", **{k: [v, v] for k, v in values.items()}}],
        }
    )

    events = _run(store, scenario, "C")

    [injected] = _of(events, "fault_injected")
    assert injected.params == values  # type: ignore[attr-defined]
    stalled = ("UNKNOWN", "insert_stalled")
    assert _verdicts(events) == [(s, *stalled) for s in ("insert_scripted", *["insert_act"] * 2)]
    assert _deepest_mm(events) <= values["stall_depth_mm"] + 1e-9
    assert _end(events)[0] == "failed"


def _decisions(events: list[Event]) -> list[dict[str, object]]:
    return [p.decision for p in _of(events, "planner_decided")]  # type: ignore[attr-defined]


ABORTED = ("aborted", f"insert_scripted failed: UNKNOWN (planner aborted: {ABORT_REASON})")


@pytest.mark.parametrize("seed", range(4))
def test_on_a_damaged_port_the_fixed_planner_retries_once_then_aborts(
    store: EventStore, seed: int
) -> None:
    events = _run(store, PHASE3["damaged_port"], "C", seed, planner=FixedPlanner())

    called = [(p.skill, p.call) for p in _of(events, "planner_called")]  # type: ignore[attr-defined]
    assert called == [("insert_scripted", 1), ("insert_scripted", 2)]
    assert _decisions(events) == [{"kind": "retry"}, {"kind": "abort", "reason": ABORT_REASON}]
    assert _recoveries(events) == [("insert_scripted", "UNKNOWN", "retry")]
    started = [p.skill for p in _of(events, "skill_started")]  # type: ignore[attr-defined]
    assert "insert_act" not in started and started.count("insert_scripted") == 2
    assert _end(events) == ABORTED


def test_on_a_yaw_mismatch_the_fixed_planner_never_tries_the_skill_that_would_win(
    store: EventStore,
) -> None:
    events = _run(store, PHASE3["yaw_mismatch"], "C", planner=FixedPlanner())

    assert _decisions(events) == [{"kind": "retry"}, {"kind": "abort", "reason": ABORT_REASON}]
    assert _end(events) == ABORTED


@pytest.mark.parametrize(
    ("budget", "kinds"),
    [(1, ["abort"]), (2, ["retry", "abort"]), (3, ["retry", "retry", "abort"])],
)
def test_the_fixed_planner_retries_until_its_last_planner_call_and_aborts_on_it(
    store: EventStore, budget: int, kinds: list[str]
) -> None:
    recovery = RecoveryConfig(planner_budget=budget)

    events = _run(store, PHASE3["damaged_port"], "C", planner=FixedPlanner(), recovery=recovery)

    called = _of(events, "planner_called")
    assert [p.context["planner_calls_left"] for p in called] == list(range(budget, 0, -1))  # type: ignore[attr-defined]
    assert [d["kind"] for d in _decisions(events)] == kinds
    assert _end(events) == ABORTED  # it stops on its last call: never "planner budget used up"


@pytest.mark.parametrize(
    ("total_budget", "kinds"),
    [(1, ["abort"]), (2, ["retry", "abort"]), (4, ["retry", "retry", "abort"])],
)
def test_the_fixed_planner_stops_before_the_task_recovery_budget_runs_out(
    store: EventStore, total_budget: int, kinds: list[str]
) -> None:
    recovery = RecoveryConfig(total_budget=total_budget, planner_budget=3)

    events = _run(store, PHASE3["damaged_port"], "C", planner=FixedPlanner(), recovery=recovery)

    assert [d["kind"] for d in _decisions(events)] == kinds
    assert _end(events) == ABORTED  # never "task recovery budget used up"


def test_one_fixed_planner_serves_many_runs_with_the_same_answers(store: EventStore) -> None:
    planner = FixedPlanner()

    first = _run(store, PHASE3["damaged_port"], "C", planner=planner)
    again = _run(store, PHASE3["damaged_port"], "C", planner=planner)
    other_seed = _run(store, PHASE3["damaged_port"], "C", 1, planner=planner)

    assert _decisions(first) == _decisions(again) == _decisions(other_seed)
    assert _end(first) == _end(again) == _end(other_seed)


def _config_hash(events: list[Event]) -> str:
    return events[0].payload.config_hash  # type: ignore[union-attr]


def test_the_planner_type_keeps_rule_and_fixed_results_apart(store: EventStore) -> None:
    scenario = PHASE3["damaged_port"]

    rule = _run(store, scenario, "C")
    fixed = _run(store, scenario, "C", planner=FixedPlanner())
    fixed_again = _run(store, scenario, "C", planner=FixedPlanner())

    assert _config_hash(fixed) == _config_hash(fixed_again)
    assert _config_hash(rule) != _config_hash(fixed)


def _cli(*args: str) -> Result:
    result = CliRunner().invoke(cellrt.cli.app, list(args))
    # An exception other than the command's own exit would be a traceback for the user.
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    return result


def _all_output(result: Result) -> str:
    return result.output + getattr(result, "stderr", "")


@pytest.mark.parametrize(("planner", "c_success"), [("fixed", "33.3%"), ("rule", "66.7%")])
def test_eval_on_the_phase3_file_prints_the_table_for_either_planner(
    tmp_path: Path, planner: str, c_success: str
) -> None:
    result = _cli("eval", "--scenarios", str(PHASE3_PATH), "--settings", "B,C", "--seeds", "2",
                  "--k", "2", "--planner", planner, "--db", str(tmp_path / "e.sqlite"))  # fmt: skip

    assert result.exit_code == 0, _all_output(result)
    lines = result.stdout.splitlines()
    assert any(line.startswith("setting") and "pass^2" in line for line in lines)
    rows = {line.split()[0]: line.split() for line in lines if line[:2] in ("B ", "C ")}
    assert rows["B"][1] == rows["C"][1] == "6"  # 3 scenarios x 2 seeds
    assert rows["B"][2] == "33.3%"  # only the baseline succeeds
    # The rule planner also wins the yaw mismatch; the fixed policy wins neither UNKNOWN.
    assert rows["C"][2] == c_success
    assert f"planner for setting C: {planner}" in _all_output(result)


@pytest.mark.parametrize(
    ("setting", "planner", "outcome"),
    [
        ("C", ["--planner", "fixed"], "aborted"),
        ("C", ["--planner", "rule"], "failed"),
        ("C", [], "failed"),  # the rule planner by default
        ("B", ["--planner", "fixed"], "failed"),  # only setting C asks a planner
    ],
)
def test_run_asks_the_chosen_planner_in_setting_c(
    tmp_path: Path, setting: str, planner: list[str], outcome: str
) -> None:
    result = _cli("run", "--scenarios", str(PHASE3_PATH), "--scenario", "damaged_port",
                  "--setting", setting, *planner, "--db", str(tmp_path / "r.sqlite"))  # fmt: skip

    assert result.exit_code == 0, _all_output(result)
    assert result.stdout.splitlines()[0] == f"outcome: {outcome}"


@pytest.mark.parametrize("command", ["eval", "run"])
def test_an_unknown_planner_is_refused_before_anything_runs(tmp_path: Path, command: str) -> None:
    db = tmp_path / "x.sqlite"
    run_args = ["--scenario", "damaged_port", "--setting", "C"] if command == "run" else []

    result = _cli(command, "--scenarios", str(PHASE3_PATH), "--planner", "oracle",
                  "--db", str(db), *run_args)  # fmt: skip

    assert result.exit_code == 1
    assert "unknown planner 'oracle'; known: rule, fixed" in _all_output(result)
    assert not db.exists()
