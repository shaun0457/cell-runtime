"""`cellrt promote approve` and `cellrt eval --promoted` (ADR 021, ticket T3), end to end over a
store the phase 3 eval left: validate a candidate, approve its report, run the eval with it."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from cellrt.eval.runner import run_eval
from cellrt.eval.scenarios import load_scenarios
from cellrt.monitor.promoted import Candidate
from cellrt.promotion.adopt import Adopted
from cellrt.runtime.events import PlannerCalled, RunFinished
from cellrt.runtime.store import EventStore

REPO = Path(__file__).resolve().parents[1]
PHASE3 = REPO / "eval" / "scenarios" / "phase3.yaml"
ROTATED = "insert_scripted/insert_stalled force <10 N torque >=0.1 Nm"
RULE: dict[str, Any] = {
    "name": "PLUG_ROTATED",
    "skill": "insert_scripted",
    "window_s": 0.5,
    "conditions": [
        {"field": "torque_nm", "op": ">=", "value": 0.3},
        {"field": "progress_mm", "op": "<", "value": 0.2},
    ],
}


def _cellrt(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cellrt", *args],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        cwd=REPO,
    )


def _report(store: Path, tmp_path: Path, **rule: Any) -> Path:
    """The report `promote validate --out` writes for the rotated plug's candidate."""
    candidate = Candidate.parse(
        json.dumps(
            {"rule": {**RULE, **rule}, "plan": {"action": "switch_skill", "skill": "insert_act"}}
        )
    )
    path, out = tmp_path / "candidate.json", tmp_path / "report.json"
    path.write_text(candidate.canonical(), encoding="utf-8")
    _cellrt(
        "promote", "validate", str(path), "--group", ROTATED, "--db", str(store), "--out", str(out)
    )
    assert out.is_file()
    return out


@pytest.fixture(scope="module")
def phase3_store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("approve") / "phase3.sqlite"
    with EventStore(path) as store:
        run_eval(load_scenarios(PHASE3), ["A", "B", "C"], 3, store)
    return path


