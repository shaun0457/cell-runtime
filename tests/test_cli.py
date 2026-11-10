"""CLI seam: `cellrt` in a subprocess, the way a person runs it."""

from __future__ import annotations

import json
import math
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from cellrt.runtime.events import Event, SkillStarted
from cellrt.runtime.store import EventStore

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

    for path, message in ((missing, "cannot read"), (empty, "invalid")):
        run = _cellrt("run", "--scenarios", str(path), "--scenario", "baseline",
                      "--db", str(tmp_path / "x.sqlite"))  # fmt: skip

        assert run.returncode == 1
        assert f"{message} scenarios file {path}" in run.stderr
        assert "Traceback" not in run.stderr


def test_replay_of_unknown_run_fails_clearly(tmp_path: Path) -> None:
    replay = _cellrt("replay", "nope/A/0/a1", "--db", str(tmp_path / "empty.sqlite"))

    assert replay.returncode != 0
    assert "no events for run nope/A/0/a1" in replay.stderr


@pytest.mark.parametrize("command", ["replay", "trace"])
def test_a_database_that_is_not_an_event_store_is_refused_and_left_alone(
    tmp_path: Path, command: str
) -> None:
    db = tmp_path / "other.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE notes (text TEXT)")
    conn.close()
    before = db.read_bytes()

    done = _cellrt(command, "r/A/0/a1", "--db", str(db))

    assert done.returncode == 1
    assert done.stderr == f"cannot read the event store at {db} (OperationalError)\n"
    assert db.read_bytes() == before


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


def _names(trace: dict[str, Any], ph: str) -> list[str]:
    return [e["name"] for e in trace["traceEvents"] if e["ph"] == ph]


def test_trace_writes_a_stored_run_as_trace_json(tmp_path: Path) -> None:
    db = str(tmp_path / "cli.sqlite")
    out = tmp_path / "traces" / "yaw.json"
    run = _cellrt("run", "--scenarios", str(SCENARIOS), "--scenario", "yaw_mismatch",
                  "--setting", "C", "--seed", "0", "--db", db)  # fmt: skip
    assert run.returncode == 0, run.stderr
    run_id = run.stdout.strip().splitlines()[-1]

    to_file = _cellrt("trace", run_id, "--db", db, "--out", str(out))
    to_stdout = _cellrt("trace", run_id, "--db", db, "--clock", "sim")

    assert to_file.returncode == 0, to_file.stderr
    text = out.read_text(encoding="utf-8")
    trace = json.loads(text)
    assert trace["otherData"]["run_id"] == run_id
    assert trace["otherData"]["clock"] == "wall"
    spans = _names(trace, "X")
    assert spans[0] == run_id
    assert {"insert_scripted", "insert_act", "planner call 1"} <= set(spans)
    assert "monitor_verdict: UNKNOWN" in _names(trace, "i")
    # Only the log goes in: no paths, no environment. JSON escapes a Windows path's backslashes.
    assert str(tmp_path) not in text and json.dumps(str(tmp_path))[1:-1] not in text
    assert to_stdout.returncode == 0, to_stdout.stderr
    sim = json.loads(to_stdout.stdout)
    assert sim["otherData"]["clock"] == "sim"
    [run_span] = [e for e in sim["traceEvents"] if e["ph"] == "X" and e["name"] == run_id]
    assert run_span["dur"] == 10_050_000


def test_trace_of_unknown_run_fails_clearly(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite"
    db = tmp_path / "cli.sqlite"
    with EventStore(db):
        pass

    for store in (missing, db):
        trace = _cellrt("trace", "nope/A/0/a1", "--db", str(store))

        assert trace.returncode == 1
        assert "no events for run nope/A/0/a1" in trace.stderr
        assert trace.stdout == ""
        assert "Traceback" not in trace.stderr
    assert not missing.exists()


def test_trace_of_a_run_with_an_infinite_time_fails_clearly(tmp_path: Path) -> None:
    db = tmp_path / "cli.sqlite"
    out = tmp_path / "trace.json"
    with EventStore(db) as store:
        payload = SkillStarted(skill="approach", attempt=1)
        store.append(Event(run_id="r/A/0/a1", seq=1, ts=0.0, wall_ts=math.inf, payload=payload))

    trace = _cellrt("trace", "r/A/0/a1", "--db", str(db), "--out", str(out))

    assert trace.returncode == 1
    assert "run r/A/0/a1 has a time that is not a finite number" in trace.stderr
    assert "Traceback" not in trace.stderr
    assert not out.exists()


def test_trace_never_writes_over_the_event_store(tmp_path: Path) -> None:
    db = tmp_path / "cli.sqlite"
    with EventStore(db) as store:
        payload = SkillStarted(skill="approach", attempt=1)
        store.append(Event(run_id="r/A/0/a1", seq=1, ts=0.0, wall_ts=1.0, payload=payload))

    trace = _cellrt("trace", "r/A/0/a1", "--db", str(db), "--out", str(db))

    assert trace.returncode == 1
    assert "--out is the event store itself; no trace written" in trace.stderr
    with EventStore(db) as store:
        assert len(store.events("r/A/0/a1")) == 1
