"""A stored run as Chrome trace event JSON, for `cellrt trace`: load it in https://ui.perfetto.dev
or chrome://tracing to see where the time went.

One process with four named tracks: the run, its skills, its planner calls and the Monitor.
Spans ("X") cover the run, each skill run and each planner call. Timestamps are whole
microseconds from the run's earliest event on the chosen clock. Everything in the output is
copied from the event log, which is scrubbed when it is written.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from cellrt.runtime.events import Event

TraceClock = Literal["wall", "sim"]
_T = TypeVar("_T")

# Points per counter track. A run on the AIC cell samples for minutes; above this the samples
# are evenly strided, always keeping the first and the last. A spike between two kept samples
# does not show: read the log (`cellrt replay --signals`) for every sample.
MAX_COUNTER_POINTS = 2_000

# No track's id: Perfetto shows the thread whose id is the process id as the "main thread".
PID = 100
TRACKS = {"run": 1, "skills": 2, "planner": 3, "monitor": 4}
_RUN_ARGS = ("scenario_id", "setting", "seed", "attempt")
# No mark of their own: progress happens inside a skill span, and tree ticks are noise.
# `signal_sampled` becomes counters.
_UNMARKED = frozenset({"signal_sampled", "skill_progress", "bt_node_changed"})
# Counter tracks from `signal_sampled`: (track name, unit, field).
_COUNTERS = (("tared force", "N", "force_n"), ("depth", "mm", "depth_mm"))
# The field an instant's name shows after its type.
_LABELS = {
    "monitor_verdict": "failure_class",
    "recovery_started": "action",
    "precondition_failed": "skill",
    "fault_injected": "fault_id",
    "fault_cleared": "fault_id",
    "hold_checked": "result",
}


@dataclass
class _Open:
    """A span that has started and not yet ended."""

    name: str
    track: str
    start: Event
    args: dict[str, Any]


def export_trace(events: list[Event], *, clock: TraceClock = "wall") -> dict[str, Any]:
    """The trace of one run's events, in seq order. `clock` "wall" times each mark by when it
    was recorded, so a model call shows its real cost; "sim" by the run's clock, which is what
    the robot saw."""
    if clock not in ("wall", "sim"):
        raise ValueError(f"clock must be 'wall' or 'sim', not {clock!r}")
    if not events:
        raise ValueError("a trace needs at least one event")
    # Both clocks go in the args, so both must be numbers JSON can hold, and the run must span a
    # finite number of microseconds on each.
    for times in ([e.ts for e in events], [e.wall_ts for e in events]):
        span_us = (max(times) - min(times)) * 1_000_000
        if not (all(math.isfinite(t) for t in times) and math.isfinite(span_us)):
            raise ValueError("an event time is not a finite number")
    trace = _Trace(events, clock)
    for event in events:
        trace.add(event)
    return trace.finish()


class _Trace:
    def __init__(self, events: list[Event], clock: TraceClock) -> None:
        self._events = events
        self._clock = clock
        self._origin = min(self._time(e) for e in events)
        self._wall_origin = min(e.wall_ts for e in events)
        self._marks: list[dict[str, Any]] = []
        self._samples: list[tuple[Event, dict[str, Any]]] = []
        self._run: _Open | None = None
        self._skills: dict[str, _Open] = {}
        self._planner: _Open | None = None
        self._started: dict[str, Any] = {}
        self._finished: dict[str, Any] = {}

    def add(self, event: Event) -> None:
        kind = event.type
        fields = _fields(event)
        if kind == "run_started":
            self._started = fields
            args = {k: fields[k] for k in _RUN_ARGS}
            self._run = self._start(self._run, _Open(event.run_id, "run", event, args))
        elif kind == "run_finished":
            self._finished = fields
            self._end(self._run, event, fields, fields, "run")
            self._run = None
        elif kind == "skill_started":
            skill = fields["skill"]
            args = {"attempt": fields["attempt"], "params": fields["params"]}
            self._skills[skill] = self._start(
                self._skills.get(skill), _Open(skill, "skills", event, args)
            )
        elif kind in ("skill_finished", "skill_cancelled"):
            if kind == "skill_finished":
                ending = {k: fields[k] for k in ("ok", "failure", "detail")}
            else:
                ending = {"cancelled": fields["reason"]}
            self._end(self._skills.pop(fields["skill"], None), event, fields, ending, "skills")
        elif kind == "planner_called":
            args = {k: fields[k] for k in ("skill", "failure_class", "call")}
            name = f"planner call {fields['call']}"
            self._planner = self._start(self._planner, _Open(name, "planner", event, args))
        elif kind == "planner_responded":
            if self._planner is None:
                self._instant("planner", event, _without(fields, "prompt"))
            else:
                # The prompt can be 16,000 characters; `cellrt replay` and the log keep it.
                self._planner.args.update(_without(fields, "skill", "prompt"))
        elif kind.startswith("planner_"):
            # Decided, rejected, timed out (planner_timed_out), or a planner event added later:
            # the call is over, and the event's fields say how.
            ending = {"result": kind, **_without(fields, "skill")}
            self._end(self._planner, event, fields, ending, "planner")
            self._planner = None
        elif kind == "signal_sampled":
            self._samples.append((event, fields))
        elif kind == "monitor_verdict":
            self._instant("monitor", event, fields)
        elif kind in ("recovery_started", "precondition_failed"):
            self._instant("skills", event, fields)
        elif kind not in _UNMARKED:
            self._instant("run", event, fields)

    def finish(self) -> dict[str, Any]:
        # A run cut short (the process died, or the log was read mid-run): whatever is still
        # open ends at the last event.
        last = self._events[-1]
        for span in [self._run, *self._skills.values(), self._planner]:
            if span is not None:
                self._close(span, last, {"unfinished": True})
        marks = sorted([*self._marks, *self._counters()], key=lambda m: (m["ts"], -m.get("dur", 0)))
        return {
            "traceEvents": [*self._metadata(), *marks],
            "displayTimeUnit": "ms",
            "otherData": {
                "run_id": self._events[0].run_id,
                "scenario": self._started.get("scenario_id"),
                "setting": self._started.get("setting"),
                "seed": self._started.get("seed"),
                "outcome": self._finished.get("outcome"),
                "reason": self._finished.get("reason"),
                "schema_version": self._started.get("schema_version"),
                "clock": self._clock,
            },
        }

    def _metadata(self) -> list[dict[str, Any]]:
        run_id = self._events[0].run_id
        meta: list[dict[str, Any]] = [
            {"name": "process_name", "ph": "M", "pid": PID, "tid": 0, "args": {"name": run_id}}
        ]
        for track, tid in TRACKS.items():
            meta.append(
                {"name": "thread_name", "ph": "M", "pid": PID, "tid": tid, "args": {"name": track}}
            )
        return meta

    def _counters(self) -> list[dict[str, Any]]:
        """Counter marks carry no clocks in their args: every arg of a counter is a series."""
        kept = _strided(self._samples, MAX_COUNTER_POINTS)
        return [
            {
                "name": name,
                "ph": "C",
                "pid": PID,
                "ts": self._us(event),
                "args": {unit: fields[field]},
            }
            for name, unit, field in _COUNTERS
            for event, fields in kept
        ]

    def _instant(self, track: str, event: Event, fields: dict[str, Any]) -> None:
        label = _LABELS.get(event.type)
        self._marks.append(
            {
                "name": f"{event.type}: {fields[label]}" if label in fields else event.type,
                "ph": "i",
                "s": "t",
                "pid": PID,
                "tid": TRACKS[track],
                "ts": self._us(event),
                "args": {**fields, **self._clocks(event)},
            }
        )

    def _start(self, open_span: _Open | None, span: _Open) -> _Open:
        """`span`, after ending the span of the same kind still open, if any, where it starts."""
        if open_span is not None:
            self._close(open_span, span.start, {"unfinished": True})
        return span

    def _end(
        self,
        span: _Open | None,
        event: Event,
        fields: dict[str, Any],
        ending: dict[str, Any],
        track: str,
    ) -> None:
        """End `span` at `event`, adding `ending` to its args. With no span open (its start is
        not in the log), `event` shows as an instant on `track` instead."""
        if span is None:
            self._instant(track, event, fields)
        else:
            self._close(span, event, ending)

    def _close(self, span: _Open, end: Event, ending: dict[str, Any]) -> None:
        start_us = self._us(span.start)
        self._marks.append(
            {
                "name": span.name,
                "ph": "X",
                "pid": PID,
                "tid": TRACKS[span.track],
                "ts": start_us,
                "dur": max(0, self._us(end) - start_us),
                "args": {
                    **span.args,
                    **ending,
                    **self._clocks(span.start),
                    "sim_dur_s": round(end.ts - span.start.ts, 6),
                    "wall_dur_s": round(end.wall_ts - span.start.wall_ts, 6),
                },
            }
        )

    def _clocks(self, event: Event) -> dict[str, Any]:
        """Both clocks, whichever the trace is timed by: the run clock as `cellrt replay` prints
        it, and wall seconds since the run's earliest event."""
        return {
            "seq": event.seq,
            "sim_s": event.ts,
            "wall_s": round(event.wall_ts - self._wall_origin, 6),
        }

    def _time(self, event: Event) -> float:
        return event.wall_ts if self._clock == "wall" else event.ts

    def _us(self, event: Event) -> int:
        return round((self._time(event) - self._origin) * 1_000_000)


def _fields(event: Event) -> dict[str, Any]:
    return event.payload.model_dump(mode="json", exclude={"type"})


def _without(fields: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: v for k, v in fields.items() if k not in keys}


def _strided(items: list[_T], limit: int) -> list[_T]:
    """At most `limit` (at least 2) items, evenly spread, always the first and the last."""
    if len(items) <= limit:
        return items
    # A step above 1 rounds to distinct, increasing indices; the last is len(items) - 1.
    step = (len(items) - 1) / (limit - 1)
    return [items[round(i * step)] for i in range(limit)]
