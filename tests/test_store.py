"""The event store's public API: append-only, bounded write buffer, attempt numbering."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cellrt.runtime.events import Event, RunStarted, SignalSampled
from cellrt.runtime.store import EventStore
from cellrt.runtime.task import Task


def _signal(run_id: str, seq: int) -> Event:
    return Event(
        run_id=run_id,
        seq=seq,
        ts=seq * 0.05,
        wall_ts=0.0,
        payload=SignalSampled(
            step=seq,
            height_mm=1.0,
            depth_mm=0.0,
            xy_error_mm=0.0,
            raw_force_n=(0.0, 0.0, 0.0),
            tare_offset_n=(0.0, 0.0, 0.0),
            force_n=0.0,
            torque_nm=0.0,
            wrench_valid=True,
        ),
    )


def _started(run_id: str, scenario: str, setting: str, seed: int, attempt: int) -> Event:
    return Event(
        run_id=run_id,
        seq=1,
        ts=0.0,
        wall_ts=0.0,
        payload=RunStarted(
            scenario_id=scenario,
            setting=setting,
            seed=seed,
            attempt=attempt,
            task=Task(),
            config_hash="x",
        ),
    )


def test_events_cannot_be_updated_or_deleted(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite"
    with EventStore(path) as store:
        store.append(_started("r", "baseline", "A", 0, 1))

    conn = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE events SET type = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM events")
    conn.close()


def test_existing_events_cannot_be_replaced(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite"
    with EventStore(path) as store:
        store.append(_started("r", "baseline", "A", 0, 1))

    conn = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "INSERT OR REPLACE INTO events VALUES ('r', 1, 0.0, 0.0, 'skill_started', '{}')"
        )
    conn.close()


def test_signal_samples_are_flushed_when_the_buffer_is_full(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite"
    writer = EventStore(path, buffer_limit=5)
    reader = EventStore(path)
    for seq in range(1, 5):
        writer.append(_signal("r", seq))

    assert reader.events("r") == []  # four samples still buffered

    writer.append(_signal("r", 5))  # buffer reaches its bound

    assert [e.seq for e in reader.events("r")] == [1, 2, 3, 4, 5]
    writer.close()
    reader.close()


def test_a_decision_event_flushes_buffered_samples(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite"
    writer = EventStore(path)
    reader = EventStore(path)
    writer.append(_signal("r", 1))
    writer.append(_started("r2", "baseline", "A", 0, 1))

    assert [e.seq for e in reader.events("r")] == [1]
    writer.close()
    reader.close()


def test_next_attempt_counts_started_runs_of_the_same_unit(store: EventStore) -> None:
    assert store.next_attempt("baseline", "A", 0) == 1
    store.append(_started("baseline/A/0/a1", "baseline", "A", 0, 1))
    store.append(_started("baseline/A/1/a1", "baseline", "A", 1, 1))

    assert store.next_attempt("baseline", "A", 0) == 2
    assert store.next_attempt("baseline", "A", 1) == 2
    assert store.next_attempt("other", "A", 0) == 1


def test_buffer_limit_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="buffer_limit"):
        EventStore(tmp_path / "events.sqlite", buffer_limit=0)
