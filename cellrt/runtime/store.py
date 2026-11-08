"""Append-only SQLite event store."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import TracebackType

from cellrt.runtime.events import PAYLOAD_ADAPTER, Event

DEFAULT_BUFFER_LIMIT = 500

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
