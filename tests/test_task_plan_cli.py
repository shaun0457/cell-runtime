"""CLI seam for task plans: `cellrt run | eval --tree` in a subprocess."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from cellrt.runtime.events import TaskPlanned
from cellrt.runtime.store import EventStore

REPO = Path(__file__).resolve().parent.parent
TASKS = "scenarios:\n  - id: sc\n    task:\n      plug_type: sc\n"


def _cellrt(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cellrt", *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=REPO,
    )


def test_run_with_the_table_tree_records_its_plan(tmp_path: Path) -> None:
    scenarios = tmp_path / "tasks.yaml"
    scenarios.write_text(TASKS, encoding="utf-8")
    db = tmp_path / "cli.sqlite"

    run = _cellrt("run", "--scenarios", str(scenarios), "--scenario", "sc", "--setting", "B",
                  "--tree", "table", "--db", str(db))  # fmt: skip

    assert run.returncode == 0, run.stderr
    run_id = run.stdout.strip().splitlines()[-1]
    with EventStore(db) as store:
        planned = store.events(run_id)[1].payload
    assert isinstance(planned, TaskPlanned)
    assert planned.steps == ["approach", "align", "insert_act", "verify"]


def test_eval_with_the_table_tree_says_so_beside_the_table(tmp_path: Path) -> None:
    scenarios = tmp_path / "tasks.yaml"
    scenarios.write_text(TASKS, encoding="utf-8")

    done = _cellrt("eval", "--scenarios", str(scenarios), "--settings", "A", "--seeds", "1",
                   "--k", "1", "--tree", "table", "--db", str(tmp_path / "e.sqlite"))  # fmt: skip

    assert done.returncode == 0, done.stderr
    assert "tree: table" in done.stderr


def test_an_unknown_tree_is_refused_before_any_run(tmp_path: Path) -> None:
    db = tmp_path / "cli.sqlite"

    for command in (("run", "--scenario", "baseline"), ("eval", "--seeds", "1")):
        done = _cellrt(*command, "--tree", "grown", "--db", str(db))

        assert done.returncode != 0
        assert "unknown tree 'grown'; known: fixed, table" in done.stderr
    assert not db.exists()
