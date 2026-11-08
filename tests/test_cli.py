"""CLI seam: `cellrt` in a subprocess, the way a person runs it."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCENARIOS = REPO / "eval" / "scenarios" / "phase0.yaml"


def _cellrt(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cellrt", *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=REPO,
    )


def test_run_then_replay_baseline(tmp_path: Path) -> None:
    db = str(tmp_path / "cli.sqlite")

    run = _cellrt("run", "--scenarios", str(SCENARIOS), "--scenario", "baseline",
                  "--setting", "A", "--seed", "0", "--db", db)  # fmt: skip

    assert run.returncode == 0, run.stderr
    run_id = run.stdout.strip().splitlines()[-1]
    assert run_id == "baseline/A/0/a1"

    replay = _cellrt("replay", run_id, "--db", db)

    assert replay.returncode == 0, replay.stderr
    lines = replay.stdout.strip().splitlines()
    assert lines[0].split()[:3] == ["1", "0.000", "run_started"]
    assert "run_finished" in lines[-1]
    assert "success" in lines[-1]
    assert any("skill_finished" in line and "insert_scripted" in line for line in lines)
    assert not any("signal_sampled" in line for line in lines)

    with_signals = _cellrt("replay", run_id, "--db", db, "--signals")

    assert with_signals.returncode == 0, with_signals.stderr
    assert any("signal_sampled" in line for line in with_signals.stdout.splitlines())


def test_run_with_unreadable_scenarios_file_fails_clearly(tmp_path: Path) -> None:
    missing = tmp_path / "missing.yaml"
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")

    for path in (missing, empty):
        run = _cellrt("run", "--scenarios", str(path), "--scenario", "baseline",
                      "--db", str(tmp_path / "x.sqlite"))  # fmt: skip

        assert run.returncode == 1
        assert f"cannot read scenarios file {path}" in run.stderr
        assert "Traceback" not in run.stderr


def test_replay_of_unknown_run_fails_clearly(tmp_path: Path) -> None:
    replay = _cellrt("replay", "nope/A/0/a1", "--db", str(tmp_path / "empty.sqlite"))

    assert replay.returncode != 0
    assert "no events for run nope/A/0/a1" in replay.stderr


def test_a_setting_b_run_shows_its_recovery_in_the_replay(tmp_path: Path) -> None:
    db = str(tmp_path / "cli.sqlite")

    run = _cellrt("run", "--scenarios", str(SCENARIOS), "--scenario", "pose_shift",
                  "--setting", "B", "--seed", "0", "--db", db)  # fmt: skip

    assert run.returncode == 0, run.stderr
    replay = _cellrt("replay", run.stdout.strip().splitlines()[-1], "--db", db)

    assert replay.returncode == 0, replay.stderr
    recoveries = [line for line in replay.stdout.splitlines() if "recovery_started" in line]
    assert recoveries[0].split()[3:] == [
        "insert_scripted", "POSE_ERROR", "->", "realign", "(class", "1,", "total", "1)"
    ]  # fmt: skip