@pytest.fixture(scope="module")
def passed_report(phase3_store: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    report = _report(phase3_store, tmp_path_factory.mktemp("report"))
    assert json.loads(report.read_text(encoding="utf-8"))["passed"] is True
    return report


def _approve(
    report: Path, directory: Path, by: str = "Shaun Chen"
) -> subprocess.CompletedProcess[str]:
    return _cellrt("promote", "approve", str(report), "--by", by, "--dir", str(directory))


def test_an_approved_rule_is_written_under_its_name(passed_report: Path, tmp_path: Path) -> None:
    done = _approve(passed_report, tmp_path / "promoted")
    assert done.returncode == 0, done.stderr
    path = tmp_path / "promoted" / "PLUG_ROTATED.json"
    adopted = Adopted.model_validate_json(path.read_text(encoding="utf-8"))
    assert adopted.approved_by == "Shaun Chen"
    assert done.stdout.splitlines() == [
        f"approved PLUG_ROTATED ({adopted.candidate_hash}) by Shaun Chen",
        str(path),
    ]


def test_an_approved_rule_is_not_replaced(passed_report: Path, tmp_path: Path) -> None:
    assert _approve(passed_report, tmp_path).returncode == 0
    again = _approve(passed_report, tmp_path, by="Someone Else")
    assert again.returncode == 1
    assert "approval refused: already_adopted" in again.stderr
    assert "Shaun Chen" in (tmp_path / "PLUG_ROTATED.json").read_text(encoding="utf-8")


def test_a_rule_that_overlaps_an_approved_one_is_refused(
    passed_report: Path, phase3_store: Path, tmp_path: Path
) -> None:
    directory = tmp_path / "promoted"
    assert _approve(passed_report, directory).returncode == 0
    twin = _report(phase3_store, tmp_path, name="PLUG_TWISTED")  # the same conditions
    assert json.loads(twin.read_text(encoding="utf-8"))["passed"] is True
    done = _approve(twin, directory)
    assert done.returncode == 1
    assert "approval refused: rules_overlap" in done.stderr
    assert not (directory / "PLUG_TWISTED.json").exists()


def test_a_broken_file_already_in_the_directory_is_named_as_the_cause(
    passed_report: Path, tmp_path: Path
) -> None:
    # An approved file gone stale (the Monitor changed since) refuses the next approval; the
    # message points at the directory, not at the new report.
    directory = tmp_path / "promoted"
    assert _approve(passed_report, directory).returncode == 0
    path = directory / "PLUG_ROTATED.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["config_parts"]["monitor"] = "0" * 16
    path.write_text(json.dumps(data), encoding="utf-8")
    done = _approve(passed_report, directory)
    assert done.returncode == 1
    assert f"approved rules in {directory}: approval refused: config_stale" in done.stderr


def test_a_report_with_a_failed_gate_is_refused(phase3_store: Path, tmp_path: Path) -> None:
    report = _report(phase3_store, tmp_path, window_s=2.0)  # misses every window: gate 1 fails
    done = _approve(report, tmp_path / "promoted")
    assert done.returncode == 1
    assert "approval refused: not_passed" in done.stderr
    assert not (tmp_path / "promoted").exists()


def test_an_approver_must_be_a_plain_name(passed_report: Path, tmp_path: Path) -> None:
    done = _approve(passed_report, tmp_path, by="<script>")
    assert done.returncode == 1
    assert "approval refused: approver_invalid" in done.stderr
    assert "<script>" not in done.stderr


def test_an_unreadable_report_is_refused_by_type(tmp_path: Path) -> None:
    done = _approve(tmp_path / "absent.json", tmp_path)
    assert done.returncode == 1
    assert "cannot read the report" in done.stderr
    assert "FileNotFoundError" in done.stderr


# `cellrt eval --promoted`.


def _eval(db: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return _cellrt(
        "eval", "--scenarios", str(PHASE3), "--seeds", "2", "--k", "1", "--settings", "B,C",
        "--db", str(db), *extra,
    )  # fmt: skip


def test_the_eval_runs_the_approved_plan_without_the_planner(
    passed_report: Path, tmp_path: Path
) -> None:
    assert _approve(passed_report, tmp_path / "promoted").returncode == 0
    db = tmp_path / "after.sqlite"
    done = _eval(db, "--promoted", str(tmp_path / "promoted"))
    assert done.returncode == 0, done.stderr
    assert "promoted rules: PLUG_ROTATED" in done.stderr
    with EventStore(db) as store:
        runs = {r.run_id: store.events(r.run_id) for r in store.runs()}
    asked = {
        rid
        for rid, events in runs.items()
        if any(isinstance(e.payload, PlannerCalled) for e in events)
    }
    outcome = {rid: events[-1].payload.outcome for rid, events in runs.items()}  # type: ignore[union-attr]
    assert all(isinstance(events[-1].payload, RunFinished) for events in runs.values())
    for setting in "BC":
        for seed in range(2):
            rid = f"yaw_mismatch/{setting}/{seed}/a1"
            assert outcome[rid] == "success" and rid not in asked
    # The damaged port is not the rule's group: in C the planner still handles it.
    assert {rid for rid in asked} == {"damaged_port/C/0/a1", "damaged_port/C/1/a1"}


def test_a_missing_promoted_directory_is_refused_before_the_store_opens(tmp_path: Path) -> None:
    db = tmp_path / "none.sqlite"
    done = _eval(db, "--promoted", str(tmp_path / "absent"))
    assert done.returncode == 1
    assert "approval refused: no_directory" in done.stderr
    assert not db.exists()


def test_an_empty_promoted_directory_changes_nothing(tmp_path: Path) -> None:
    (tmp_path / "promoted").mkdir()
    without = _eval(tmp_path / "without.sqlite")
    empty = _eval(tmp_path / "empty.sqlite", "--promoted", str(tmp_path / "promoted"))
    assert without.returncode == empty.returncode == 0
    assert "promoted rules: none" in empty.stderr
    assert empty.stdout == without.stdout
