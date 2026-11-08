"""Append-only SQLite event store."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from cellrt.runtime.events import PAYLOAD_ADAPTER, Event

DEFAULT_BUFFER_LIMIT = 500
# Test only: set to N, and the process exits abruptly after appending its N-th event, without
# flushing, as if the VM were preempted. Used to test that an eval resumes correctly.
CRASH_AFTER_EVENTS_ENV = "CELLRT_TEST_CRASH_AFTER_EVENTS"
CRASH_EXIT_CODE = 75

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    run_id  TEXT    NOT NULL,
    seq     INTEGER NOT NULL,
    ts      REAL    NOT NULL,
    wall_ts REAL    NOT NULL,
    type    TEXT    NOT NULL,
    payload TEXT    NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE INDEX IF NOT EXISTS events_by_type ON events (type);
CREATE TRIGGER IF NOT EXISTS events_no_replace BEFORE INSERT ON events
WHEN EXISTS (SELECT 1 FROM events WHERE run_id = NEW.run_id AND seq = NEW.seq)
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""

_Row = tuple[str, int, float, float, str, str]


@dataclass(frozen=True)
class RunInfo:
    """One attempt at a unit, from its `run_started`, and whether it has `run_finished`."""

    run_id: str
    scenario_id: str
    setting: str
    seed: int
    attempt: int
    config_hash: str
    finished: bool


# Every attempt with its unit. The subquery is not correlated, so SQLite builds the set of
# finished runs once instead of looking it up for every started run (which grows as n^2).
RUNS_SQL = (
    "SELECT run_id,"
    " json_extract(payload, '$.scenario_id'), json_extract(payload, '$.setting'),"
    " json_extract(payload, '$.seed'), json_extract(payload, '$.attempt'),"
    " json_extract(payload, '$.config_hash'),"
    " run_id IN (SELECT run_id FROM events WHERE type = 'run_finished')"
    " FROM events WHERE type = 'run_started'"
    " ORDER BY 2, 3, 4, 5"
)


class EventStore:
    """Writes are buffered: a non-signal event, or a full buffer, flushes everything pending.

    So a crash loses at most `buffer_limit` signal samples, never a decision event.
    """

    def __init__(self, path: Path | str, *, buffer_limit: int = DEFAULT_BUFFER_LIMIT) -> None:
        if buffer_limit < 1:
            raise ValueError("buffer_limit must be at least 1")
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._buffer: list[_Row] = []
        self._buffer_limit = buffer_limit
        self._crash_after = _crash_after_from_env()
        self._appended = 0

    def append(self, event: Event) -> None:
        self._buffer.append(
            (
                event.run_id,
                event.seq,
                event.ts,
                event.wall_ts,
                event.type,
                event.payload.model_dump_json(),
            )
        )
        if event.type != "signal_sampled" or len(self._buffer) >= self._buffer_limit:
            self.flush()
        if self._crash_after is not None:
            self._appended += 1
            if self._appended >= self._crash_after:
                os._exit(CRASH_EXIT_CODE)  # no flush, no cleanup: a hard kill

    def flush(self) -> None:
        if not self._buffer:
            return
        with self._conn:
            self._conn.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)", self._buffer)
        self._buffer.clear()

    def events(self, run_id: str) -> list[Event]:
        self.flush()
        rows = self._conn.execute(
            "SELECT run_id, seq, ts, wall_ts, payload FROM events WHERE run_id = ? ORDER BY seq",
            (run_id,),
        )
        return [
            Event(
                run_id=r[0],
                seq=r[1],
                ts=r[2],
                wall_ts=r[3],
                payload=PAYLOAD_ADAPTER.validate_json(r[4]),
            )
            for r in rows
        ]

    def next_attempt(self, scenario_id: str, setting: str, seed: int) -> int:
        self.flush()
        (count,) = self._conn.execute(
            "SELECT COUNT(*) FROM events WHERE type = 'run_started'"
            " AND json_extract(payload, '$.scenario_id') = ?"
            " AND json_extract(payload, '$.setting') = ?"
            " AND json_extract(payload, '$.seed') = ?",
            (scenario_id, setting, seed),
        ).fetchone()
        return int(count) + 1

    def run_ids(self) -> list[str]:
        """Every run that started, finished or not, sorted by id."""
        self.flush()
        rows = self._conn.execute(
            "SELECT run_id FROM events WHERE type = 'run_started' ORDER BY run_id"
        )
        return [r[0] for r in rows]

    def runs(self) -> list[RunInfo]:
        """Every attempt that started, finished or not, oldest attempt of a unit first."""
        self.flush()
        return [
            RunInfo(run_id, scenario, setting, seed, attempt, digest, bool(finished))
            for run_id, scenario, setting, seed, attempt, digest, finished in self._conn.execute(
                RUNS_SQL
            )
        ]

    def close(self) -> None:
        try:
            self.flush()
        finally:
            self._conn.close()

    def __enter__(self) -> EventStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _crash_after_from_env() -> int | None:
    raw = os.environ.get(CRASH_AFTER_EVENTS_ENV)
    if raw is None:
        return None
    try:
        count = int(raw)
    except ValueError:
        count = 0
    if count < 1:
        raise ValueError(f"{CRASH_AFTER_EVENTS_ENV} must be a positive number of events")
    return count
