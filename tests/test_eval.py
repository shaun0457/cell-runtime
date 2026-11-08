"""`cellrt eval`: scenarios x settings x seeds, then a metrics table read from the event log."""

from __future__ import annotations

import subprocess
import sys
import typing
from pathlib import Path

from cellrt.cli import MAX_SEEDS, MAX_SHOWN_ERRORS
from cellrt.eval.faults import FaultSpec
from cellrt.eval.metrics import summarize_run
from cellrt.eval.runner import run_eval, run_unit
from cellrt.eval.scenarios import load_scenarios
from cellrt.monitor.classifier import MonitorConfig
from cellrt.runtime.store import EventStore

REPO = Path(__file__).resolve().parent.parent
PHASE0 = REPO / "eval" / "scenarios" / "phase0.yaml"

TWO_SCENARIOS = """\
scenarios:
  - id: baseline
  - id: yaw_mismatch
    faults:
      - kind: yaw_mismatch
        stall_depth_mm: [3.0, 5.0]
        force_n: [6.0, 9.0]
        torque_nm: [0.4, 0.8]
        yaw_deg: [5.0, 15.0]
"""


def _cellrt(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cellrt", *args],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        cwd=REPO,
    )


def test_the_phase0_file_has_a_baseline_and_one_scenario_per_fault_kind() -> None:
    scenarios = load_scenarios(PHASE0)
    union = typing.get_args(typing.get_args(FaultSpec)[0])
    all_kinds = {typing.get_args(spec.model_fields["kind"].annotation)[0] for spec in union}

    ids = [s.id for s in scenarios]
    assert ids[0] == "baseline" and scenarios[0].faults == []
    per_scenario = [[f.kind for f in s.faults] for s in scenarios[1:]]
    assert all(len(kinds) == 1 for kinds in per_scenario)
    assert sorted(k[0] for k in per_scenario) == sorted(all_kinds)


def test_run_eval_runs_every_scenario_setting_and_seed_in_order(tmp_path: Path) -> None:
    path = tmp_path / "two.yaml"
    path.write_text(TWO_SCENARIOS, encoding="utf-8")

    with EventStore(tmp_path / "e.sqlite") as store:
        run_ids = run_eval(load_scenarios(path), ["A", "C"], seeds=2, store=store).run_ids

    assert run_ids == [
        "baseline/A/0/a1", "baseline/A/1/a1", "baseline/C/0/a1", "baseline/C/1/a1",
        "yaw_mismatch/A/0/a1", "yaw_mismatch/A/1/a1",
        "yaw_mismatch/C/0/a1", "yaw_mismatch/C/1/a1",
    ]  # fmt: skip


def test_eval_prints_one_row_per_setting_and_a_confusion_matrix(tmp_path: Path) -> None:
    path = tmp_path / "two.yaml"
    path.write_text(TWO_SCENARIOS, encoding="utf-8")

    # A repeated setting runs once: its runs would otherwise count twice.
    result = _cellrt("eval", "--scenarios", str(path), "--settings", "A,B,C,A", "--seeds", "2",
                     "--db", str(tmp_path / "e.sqlite"))  # fmt: skip

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    header = next(line for line in lines if line.startswith("setting"))
    assert "pass^5" in header  # --k defaults to 5
    rows = {line.split()[0]: line.split() for line in lines if line[:2] in ("A ", "B ", "C ")}
    assert set(rows) == {"A", "B", "C"}
    assert rows["A"][1] == "4"  # runs: 2 scenarios x 2 seeds
    assert rows["A"][2] == "50.0%"  # baseline succeeds, the yaw mismatch fails without a planner
    assert rows["C"][2] == "100.0%"
    assert rows["A"][3] == "-"  # pass^5 is undefined with 2 runs per scenario
    assert any(line.startswith("truth") for line in lines)
    # C wins both yaw mismatch seeds that A loses, each through a planner call.
    assert "  B: 0 won, 0 unexplained" in lines
    assert "  C: 2 won, 0 unexplained" in lines
    notes = " ".join(line for line in lines if line.startswith("note:"))
    assert "fault persistence" in notes and "budgets" in notes and "speed" in notes
    assert "not the value of recovery" in notes


def test_eval_rejects_an_invalid_scenarios_file_with_where_it_is_wrong(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("scenarios:\n  - id: x\n    faults:\n      - kind: meteor\n", encoding="utf-8")

    result = _cellrt("eval", "--scenarios", str(path), "--db", str(tmp_path / "e.sqlite"))

    assert result.returncode == 1
    assert f"invalid scenarios file {path}" in result.stderr
    assert "scenarios.0.faults.0" in result.stderr
    assert "Traceback" not in result.stderr


def test_eval_rejects_a_scenarios_file_that_is_not_utf8(tmp_path: Path) -> None:
    path = tmp_path / "utf16.yaml"
    path.write_text("scenarios:\n  - id: baseline\n", encoding="utf-16")  # Notepad's "Unicode"

    result = _cellrt("eval", "--scenarios", str(path), "--db", str(tmp_path / "e.sqlite"))

    assert result.returncode == 1
    assert f"cannot read scenarios file {path} (UnicodeDecodeError)" in result.stderr
    assert "Traceback" not in result.stderr


def test_eval_shows_only_the_first_few_problems_of_an_invalid_file(tmp_path: Path) -> None:
    bad = MAX_SHOWN_ERRORS + 2
    path = tmp_path / "bad.yaml"
    path.write_text(
        "scenarios:\n" + "".join(f"  - id: s{i}\n    colour: red\n" for i in range(bad)),
        encoding="utf-8",
    )

    result = _cellrt("eval", "--scenarios", str(path), "--db", str(tmp_path / "e.sqlite"))

    shown = [line for line in result.stderr.splitlines() if "extra_forbidden" in line]
    assert result.returncode == 1
    assert len(shown) == MAX_SHOWN_ERRORS
    assert "... and 2 more" in result.stderr
    assert "red" not in result.stderr  # where it is wrong, never what the file says


def test_eval_rejects_bad_options(tmp_path: Path) -> None:
    db = str(tmp_path / "e.sqlite")

    unknown = _cellrt("eval", "--settings", "A,X", "--db", db)
    no_seeds = _cellrt("eval", "--seeds", "0", "--db", db)
    no_k = _cellrt("eval", "--k", "0", "--db", db)

    too_many = _cellrt("eval", "--seeds", str(MAX_SEEDS + 1), "--db", db)

    assert unknown.returncode == 1 and "unknown setting 'X'" in unknown.stderr
    # 2 is a usage error, raised while parsing options, before anything runs. (The message is
    # not checked: it may be coloured, which splits the option name with escape codes.)
    assert no_seeds.returncode == 2
    assert no_k.returncode == 2
    assert too_many.returncode == 2
    assert not Path(db).exists()  # nothing ran


def test_unsafe_force_is_counted_even_when_the_monitor_misses_it(tmp_path: Path) -> None:
    tight = next(s for s in load_scenarios(PHASE0) if s.id == "tight_fit")
    # A Monitor that never acts on force: its trigger is beyond any run.
    blind = MonitorConfig(force_budget_s=1e9, force_trigger_s=1e9)

    with EventStore(tmp_path / "e.sqlite") as store:
        run = run_unit(tight, "A", 0, store, monitor=blind)
        summary = summarize_run(store.events(run.run_id))

    assert summary is not None
    assert all(v.predicted != "FORCE_LIMIT" for v in summary.verdicts)
    assert summary.over_limit_s > 1.0
    assert summary.unsafe
