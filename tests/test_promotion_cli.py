"""`cellrt promote group` over stores the mock eval left (ticket T1)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from cellrt.eval.runner import run_eval, run_unit
from cellrt.eval.scenarios import load_scenarios
from cellrt.monitor.classifier import MonitorConfig
from cellrt.runtime.store import EventStore

REPO = Path(__file__).resolve().parents[1]
PHASE3 = REPO / "eval" / "scenarios" / "phase3.yaml"


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cellrt", *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=REPO,
    )


@pytest.fixture(scope="module")
def phase3_store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("cli") / "phase3.sqlite"
    with EventStore(path) as store:
        run_eval(load_scenarios(PHASE3), ["A", "B", "C"], 2, store)
    return path


def test_group_prints_gate_0_and_the_recurring_unknowns(phase3_store: Path) -> None:
    out = _cli("promote", "group", "--db", str(phase3_store))
    assert out.returncode == 0, out.stderr
    lines = out.stdout.splitlines()
    assert "replayed 18 runs: ok 18" in lines[0]
    assert lines[1].startswith("gate 0 passed: every recorded verdict reproduced")
    rotated = next(line for line in lines if "torque >=0.1 Nm" in line)
    assert "6 verdicts in 6 runs" in rotated
    assert "insert_scripted/insert_stalled force <10 N" in rotated
    assert "referee: yaw_mismatch 6" in rotated


def test_groups_below_the_minimum_are_listed_apart(phase3_store: Path) -> None:
    out = _cli("promote", "group", "--db", str(phase3_store), "--min-size", "7")
    assert out.returncode == 0, out.stderr
    below = out.stdout.split("too few", 1)[1]
    assert "torque >=0.1 Nm" in below  # 6 < 7


def test_a_store_made_with_other_thresholds_is_refused(tmp_path: Path) -> None:
    [scenario] = [s for s in load_scenarios(PHASE3) if s.id == "damaged_port"]
    path = tmp_path / "other.sqlite"
    with EventStore(path) as store:
        run_unit(scenario, "A", 0, store, monitor=MonitorConfig(stall_window_s=0.8))
    out = _cli("promote", "group", "--db", str(path))
    assert out.returncode == 1
    assert "gate 0 refused" in out.stdout
    assert "monitor" in out.stdout
    assert "UNKNOWN groups" not in out.stdout


def test_a_missing_store_is_not_created(tmp_path: Path) -> None:
    path = tmp_path / "none.sqlite"
    out = _cli("promote", "group", "--db", str(path))
    assert out.returncode == 1
    assert "no event store" in out.stderr
    assert not path.exists()


@pytest.mark.parametrize("value", ["0", "-1", "100001"])
def test_min_size_is_bounded(phase3_store: Path, value: str) -> None:
    out = _cli("promote", "group", "--db", str(phase3_store), "--min-size", value)
    assert out.returncode != 0
