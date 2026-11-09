"""`InsertCable` feedback from the run's own events: each skill's start and end, and its
progress at most once per second of sim time (the plan's 1 Hz). Messages are fixed text built
from skill names and fractions, never from upstream text. Feedback is best effort: a failing
`send_feedback` never stops the run. The progress bound is one for the run, not per skill: a
skill's first progress can wait if the last skill's was sent less than a second before.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable

from cellrt.runtime.clock import Clock
from cellrt.runtime.events import (
    Event,
    Payload,
    SkillCancelled,
    SkillFinished,
    SkillProgress,
    SkillStarted,
)
from cellrt.runtime.recorder import Recorder
from cellrt.runtime.store import EventStore

PROGRESS_INTERVAL_S = 1.0


class FeedbackRecorder(Recorder):
    """A `Recorder` that also turns skill events into feedback messages."""

    def __init__(
        self,
        run_id: str,
        store: EventStore,
        clock: Clock,
        send: Callable[[str], None],
        *,
        progress_interval_s: float = PROGRESS_INTERVAL_S,
    ) -> None:
        super().__init__(run_id, store, clock)
        self._send = send
        self._interval_s = progress_interval_s
        self._last_progress_s: float | None = None

    def emit(self, payload: Payload) -> Event:
        event = super().emit(payload)
        message = self._message(payload, event.ts)
        if message is not None:
            with contextlib.suppress(Exception):
                self._send(message)
        return event

    def _message(self, payload: Payload, ts: float) -> str | None:
        match payload:
            case SkillStarted(skill=skill):
                return f"{skill} started"
            case SkillFinished(skill=skill, ok=ok):
                return f"{skill} finished" if ok else f"{skill} failed"
            case SkillCancelled(skill=skill, reason=reason):
                return f"{skill} stopped ({reason})"
            case SkillProgress(skill=skill, fraction=fraction):
                last = self._last_progress_s
                if last is not None and ts - last < self._interval_s - 1e-9:
                    return None
                self._last_progress_s = ts
                return f"{skill} {fraction:.0%}"
        return None
