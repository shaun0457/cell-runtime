"""Behavioural regression gate: a small mock eval compared with a committed baseline.

The baseline file names its own eval (scenarios file, settings, seeds, k), so checking it needs
no other input; a mismatch exits non-zero and names every differing cell.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

import cellrt.cli
from cellrt.eval.baseline import BASELINE_TOLERANCE, BaselineConfig, BaselineFile, compare

REPO = Path(__file__).resolve().parent.parent
SCENARIOS = REPO / "eval" / "scenarios" / "phase0.yaml"
COMMITTED = REPO / "eval" / "baselines" / "phase0-small.json"


def _cellrt(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cellrt", *args],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        cwd=REPO,
    )


TABLE = {
    "k": 2,
    "rows": {"A": {"success_rate": 0.5, "pass_k": None, "runs": 4}},
    "confusion": {"FORCE_LIMIT|FORCE_LIMIT": 2},
}


def test_equal_tables_match() -> None:
    assert compare(TABLE, json.loads(json.dumps(TABLE))) == []


def test_a_difference_within_the_tolerance_matches_and_one_just_over_does_not() -> None:
    within = json.loads(json.dumps(TABLE))
    within["rows"]["A"]["success_rate"] = 0.5 + BASELINE_TOLERANCE
    over = json.loads(json.dumps(TABLE))
    just_over = math.nextafter(0.5 + BASELINE_TOLERANCE, 1.0)
    over["rows"]["A"]["success_rate"] = just_over

    assert compare(TABLE, within) == []
    assert compare(TABLE, over) == [f"rows.A.success_rate: expected 0.5, got {just_over}"]


def test_a_count_that_changes_type_differs() -> None:
    expected = {"runs": 21, "valid": True, "rate": None}
    actual = {"runs": 21.0, "valid": 1, "rate": 0.0}

    assert compare(expected, actual) == [
        "rate: expected None, got 0.0",
        "runs: expected 21, got 21.0",
        "valid: expected True, got 1",
    ]


def test_counts_none_and_missing_cells_must_match_exactly() -> None:
    changed = json.loads(json.dumps(TABLE))
    changed["rows"]["A"]["runs"] = 5
    changed["rows"]["A"]["pass_k"] = 0.25
    del changed["confusion"]["FORCE_LIMIT|FORCE_LIMIT"]
    changed["confusion"]["FORCE_LIMIT|MISSED"] = 2

    assert compare(TABLE, changed) == [
        "confusion.FORCE_LIMIT|FORCE_LIMIT: expected 2, got <missing>",
        "confusion.FORCE_LIMIT|MISSED: expected <missing>, got 2",
        "rows.A.pass_k: expected None, got 0.25",
        "rows.A.runs: expected 4, got 5",
    ]


def test_a_written_baseline_checks_clean_and_a_changed_one_fails_naming_the_cell(
    tmp_path: Path,
) -> None:
    path = tmp_path / "small.json"
    written = _cellrt("baseline", str(path), "--write", "--scenarios", "eval/scenarios/phase0.yaml",
                      "--settings", "A", "--seeds", "1", "--k", "1")  # fmt: skip
    assert written.returncode == 0, written.stderr

    clean = _cellrt("baseline", str(path))
    assert clean.returncode == 0, clean.stderr

    baseline = json.loads(path.read_text(encoding="utf-8"))
    baseline["table"]["rows"]["A"]["runs"] += 1
    path.write_text(json.dumps(baseline), encoding="utf-8")
    drifted = _cellrt("baseline", str(path))

    assert drifted.returncode == 1
    assert "rows.A.runs: expected" in drifted.stderr


def test_a_malformed_baseline_file_fails_with_locations_only(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            {
                "config": {
                    "scenarios": "x.yaml",
                    "settings": ["A"],
                    "seeds": 10**9,
                    "k": "secret-value",
                },
                "table": [1],
            }
        ),
        encoding="utf-8",
    )
    bad = _cellrt("baseline", str(path))

    assert bad.returncode == 1
    assert "config.seeds: less_than_equal" in bad.stderr and "table: dict_type" in bad.stderr
    assert "Traceback" not in bad.stderr and "secret-value" not in bad.stderr


def test_writing_a_baseline_refuses_an_absolute_scenarios_path(tmp_path: Path) -> None:
    written = _cellrt("baseline", str(tmp_path / "x.json"), "--write", "--scenarios",
                      str(SCENARIOS))  # fmt: skip

    assert written.returncode == 1 and "relative" in written.stderr


def test_the_committed_baseline_matches_the_current_code() -> None:
    check = _cellrt("baseline", str(COMMITTED))

    assert check.returncode == 0, check.stderr


def _write_in_process(
    monkeypatch: pytest.MonkeyPatch, path: Path, table: dict[str, object]
) -> tuple[int, str]:
    """`cellrt baseline --write` with the eval replaced by a fixed table (no 8 s run)."""
    monkeypatch.setattr(cellrt.cli, "_baseline_table", lambda config: table)
    result = CliRunner().invoke(cellrt.cli.app, ["baseline", str(path), "--write"])
    # An exception other than the command's own exit is the raw traceback this must not give.
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    return result.exit_code, result.output + getattr(result, "stderr", "")


def test_a_baseline_that_cannot_be_written_fails_with_the_error_type_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "is-a-directory.json"
    target.mkdir()

    code, output = _write_in_process(monkeypatch, target, {"k": 2})

    assert code == 1
    assert f"cannot write baseline file {target} (" in output and "Traceback" not in output
    assert sorted(p.name for p in tmp_path.iterdir()) == ["is-a-directory.json"]


def test_a_failed_write_keeps_the_old_baseline_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "small.json"
    target.write_text('{"old": true}\n', encoding="utf-8")

    def fail_replace(src: object, dst: object) -> None:
        raise PermissionError("locked by another process")

    monkeypatch.setattr(cellrt.cli.os, "replace", fail_replace)
    code, output = _write_in_process(monkeypatch, target, {"k": 2})

    assert code == 1 and "(PermissionError)" in output
    assert "locked by another process" not in output
    assert target.read_text(encoding="utf-8") == '{"old": true}\n'
    assert sorted(p.name for p in tmp_path.iterdir()) == ["small.json"]


def test_a_table_with_a_non_finite_number_is_not_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "small.json"

    code, output = _write_in_process(monkeypatch, target, {"rows": {"A": {"mean": math.nan}}})

    assert code == 1 and "non-finite" in output
    assert not target.exists()


def test_the_write_defaults_are_the_committed_baselines_config() -> None:
    committed = BaselineFile.model_validate_json(COMMITTED.read_bytes()).config
    defaults = BaselineConfig(
        scenarios=cellrt.cli.DEFAULT_SCENARIOS.as_posix(),
        settings=list(cellrt.cli.SETTINGS),
        seeds=cellrt.cli.BASELINE_SEEDS,
        k=cellrt.cli.BASELINE_K,
    )

    assert committed == defaults
