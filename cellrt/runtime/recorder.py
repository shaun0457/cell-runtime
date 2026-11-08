"""Turns payloads into numbered, timestamped events for one run and keeps the live state."""

from __future__ import annotations

import time

from cellrt.runtime.clock import Clock
from cellrt.runtime.events import Event, Payload
from cellrt.runtime.state import RunState, apply
from cellrt.runtime.store import EventStore


class Recorder:
    def __init__(self, run_id: str, store: EventStore, clock: Clock) -> None:
        self.run_id = run_id
        self._store = store
        self._clock = clock
        self._seq = 0
        self._state = RunState()

    @property
    def state(self) -> RunState:
        """Live state: the fold of every event emitted so far."""
        return self._state

    def emit(self, payload: Payload) -> Event:
        self._seq += 1
        event = Event(
            run_id=self.run_id,
            seq=self._seq,
            ts=self._clock.now(),
            # Real time on purpose: debugging only, never read by the fold.
            wall_ts=time.time(),
            payload=payload,
        )
        self._store.append(event)
        self._state = apply(self._state, event)
        return event
