"""A resumable eval: rerunning skips finished units and reruns interrupted ones as a new attempt.

The unit of work is one (scenario, setting, seed). It is complete when an attempt has
`run_finished` under the same configuration; an interrupted attempt is never resumed half-way,
and nothing is deleted from the log.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from cellrt.eval import runner
from cellrt.eval.runner import (
    MAX_ATTEMPTS,
    adapters_fingerprint,
    code_fingerprint,
    lock_fingerprint,
    run_eval,
    run_unit,
)
from cellrt.eval.scenarios import Scenario, load_scenarios
from cellrt.runtime.events import SCHEMA_VERSION, Event, RunStarted, SkillStarted
from cellrt.runtime.store import CRASH_AFTER_EVENTS_ENV, CRASH_EXIT_CODE, RUNS_SQL, EventStore
from cellrt.runtime.task import Task
from cellrt.world.mock_world import MockParams

REPO = Path(__file__).resolve().parent.parent

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


@pytest.fixture
def scenarios(tmp_path: Path) -> list[Scenario]:
    path = tmp_path / "two.yaml"
    path.write_text(TWO_SCENARIOS, encoding="utf-8")
    return load_scenarios(path)


def _interrupted(store: EventStore, unit: str, attempt: int, config_hash: str) -> None:
    """What a kill leaves behind: the attempt started, never finished."""
    scenario, setting, seed = unit.split("/")
    started = RunStarted(scenario_id=scenario, setting=setting, seed=int(seed), attempt=attempt,
                         task=Task(), config_hash=config_hash,
                         schema_version=SCHEMA_VERSION)  # fmt: skip
    for seq, payload in enumerate([started, SkillStarted(skill="approach", attempt=1)], start=1):
        store.append(
            Event(run_id=f"{unit}/a{attempt}", seq=seq, ts=0.0, wall_ts=0.0, payload=payload)
        )


# --- in process -----------------------------------------------------------------------------


def test_a_second_eval_skips_every_finished_unit(
    store: EventStore, scenarios: list[Scenario]
) -> None:
    first = run_eval(scenarios, ["A", "C"], 2, store)
    second = run_eval(scenarios, ["A", "C"], 2, store)

    assert (first.ran, first.skipped) == (8, 0)
    assert (second.ran, second.skipped) == (0, 8)
    assert second.run_ids == first.run_ids
    assert all(r.endswith("/a1") for r in store.run_ids())


def test_a_setting_given_twice_runs_once(store: EventStore, scenarios: list[Scenario]) -> None:
    result = run_eval(scenarios[:1], ["A", "A"], 1, store)

    assert result.run_ids == ["baseline/A/0/a1"] and result.ran == 1


def test_an_interrupted_unit_is_rerun_from_the_start_as_the_next_attempt(
    store: EventStore, scenarios: list[Scenario]
) -> None:
    _interrupted(store, "baseline/A/1", 1, "whatever")

    result = run_eval(scenarios[:1], ["A"], 2, store)

    assert result.run_ids == ["baseline/A/0/a1", "baseline/A/1/a2"]
    assert (result.ran, result.skipped) == (2, 0)
    # The interrupted attempt is still in the log, untouched.
    assert [e.type for e in store.events("baseline/A/1/a1")] == ["run_started", "skill_started"]


def test_the_eval_numbers_attempts_from_the_runs_it_read_once(
    store: EventStore, scenarios: list[Scenario], monkeypatch: pytest.MonkeyPatch
) -> None:
    _interrupted(store, "baseline/A/1", 1, "whatever")
    _interrupted(store, "baseline/A/1", 2, "whatever")

    # A count per unit would scan every run_started for each unit: n^2 over an eval.
    def no_scan(*args: object) -> int:
        raise AssertionError("run_eval must not query the store per unit")

    monkeypatch.setattr(EventStore, "next_attempt", no_scan)
    result = run_eval(scenarios[:1], ["A"], 2, store)

    assert result.run_ids == ["baseline/A/0/a1", "baseline/A/1/a3"]


def test_a_unit_finished_under_another_configuration_is_run_again(
    store: EventStore, scenarios: list[Scenario]
) -> None:
    run_eval(scenarios[:1], ["A"], 1, store)

    changed = run_eval(scenarios[:1], ["A"], 1, store, params=MockParams(max_twist_deg_s=40.0))
    again = run_eval(scenarios[:1], ["A"], 1, store, params=MockParams(max_twist_deg_s=40.0))

    assert changed.run_ids == ["baseline/A/0/a2"] and changed.ran == 1
    assert again.run_ids == ["baseline/A/0/a2"] and again.skipped == 1


def test_a_unit_whose_code_changed_is_run_again(
    store: EventStore, scenarios: list[Scenario], monkeypatch: pytest.MonkeyPatch
) -> None:
    run_eval(scenarios[:1], ["A"], 1, store)
    monkeypatch.setattr(runner, "code_fingerprint", lambda: "changed-code")

    changed = run_eval(scenarios[:1], ["A"], 1, store)

    assert changed.run_ids == ["baseline/A/0/a2"] and changed.ran == 1


def test_the_code_fingerprint_follows_source_files_only(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    source = tmp_path / "pkg" / "mod.py"
    source.write_bytes(b"x = 1\n")
    before = code_fingerprint.__wrapped__(tmp_path)

    (tmp_path / "pkg" / "__pycache__").mkdir()
    (tmp_path / "pkg" / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"bytecode")
    (tmp_path / "notes.txt").write_text("not code", encoding="utf-8")
    unrelated = code_fingerprint.__wrapped__(tmp_path)
    source.write_bytes(b"x = 1\r\n")  # the same file checked out with Windows line endings
    crlf = code_fingerprint.__wrapped__(tmp_path)
    source.write_bytes(b"x = 2\n")
    edited = code_fingerprint.__wrapped__(tmp_path)

    assert before == unrelated == crlf
    assert edited != before


def test_a_unit_whose_lock_file_changed_is_run_again(
    store: EventStore, scenarios: list[Scenario], monkeypatch: pytest.MonkeyPatch
) -> None:
    run_eval(scenarios[:1], ["A"], 1, store)
    monkeypatch.setattr(runner, "lock_fingerprint", lambda: "upgraded-dependencies")

    changed = run_eval(scenarios[:1], ["A"], 1, store)

    assert changed.run_ids == ["baseline/A/0/a2"] and changed.ran == 1


def test_a_unit_whose_adapter_code_changed_is_run_again(
    store: EventStore, scenarios: list[Scenario], monkeypatch: pytest.MonkeyPatch
) -> None:
    run_eval(scenarios[:1], ["A"], 1, store)
    monkeypatch.setattr(runner, "adapters_fingerprint", lambda: "changed-adapter")

    changed = run_eval(scenarios[:1], ["A"], 1, store)

    assert changed.run_ids == ["baseline/A/0/a2"] and changed.ran == 1


def test_the_lock_fingerprint_follows_the_lock_file_contents(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    missing = lock_fingerprint.__wrapped__(lock)
    lock.write_bytes(b'version = 1\n[[package]]\nname = "pydantic"\nversion = "2.7.0"\n')
    before = lock_fingerprint.__wrapped__(lock)
    lock.write_bytes(lock.read_bytes().replace(b"\n", b"\r\n"))  # Windows line endings
    crlf = lock_fingerprint.__wrapped__(lock)
    lock.write_bytes(b'version = 1\n[[package]]\nname = "pydantic"\nversion = "2.8.0"\n')
    upgraded = lock_fingerprint.__wrapped__(lock)

    assert before == crlf
    assert upgraded != before
    assert missing not in (before, upgraded)


def test_the_adapters_fingerprint_follows_adapter_source_files(tmp_path: Path) -> None:
    adapters = tmp_path / "adapters"
    missing = adapters_fingerprint.__wrapped__(adapters)
    (adapters / "aic").mkdir(parents=True)
    source = adapters / "aic" / "world.py"
    source.write_bytes(b"x = 1\n")
    before = adapters_fingerprint.__wrapped__(adapters)
    source.write_bytes(b"x = 2\n")
    edited = adapters_fingerprint.__wrapped__(adapters)

    assert edited != before
    assert missing not in (before, edited)


def test_the_lock_file_and_the_adapters_are_parts_of_the_config_hash(
    store: EventStore, scenarios: list[Scenario]
) -> None:
    done = run_unit(scenarios[0], "A", 0, store)
    parts = store.events(done.run_id)[0].payload.config_parts

    assert {"code", "lock", "adapters"} <= parts.keys()


def test_in_a_checkout_the_fingerprints_read_the_repo_lock_file_and_adapters_folder() -> None:
    assert runner.LOCK_FILE == REPO / "uv.lock" and runner.ADAPTERS_ROOT == REPO / "adapters"
    assert lock_fingerprint() == lock_fingerprint.__wrapped__(REPO / "uv.lock")
    assert lock_fingerprint() != lock_fingerprint.__wrapped__(REPO / "missing.lock")


def test_an_unchanged_checkout_gives_the_same_fingerprints_in_two_processes() -> None:
    script = (
        "from cellrt.eval.runner import adapters_fingerprint, code_fingerprint, lock_fingerprint;"
        "print(code_fingerprint(), lock_fingerprint(), adapters_fingerprint())"
    )
    outputs = [
        subprocess.run(
            [sys.executable, "-c", script], cwd=REPO, check=True, capture_output=True, text=True
        ).stdout
        for _ in range(2)
    ]

    assert outputs[0] == outputs[1] and len(outputs[0].split()) == 3


def test_a_unit_interrupted_max_attempts_times_is_given_up(
    store: EventStore, scenarios: list[Scenario]
) -> None:
    done = run_unit(scenarios[0], "A", 0, store)
    current = store.events(done.run_id)[0].payload.config_hash
    for attempt in range(1, MAX_ATTEMPTS + 1):
        _interrupted(store, "baseline/A/1", attempt, current)
    for attempt in range(1, MAX_ATTEMPTS):  # one short of the limit: still tried
        _interrupted(store, "baseline/A/2", attempt, current)

    result = run_eval(scenarios[:1], ["A"], 3, store)

    assert result.gave_up == ["baseline/A/1"]
    assert result.run_ids == ["baseline/A/0/a1", f"baseline/A/2/a{MAX_ATTEMPTS}"]
    assert f"baseline/A/1/a{MAX_ATTEMPTS + 1}" not in store.run_ids()


def test_listing_runs_builds_the_set_of_finished_runs_once(tmp_path: Path) -> None:
    with EventStore(tmp_path / "e.sqlite"):
        pass
    with sqlite3.connect(tmp_path / "e.sqlite") as conn:
        plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + RUNS_SQL)]

    # Not a lookup per started run (which grows as the square of the number of runs).
    assert any("LIST SUBQUERY" in step for step in plan)
    assert not any("EXISTS" in step or "CORRELATED" in step for step in plan)


def test_of_several_finished_attempts_the_first_one_counts(
    store: EventStore, scenarios: list[Scenario]
) -> None:
    for _ in range(2):  # as `cellrt run` would leave them
        run_unit(scenarios[0], "A", 0, store)

    result = run_eval(scenarios[:1], ["A"], 1, store)

    assert result.run_ids == ["baseline/A/0/a1"] and result.skipped == 1


# --- a real process, killed -----------------------------------------------------------------


def _eval(
    db: Path, scenarios_file: Path, crash_after: int | None = None
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != CRASH_AFTER_EVENTS_ENV}
    if crash_after is not None:
        env[CRASH_AFTER_EVENTS_ENV] = str(crash_after)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "cellrt",
            "eval",
            "--scenarios",
            str(scenarios_file),
            "--settings",
            "A,C",
            "--seeds",
            "2",
            "--k",
            "2",
            "--db",
            str(db),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        cwd=REPO,
        env=env,
    )


def _unfinished(db: Path) -> list[str]:
    with EventStore(db) as store:
        return [r for r in store.run_ids() if store.events(r)[-1].type != "run_finished"]


def _events_per_unit(db: Path) -> list[tuple[str, int]]:
    """(run id, event count) of each run in an uninterrupted eval, in eval order: the order the
    rows were written, which a coarse wall clock could not tell apart."""
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT run_id, COUNT(*) FROM events GROUP BY run_id ORDER BY MIN(rowid)"
        ).fetchall()
    return [(run_id, count) for run_id, count in rows]


def _middle_of_third_unit(fresh_db: Path) -> tuple[str, int, int]:
    """The third unit's run id, how many events to let through to stop in its middle, and how
    many to stop in its middle when it is the first unit to run."""
    counts = _events_per_unit(fresh_db)
    run_id, size = counts[2]
    return run_id, counts[0][1] + counts[1][1] + size // 2, size // 2


def test_an_eval_killed_mid_run_and_rerun_prints_the_same_table_as_one_that_was_not(
    tmp_path: Path,
) -> None:
    scenarios_file = tmp_path / "two.yaml"
    scenarios_file.write_text(TWO_SCENARIOS, encoding="utf-8")
    fresh = _eval(tmp_path / "fresh.sqlite", scenarios_file)
    third, crash_after, _ = _middle_of_third_unit(tmp_path / "fresh.sqlite")
    db = tmp_path / "killed.sqlite"

    killed = _eval(db, scenarios_file, crash_after=crash_after)
    interrupted = _unfinished(db)
    resumed = _eval(db, scenarios_file)

    assert fresh.returncode == 0, fresh.stderr
    assert killed.returncode == CRASH_EXIT_CODE
    assert killed.stdout == ""  # it never got to the table
    assert interrupted == [third]
    assert resumed.returncode == 0, resumed.stderr
    assert resumed.stdout == fresh.stdout
    unit = interrupted[0].removesuffix("/a1")
    with EventStore(db) as store:
        assert f"{unit}/a2" in store.run_ids()
    # The interrupted attempt can still be replayed.
    replay = subprocess.run(
        [sys.executable, "-m", "cellrt", "replay", interrupted[0], "--db", str(db)],
        capture_output=True, text=True, timeout=120, check=False, cwd=REPO,
    )  # fmt: skip
    assert replay.returncode == 0, replay.stderr
    assert "run_started" in replay.stdout and "run_finished" not in replay.stdout


def test_a_unit_interrupted_twice_finishes_as_its_third_attempt(tmp_path: Path) -> None:
    scenarios_file = tmp_path / "two.yaml"
    scenarios_file.write_text(TWO_SCENARIOS, encoding="utf-8")
    db = tmp_path / "e.sqlite"
    assert _eval(tmp_path / "fresh.sqlite", scenarios_file).returncode == 0
    third, crash_after, crash_again = _middle_of_third_unit(tmp_path / "fresh.sqlite")

    first = _eval(db, scenarios_file, crash_after=crash_after)
    assert _unfinished(db) == [third]
    unit = third.removesuffix("/a1")
    # Finished units are skipped, so the rerun starts with the interrupted unit, and stops in
    # the middle of its second attempt.
    second = _eval(db, scenarios_file, crash_after=crash_again)
    third = _eval(db, scenarios_file)

    assert (first.returncode, second.returncode, third.returncode) == (
        CRASH_EXIT_CODE,
        CRASH_EXIT_CODE,
        0,
    ), third.stderr
    with EventStore(db) as store:
        attempts = [r for r in store.run_ids() if r.startswith(unit + "/")]
        finished = [r for r in attempts if store.events(r)[-1].type == "run_finished"]
        others = [r for r in store.run_ids() if not r.startswith(unit + "/")]
    assert attempts == [f"{unit}/a1", f"{unit}/a2", f"{unit}/a3"]
    assert finished == [f"{unit}/a3"]
    assert all(r.endswith("/a1") for r in others)  # no other unit ran twice


def test_the_crash_hook_rejects_a_value_that_is_not_a_positive_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for bad in ("0", "-3", "soon"):
        monkeypatch.setenv(CRASH_AFTER_EVENTS_ENV, bad)
        with pytest.raises(ValueError, match=CRASH_AFTER_EVENTS_ENV):
            EventStore(tmp_path / "x.sqlite")
