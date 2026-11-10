"""Validating a candidate rule by replay (ADR 022, ticket T2): gate 0 first, then one failing
candidate per gate, over a store the phase 0 eval left (it has clean runs, known classes and the
rotated plug)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from cellrt.eval.runner import run_eval, run_unit
from cellrt.eval.scenarios import load_scenarios
from cellrt.monitor.classifier import MonitorConfig
from cellrt.monitor.promoted import Candidate, CandidateRefused
from cellrt.promotion.groups import SurveyTooLarge
from cellrt.promotion.replay import ReplayConfig
from cellrt.promotion.validate import (
    MAX_CASES,
    NOT_CHECKED,
    GroupNotFound,
    Report,
    check_registry,
    validate,
)
from cellrt.runtime.events import (
    SCHEMA_VERSION,
    Event,
    FaultInjected,
    MonitorVerdict,
    Payload,
    RunFinished,
    RunStarted,
    SignalSampled,
    SkillCancelled,
    SkillFinished,
    SkillStarted,
)
from cellrt.runtime.store import EventStore
from cellrt.runtime.task import Task
from cellrt.skills.mock import mock_registry

REPO = Path(__file__).resolve().parents[1]
PHASE0 = REPO / "eval" / "scenarios" / "phase0.yaml"
SEEDS = 4
ROTATED = "insert_scripted/insert_stalled force <10 N torque >=0.1 Nm"

Runs = list[list[Event]]


def _candidate(plan: dict[str, Any] | None = None, **rule: Any) -> Candidate:
    data: dict[str, Any] = {
        "name": "PLUG_ROTATED",
        "skill": "insert_scripted",
        "window_s": 0.5,
        "conditions": [
            {"field": "torque_nm", "op": ">=", "value": 0.3},
            {"field": "progress_mm", "op": "<", "value": 0.2},
        ],
    }
    data.update(rule)
    plan = plan or {"action": "switch_skill", "skill": "insert_act"}
    return Candidate.parse(json.dumps({"rule": data, "plan": plan}))


@pytest.fixture(scope="module")
def phase0_store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("validate") / "phase0.sqlite"
    with EventStore(path) as store:
        run_eval(load_scenarios(PHASE0), ["A", "B", "C"], SEEDS, store)
    return path


@pytest.fixture(scope="module")
def phase0_runs(phase0_store: Path) -> Runs:
    with EventStore(phase0_store) as store:
        return [store.events(r.run_id) for r in store.runs()]


def _validate(runs: Runs, candidate: Candidate, **kw: Any) -> Report:
    return validate(runs, candidate, ROTATED, ReplayConfig.mock(), **kw)


def _gate(report: Report, number: int):  # type: ignore[no-untyped-def]
    [gate] = [g for g in report.gates if g.number == number]
    return gate


def test_a_rule_for_the_rotated_plug_passes_every_gate(phase0_runs: Runs) -> None:
    report = _validate(phase0_runs, _candidate())
    assert [g.passed for g in report.gates] == [True, True, True, True]
    assert report.passed
    assert _gate(report, 1).summary.startswith(
        f"first verdict in {3 * SEEDS} of {3 * SEEDS} windows of the group, a median "
    )
    assert len(report.runs) == len(phase0_runs)


def test_a_rule_that_misses_part_of_its_group_fails_gate_1(phase0_runs: Runs) -> None:
    # The stall torque is 0.47-0.70 N m across seeds: a 0.6 threshold catches only some.
    report = _validate(
        phase0_runs,
        _candidate(conditions=[{"field": "torque_nm", "op": ">=", "value": 0.6}]),
    )
    gate = _gate(report, 1)
    assert gate.passed is False
    assert 0 < gate.failed < 3 * SEEDS
    assert gate.cases[0].recorded == "UNKNOWN/insert_stalled"
    assert gate.cases[0].replayed == "UNKNOWN/insert_stalled"
    assert not report.passed


def test_a_rule_that_waits_longer_than_the_stall_rule_never_comes_first(
    phase0_runs: Runs,
) -> None:
    report = _validate(phase0_runs, _candidate(window_s=2.0))
    assert _gate(report, 1).failed == 3 * SEEDS


def test_a_rule_that_fires_on_a_clean_run_fails_gate_2(phase0_runs: Runs) -> None:
    # Any insertion descending slower than 5 mm over 0.2 s: the clean insertion too.
    candidate = _candidate(
        window_s=0.2, conditions=[{"field": "progress_mm", "op": "<", "value": 5.0}]
    )
    gate = _gate(_validate(phase0_runs, candidate), 2)
    assert gate.passed is False
    assert gate.summary == (
        f"first verdict in {3 * SEEDS} of {3 * SEEDS} runs without an injected fault"
    )
    assert gate.cases[0].run_id.startswith("baseline/")
    assert gate.cases[0].recorded == "none"
    assert gate.cases[0].replayed == "UNKNOWN/promoted:PLUG_ROTATED"
    assert len(gate.cases) == MAX_CASES < gate.failed


@pytest.mark.parametrize(
    "conditions, taken",
    [
        # The obstacle: about 10 N with no progress, stuck after 0.5 s; this fires after 0.2 s.
        (
            [
                {"field": "force_n", "op": ">", "value": 9.5},
                {"field": "progress_mm", "op": "<", "value": 0.2},
            ],
            "INSERT_STUCK/stuck_under_force",
        ),
        # The tight fit: over 20 N for 0.5 s is a force-limit verdict; this fires at 15 N.
        ([{"field": "force_n", "op": ">", "value": 15.0}], "FORCE_LIMIT/force_over_limit"),
    ],
)
def test_a_rule_that_takes_a_known_class_fails_gate_3(
    phase0_runs: Runs, conditions: list[dict[str, Any]], taken: str
) -> None:
    gate = _gate(_validate(phase0_runs, _candidate(window_s=0.2, conditions=conditions)), 3)
    assert gate.passed is False
    assert taken in {c.recorded for c in gate.cases}
    assert {c.replayed for c in gate.cases} == {"UNKNOWN/promoted:PLUG_ROTATED"}


def test_a_store_made_with_other_thresholds_fails_gate_0_and_nothing_else_runs(
    tmp_path: Path,
) -> None:
    [scenario] = [s for s in load_scenarios(PHASE0) if s.id == "yaw_mismatch"]
    with EventStore(tmp_path / "other.sqlite") as store:
        run_unit(scenario, "A", 0, store, monitor=MonitorConfig(stall_window_s=0.8))
        runs = [store.events(r.run_id) for r in store.runs()]
    report = _validate(runs, _candidate())
    assert [(g.number, g.passed) for g in report.gates] == [
        (0, False),
        (1, None),
        (2, None),
        (3, None),
    ]
    assert "monitor" in report.gates[0].summary
    assert not report.passed


def test_a_store_without_clean_runs_cannot_pass_gate_2(phase0_runs: Runs) -> None:
    faulted = [r for r in phase0_runs if any(isinstance(e.payload, FaultInjected) for e in r)]
    gate = _gate(_validate(faulted, _candidate()), 2)
    assert gate.passed is False
    assert "false alarms cannot be checked" in gate.summary


def test_a_group_under_the_minimum_size_fails_gate_1(phase0_runs: Runs) -> None:
    gate = _gate(_validate(phase0_runs, _candidate(), min_size=3 * SEEDS + 1), 1)
    assert gate.passed is False
    assert gate.summary == f"the group has {3 * SEEDS} windows, under {3 * SEEDS + 1}"


def test_a_validation_stops_at_its_run_limit(phase0_runs: Runs) -> None:
    with pytest.raises(SurveyTooLarge):
        _validate(phase0_runs[:3], _candidate(), max_runs=2)


def test_a_group_the_store_does_not_have_is_refused(phase0_runs: Runs) -> None:
    with pytest.raises(GroupNotFound):
        validate(phase0_runs, _candidate(), "verify/insert_stalled", ReplayConfig.mock())


def test_the_report_says_replay_checks_the_rule_not_the_plan(phase0_runs: Runs) -> None:
    report = _validate(phase0_runs, _candidate())
    data = report.as_dict()
    assert data["not_checked"] == NOT_CHECKED
    assert "not the plan" in NOT_CHECKED
    assert report.lines()[-1] == f"note: {NOT_CHECKED}"
    assert data["candidate_hash"] == _candidate().digest()
    assert data["config_parts"] == dict(ReplayConfig.mock().parts)
    assert [g["passed"] for g in data["gates"]] == [True, True, True, True]


@pytest.mark.parametrize(
    "rule, plan, code",
    [
        ({"skill": "insert_fancy"}, None, "rule.skill: not_registered"),
        ({"skill": "approach"}, None, "rule.skill: not_an_insertion_skill"),
        ({}, {"action": "switch_skill", "skill": "insert_fancy"}, "plan.skill: not_registered"),
        ({}, {"action": "switch_skill", "skill": "verify"}, "plan.skill: not_an_insertion_skill"),
        (
            {},
            {"action": "switch_skill", "skill": "insert_scripted"},
            "plan.skill: same_as_rule_skill",
        ),
    ],
)
def test_a_candidate_must_name_registered_insertion_skills(
    rule: dict[str, Any], plan: dict[str, Any] | None, code: str
) -> None:
    with pytest.raises(CandidateRefused, match=code):
        check_registry(_candidate(plan, **rule), mock_registry())


@pytest.mark.parametrize("plan", [{"action": "retry"}, {"action": "abort"}, {"action": "escalate"}])
def test_plans_without_a_skill_need_no_registry_check(plan: dict[str, Any]) -> None:
    check_registry(_candidate(plan), mock_registry())


# Hand-built runs: a plug pressing at 3 N without moving.


def _sample(step: int) -> SignalSampled:
    return SignalSampled(
        step=step,
        height_mm=-2.0,
        depth_mm=2.0,
        xy_offset_mm=(0.0, 0.0),
        xy_error_mm=0.0,
        raw_force_n=(0.0, 0.0, 3.0),
        tare_offset_n=(0.0, 0.0, 0.0),
        force_n=3.0,
        torque_nm=0.0,
        wrench_valid=True,
    )


def _hand_run(run_id: str, payloads: list[Payload], *, fault: bool = False) -> list[Event]:
    """`run_started`, a `fault_injected` if `fault`, `payloads` (a sample every 0.05 s,
    anything else with the sample before it), `run_finished`."""
    if fault:
        payloads = [FaultInjected(fault_id="f1", kind="hand", label="UNKNOWN"), *payloads]
    started = RunStarted(
        scenario_id="hand",
        setting="A",
        seed=0,
        attempt=1,
        task=Task(time_limit=30.0),
        config_hash="x",
        config_parts=dict(ReplayConfig.mock().parts),
        schema_version=SCHEMA_VERSION,
    )
    timed: list[tuple[float, Payload]] = [(0.0, started)]
    t = 0.0
    for payload in payloads:
        if isinstance(payload, SignalSampled):
            t = round(t + 0.05, 6)
        timed.append((t, payload))
    timed.append((t, RunFinished(outcome="failed", reason="x", steps=1, duration=t)))
    return [
        Event(run_id=run_id, seq=i, ts=ts, wall_ts=0.0, payload=p)
        for i, (ts, p) in enumerate(timed, start=1)
    ]


HAND_GROUP = "insert_scripted/insert_stalled force <10 N torque <0.1 Nm"


def _pressing_report(*, faulted_stalls: bool) -> Report:
    """Three runs stall (the group); a fourth, without a fault, succeeds on the very tick the
    candidate would fire, so its verdict there would have lost to the success."""
    stall = MonitorVerdict(
        skill="insert_scripted",
        failure_class="UNKNOWN",
        rule="insert_stalled",
        evidence={
            "progress_mm": 0.0,
            "window_s": 1.0,
            "force_n": 3.0,
            "torque_nm": 0.0,
            "xy_error_mm": 0.0,
        },
    )
    start = SkillStarted(skill="insert_scripted", attempt=1)
    stalled = [
        _hand_run(
            f"stall/A/{i}/a1",
            [
                start,
                *[_sample(k) for k in range(1, 22)],
                stall,
                SkillCancelled(skill="insert_scripted", reason="preempted"),
            ],
            fault=faulted_stalls,
        )
        for i in range(3)
    ]
    succeeds = _hand_run(
        "clean/A/0/a1",
        [
            start,
            *[_sample(k) for k in range(1, 6)],
            SkillFinished(skill="insert_scripted", ok=True),
        ],
    )
    pressing = _candidate(
        window_s=0.2,
        conditions=[
            {"field": "force_n", "op": ">", "value": 2.0},
            {"field": "progress_mm", "op": "<", "value": 0.2},
        ],
    )
    return validate([*stalled, succeeds], pressing, HAND_GROUP, ReplayConfig.mock())


def test_a_candidate_that_fires_only_as_the_skill_succeeds_changes_nothing() -> None:
    report = _pressing_report(faulted_stalls=True)
    assert report.passed, report.lines()
    assert _gate(report, 1).summary == (
        "first verdict in 3 of 3 windows of the group, a median 0.80 s before the recorded UNKNOWN"
    )
    assert _gate(report, 2).summary == "first verdict in 0 of 1 runs without an injected fault"


def test_a_group_from_runs_without_a_fault_fails_gate_2() -> None:
    # Without a fault there is no referee to say the stall was not a false alarm (ADR 015).
    report = _pressing_report(faulted_stalls=False)
    gate = _gate(report, 2)
    assert gate.passed is False
    assert gate.summary == "first verdict in 3 of 4 runs without an injected fault"
    assert [c.run_id for c in gate.cases] == [f"stall/A/{i}/a1" for i in range(3)]
    assert _gate(report, 1).passed is True
    assert report.lines()[-2] == "failed: gate 2"


def test_gates_that_did_not_run_are_not_listed_as_failed(tmp_path: Path) -> None:
    [scenario] = [s for s in load_scenarios(PHASE0) if s.id == "yaw_mismatch"]
    with EventStore(tmp_path / "other.sqlite") as store:
        run_unit(scenario, "A", 0, store, monitor=MonitorConfig(stall_window_s=0.8))
        runs = [store.events(r.run_id) for r in store.runs()]
    assert _validate(runs, _candidate()).lines()[-2] == "failed: gate 0"


# The CLI over the same store.


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cellrt", "promote", "validate", *args],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        cwd=REPO,
    )


def _write(tmp_path: Path, candidate: Candidate) -> Path:
    path = tmp_path / "candidate.json"
    path.write_text(candidate.canonical(), encoding="utf-8")
    return path


def test_validate_prints_the_gates_and_writes_the_report(
    phase0_store: Path, tmp_path: Path
) -> None:
    out = tmp_path / "report.json"
    done = _cli(
        str(_write(tmp_path, _candidate())),
        "--group",
        ROTATED,
        "--db",
        str(phase0_store),
        "--out",
        str(out),
    )
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert lines[0] == f"candidate PLUG_ROTATED ({_candidate().digest()}) for {ROTATED}"
    assert [line.split(":")[0] for line in lines if line.startswith("gate ")] == [
        "gate 0 passed",
        "gate 1 passed",
        "gate 2 passed",
        "gate 3 passed",
    ]
    assert lines[-2:] == ["passed", f"note: {NOT_CHECKED}"]
    assert json.loads(out.read_text(encoding="utf-8"))["passed"] is True


@pytest.mark.parametrize("target", ["event store", "candidate"])
def test_the_report_is_never_written_over_what_it_was_made_from(
    phase0_store: Path, tmp_path: Path, target: str
) -> None:
    db = tmp_path / "copy.sqlite"  # a copy: a write over the module's store would end the module
    shutil.copyfile(phase0_store, db)
    candidate = _write(tmp_path, _candidate())
    out = db if target == "event store" else candidate
    before = out.read_bytes()
    done = _cli(str(candidate), "--group", ROTATED, "--db", str(db), "--out", str(out))
    assert done.returncode == 1
    assert f"--out is the {target} itself; no report written" in done.stderr
    assert out.read_bytes() == before


def test_a_report_that_cannot_be_written_leaves_the_old_one_whole(
    phase0_store: Path, tmp_path: Path
) -> None:
    out = tmp_path / "report.json"
    out.write_text("old report\n", encoding="utf-8")
    (tmp_path / "report.json.tmp").mkdir()  # the temporary file cannot be created
    done = _cli(
        str(_write(tmp_path, _candidate())),
        "--group",
        ROTATED,
        "--db",
        str(phase0_store),
        "--out",
        str(out),
    )
    assert done.returncode == 1
    assert "cannot write the report" in done.stderr
    assert out.read_text(encoding="utf-8") == "old report\n"


def test_a_failing_candidate_exits_1_with_its_cases(phase0_store: Path, tmp_path: Path) -> None:
    candidate = _candidate(window_s=2.0)
    done = _cli(str(_write(tmp_path, candidate)), "--group", ROTATED, "--db", str(phase0_store))
    assert done.returncode == 1
    assert "gate 1 failed: first verdict in 0 of 12 windows of the group" in done.stdout
    assert "failed: gate 1" in done.stdout


def test_a_candidate_outside_the_language_is_refused_by_location(
    phase0_store: Path, tmp_path: Path
) -> None:
    path = tmp_path / "candidate.json"
    secret = "sk-not-a-real-key-123"
    path.write_text(json.dumps({"rule": {"name": secret}, "plan": {}}), encoding="utf-8")
    done = _cli(str(path), "--group", ROTATED, "--db", str(phase0_store))
    assert done.returncode == 1
    assert "candidate refused: rule.name" in done.stderr
    assert secret not in done.stdout + done.stderr


def test_an_unknown_group_is_refused(phase0_store: Path, tmp_path: Path) -> None:
    path = _write(tmp_path, _candidate())
    done = _cli(str(path), "--group", "verify/none", "--db", str(phase0_store))
    assert done.returncode == 1
    assert "no UNKNOWN group with that label" in done.stderr


def test_a_missing_store_is_not_created(tmp_path: Path) -> None:
    db = tmp_path / "none.sqlite"
    done = _cli(str(_write(tmp_path, _candidate())), "--group", ROTATED, "--db", str(db))
    assert done.returncode == 1
    assert "no event store" in done.stderr
    assert not db.exists()


def test_an_unreadable_candidate_is_refused_by_type(phase0_store: Path, tmp_path: Path) -> None:
    done = _cli(str(tmp_path / "absent.json"), "--group", ROTATED, "--db", str(phase0_store))
    assert done.returncode == 1
    assert "cannot read the candidate" in done.stderr
    assert "FileNotFoundError" in done.stderr
