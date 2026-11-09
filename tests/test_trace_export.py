"""`cellrt trace` (#50): a stored run as Chrome trace event JSON, for Perfetto. The seam is a run
left in the event log by `run_unit`, read back and exported."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel

from cellrt.eval.runner import run_unit
from cellrt.eval.scenarios import load_scenarios
from cellrt.planner.llm import ChatReply, ChatRequest, LlmPlanner, LlmPlannerConfig
from cellrt.planner.rule_planner import RulePlanner
from cellrt.runtime.events import Event, SignalSampled, SkillStarted
from cellrt.runtime.store import EventStore
from cellrt.trace_export import MAX_COUNTER_POINTS, export_trace

PHASE0 = Path(__file__).resolve().parents[1] / "eval" / "scenarios" / "phase0.yaml"
YAW = {s.id: s for s in load_scenarios(PHASE0)}["yaw_mismatch"]
SWITCH_TO_ACT = '{"kind": "switch_skill", "skill": "insert_act"}'


def _yaw_run(store: EventStore, **kwargs: object) -> list[Event]:
    run = run_unit(YAW, "C", 0, store, **kwargs)  # type: ignore[arg-type]
    return store.events(run.run_id)


def _round_trip(trace: dict[str, Any]) -> dict[str, Any]:
    """The trace as a file holds it: valid JSON, read back."""
    return json.loads(json.dumps(trace, allow_nan=False))


def _tracks(trace: dict[str, Any]) -> dict[str, int]:
    return {
        e["args"]["name"]: e["tid"]
        for e in trace["traceEvents"]
        if e["ph"] == "M" and e["name"] == "thread_name"
    }


def _spans(trace: dict[str, Any], track: str) -> list[dict[str, Any]]:
    tid = _tracks(trace)[track]
    return [e for e in trace["traceEvents"] if e["ph"] == "X" and e["tid"] == tid]


def _end(span: dict[str, Any]) -> float:
    return span["ts"] + span["dur"]


def test_a_run_exports_run_skill_and_planner_spans_in_order_and_nested_in_time(
    store: EventStore,
) -> None:
    events = _yaw_run(store, planner=RulePlanner())

    trace = _round_trip(export_trace(events))

    assert trace["displayTimeUnit"] == "ms"
    assert set(_tracks(trace)) == {"run", "skills", "planner", "monitor"}
    [run] = _spans(trace, "run")
    assert run["ts"] == 0
    assert run["args"]["outcome"] == "success"
    assert run["args"]["steps"] == 5
    skills = _spans(trace, "skills")
    assert [s["name"] for s in skills] == [
        "approach", "align", "insert_scripted", "insert_act", "verify"
    ]  # fmt: skip
    assert [s["ts"] for s in skills] == sorted(s["ts"] for s in skills)
    assert all(run["ts"] <= s["ts"] and _end(s) <= _end(run) for s in skills)
    assert all(_end(a) <= b["ts"] for a, b in pairwise(skills))
    stuck, switched = skills[2], skills[3]
    assert stuck["args"]["cancelled"] == "preempted"
    assert switched["args"]["ok"] is True
    [call] = _spans(trace, "planner")
    assert _end(stuck) <= call["ts"] and _end(call) <= switched["ts"]
    assert call["args"]["failure_class"] == "UNKNOWN"
    assert call["args"]["call"] == 1
    assert call["args"]["decision"] == {"kind": "switch_skill", "skill": "insert_act"}


@dataclass
class _ScriptedClient:
    """Answers every request with one scripted reply, as the client in test_llm_planner does."""

    reply: ChatReply
    requests: list[ChatRequest] = field(default_factory=list)

    def describe(self) -> Mapping[str, str]:
        return {"provider": "scripted"}

    async def complete(self, request: ChatRequest) -> ChatReply:
        self.requests.append(request)
        return self.reply


def _model_planner(answer: str) -> LlmPlanner:
    reply = ChatReply(
        model="test-model",
        tool_arguments=answer,
        text=None,
        prompt_tokens=812,
        completion_tokens=37,
        finish_reason="tool_calls",
    )
    return LlmPlanner(_ScriptedClient(reply), LlmPlannerConfig(model="test-model"))


def test_a_model_call_carries_its_trace_and_decision_in_the_planner_span(
    store: EventStore,
) -> None:
    events = _yaw_run(store, planner=_model_planner(SWITCH_TO_ACT))

    [call] = _spans(_round_trip(export_trace(events)), "planner")

    args = call["args"]
    assert (args["provider"], args["model"]) == ("scripted", "test-model")
    assert (args["prompt_tokens"], args["completion_tokens"]) == (812, 37)
    assert args["finish_reason"] == "tool_calls"
    assert args["latency_s"] >= 0
    assert args["response"] == SWITCH_TO_ACT
    assert args["result"] == "planner_decided"
    assert args["decision"] == {"kind": "switch_skill", "skill": "insert_act"}
    assert "prompt" not in args  # up to 16,000 characters: `cellrt replay` and the log have it


def test_a_rejected_answer_ends_the_planner_span_with_its_errors(store: EventStore) -> None:
    events = _yaw_run(store, planner=_model_planner("I would retry."))

    trace = _round_trip(export_trace(events))

    [call] = _spans(trace, "planner")
    assert call["args"]["result"] == "planner_output_rejected"
    assert call["args"]["errors"] == ["<root>: not_json"]
    assert trace["otherData"]["outcome"] == "aborted"


def _instants(trace: dict[str, Any], track: str) -> list[dict[str, Any]]:
    tid = _tracks(trace)[track]
    return [e for e in trace["traceEvents"] if e["ph"] == "i" and e["tid"] == tid]


def test_monitor_verdicts_recoveries_and_faults_are_instants(store: EventStore) -> None:
    events = _yaw_run(store, planner=RulePlanner())

    trace = _round_trip(export_trace(events))

    [verdict] = _instants(trace, "monitor")
    assert verdict["s"] == "t"
    assert verdict["name"] == "monitor_verdict: UNKNOWN"
    assert verdict["args"]["rule"] == "insert_stalled"
    assert verdict["args"]["skill"] == "insert_scripted"
    assert "force_n" in verdict["args"]["evidence"]
    [stuck] = [s for s in _spans(trace, "skills") if s["name"] == "insert_scripted"]
    assert stuck["ts"] <= verdict["ts"] <= _end(stuck)
    [recovery] = _instants(trace, "skills")
    assert recovery["name"] == "recovery_started: switch_skill:insert_act"
    assert recovery["args"]["failure_class"] == "UNKNOWN"
    faults = [i["name"] for i in _instants(trace, "run")]
    assert faults == ["fault_injected: yaw_mismatch#0", "fault_cleared: yaw_mismatch#0"]


def _counter(trace: dict[str, Any], name: str) -> list[tuple[int, float]]:
    return [
        (e["ts"], *e["args"].values())
        for e in trace["traceEvents"]
        if e["ph"] == "C" and e["name"] == name
    ]


def test_tared_force_and_depth_are_counter_tracks(store: EventStore) -> None:
    events = _yaw_run(store, planner=RulePlanner())
    samples = [e.payload for e in events if isinstance(e.payload, SignalSampled)]

    trace = _round_trip(export_trace(events, clock="sim"))

    force, depth = _counter(trace, "tared force"), _counter(trace, "depth")
    assert len(force) == len(depth) == len(samples) < MAX_COUNTER_POINTS
    assert [v for _, v in force] == [s.force_n for s in samples]
    assert [v for _, v in depth] == [s.depth_mm for s in samples]
    assert [t for t, _ in force] == sorted(t for t, _ in force)


def _sample(seq: int) -> Event:
    return Event(
        run_id="long/A/0/a1",
        seq=seq,
        ts=seq * 0.05,
        wall_ts=1_000.0 + seq * 0.01,
        payload=SignalSampled(
            step=seq,
            height_mm=0.0,
            depth_mm=seq / 100,
            xy_error_mm=0.0,
            raw_force_n=(0.0, 0.0, float(seq)),
            tare_offset_n=(0.0, 0.0, 0.0),
            force_n=float(seq),
            torque_nm=0.0,
            wrench_valid=True,
        ),
    )


def test_counters_are_cut_to_the_limit_keeping_the_first_and_last_sample() -> None:
    for count, kept in (
        (MAX_COUNTER_POINTS, MAX_COUNTER_POINTS),
        (MAX_COUNTER_POINTS + 1, MAX_COUNTER_POINTS),
        (MAX_COUNTER_POINTS * 3 + 7, MAX_COUNTER_POINTS),
    ):
        events = [_sample(seq) for seq in range(1, count + 1)]

        trace = _round_trip(export_trace(events, clock="sim"))

        for name in ("tared force", "depth"):
            points = _counter(trace, name)
            assert len(points) == kept
            assert points[0][0] == 0
            assert points[-1][0] == round((count - 1) * 0.05 * 1_000_000)
            assert len({t for t, _ in points}) == kept  # no sample twice
        force = [v for _, v in _counter(trace, "tared force")]
        assert (force[0], force[-1]) == (1.0, float(count))
        strides = {b - a for a, b in pairwise(force)}
        assert max(strides) - min(strides) <= 1  # evenly spread


def test_the_sim_clock_times_marks_by_the_run_clock_and_wall_by_when_they_were_recorded(
    store: EventStore,
) -> None:
    events = _yaw_run(store, planner=RulePlanner())
    first, last = events[0], events[-1]

    sim = _round_trip(export_trace(events, clock="sim"))
    wall = _round_trip(export_trace(events, clock="wall"))

    [sim_run], [wall_run] = _spans(sim, "run"), _spans(wall, "run")
    assert sim_run["dur"] == round((last.ts - first.ts) * 1_000_000) == 10_050_000
    assert wall_run["dur"] == round((last.wall_ts - first.wall_ts) * 1_000_000)
    assert (sim["otherData"]["clock"], wall["otherData"]["clock"]) == ("sim", "wall")
    for trace in (sim, wall):  # either way, each mark has both clocks in its args
        [verdict] = _instants(trace, "monitor")
        assert verdict["args"]["sim_s"] == 7.2
        assert verdict["args"]["wall_s"] > 0
        for span in [*_spans(trace, "run"), *_spans(trace, "skills"), *_spans(trace, "planner")]:
            assert {"sim_s", "wall_s", "sim_dur_s", "wall_dur_s"} <= set(span["args"])
    assert sim_run["args"]["sim_dur_s"] == 10.05
    [sim_verdict], [wall_verdict] = _instants(sim, "monitor"), _instants(wall, "monitor")
    assert sim_verdict["ts"] == 7_200_000
    assert abs(wall_verdict["ts"] - wall_verdict["args"]["wall_s"] * 1_000_000) <= 1


def _cut_after(events: list[Event], event_type: str) -> list[Event]:
    """The run as the log holds it when the process died just after the first `event_type`."""
    index = next(i for i, e in enumerate(events) if e.type == event_type)
    return events[: index + 1]


def test_spans_of_an_unfinished_run_end_at_its_last_event(store: EventStore) -> None:
    events = _yaw_run(store, planner=RulePlanner())
    mid_skill = _cut_after(events, "fault_injected")  # insert_scripted is running
    mid_call = _cut_after(events, "planner_called")

    for cut, track, name in ((mid_skill, "skills", "insert_scripted"), (mid_call, "planner",
                             "planner call 1")):  # fmt: skip
        trace = _round_trip(export_trace(cut, clock="sim"))

        last_us = round(cut[-1].ts * 1_000_000)
        [run] = _spans(trace, "run")
        assert run["args"]["unfinished"] is True
        assert _end(run) == last_us
        open_span = _spans(trace, track)[-1]
        assert open_span["name"] == name
        assert open_span["args"]["unfinished"] is True
        assert _end(open_span) == last_us
        assert trace["otherData"]["outcome"] is None
    finished_skills = _spans(_round_trip(export_trace(mid_call)), "skills")
    assert all("unfinished" not in s["args"] for s in finished_skills)


def test_a_skill_started_again_before_it_ended_ends_its_open_span_unfinished() -> None:
    def started(seq: int) -> Event:
        payload = SkillStarted(skill="approach", attempt=seq)
        return Event(run_id="r", seq=seq, ts=float(seq), wall_ts=float(seq), payload=payload)

    trace = _round_trip(export_trace([started(1), started(2)], clock="sim"))

    first, second = _spans(trace, "skills")
    assert (first["args"]["attempt"], first["ts"], first["dur"]) == (1, 0, 1_000_000)
    assert first["args"]["unfinished"] is True
    assert (second["args"]["attempt"], second["ts"], second["dur"]) == (2, 1_000_000, 0)


class _PlannerTimedOut(BaseModel):
    """The shape #47 adds to the log; not in this checkout's event union."""

    type: Literal["planner_timed_out"] = "planner_timed_out"
    skill: str
    deadline_s: float
    waited_s: float


def test_a_planner_event_the_export_does_not_know_ends_the_call_with_its_fields(
    store: EventStore,
) -> None:
    events = _yaw_run(store, planner=RulePlanner())
    called = next(i for i, e in enumerate(events) if e.type == "planner_called")
    decided = next(i for i, e in enumerate(events) if e.type == "planner_decided")
    timed_out = Event.model_construct(
        run_id=events[0].run_id,
        seq=events[decided].seq,
        ts=events[decided].ts,
        wall_ts=events[decided].wall_ts,
        payload=_PlannerTimedOut(skill="insert_scripted", deadline_s=30.0, waited_s=30.0),
    )
    log = [*events[: called + 1], timed_out, *events[decided + 1 :]]

    [call] = _spans(_round_trip(export_trace(log, clock="sim")), "planner")

    assert call["args"]["result"] == "planner_timed_out"
    assert (call["args"]["deadline_s"], call["args"]["waited_s"]) == (30.0, 30.0)
    assert _end(call) == round(events[decided].ts * 1_000_000)
    assert "unfinished" not in call["args"]


def test_the_export_refuses_what_it_cannot_turn_into_a_trace() -> None:
    def started(ts: float, wall_ts: float) -> Event:
        payload = SkillStarted(skill="approach", attempt=1)
        return Event(run_id="r", seq=1, ts=ts, wall_ts=wall_ts, payload=payload)

    for events, clock in (
        ([], "wall"),
        ([started(0.0, 0.0)], "cpu"),
        ([started(0.0, math.inf)], "sim"),  # the other clock goes in the args too
        ([started(-math.inf, 0.0)], "wall"),
        ([started(0.0, 0.0), started(1e303, 0.0)], "sim"),  # finite, but not in microseconds
        ([started(0.0, 0.0), started(0.0, -1e303)], "sim"),
    ):
        with pytest.raises(ValueError):
            export_trace(events, clock=clock)  # type: ignore[arg-type]


def test_an_end_whose_start_is_not_in_the_log_is_still_shown_as_an_instant(
    store: EventStore,
) -> None:
    events = _yaw_run(store, planner=_model_planner(SWITCH_TO_ACT))
    called = next(i for i, e in enumerate(events) if e.type == "planner_called")
    started = next(i for i, e in enumerate(events) if e.type == "skill_started")
    orphans = [e for i, e in enumerate(events) if i not in (called, started)]

    trace = _round_trip(export_trace(orphans))

    assert [i["name"] for i in _instants(trace, "planner")] == [
        "planner_responded", "planner_decided"
    ]  # fmt: skip
    assert "planner_responded" not in json.dumps(_instants(trace, "run"))
    [approach_end] = [i for i in _instants(trace, "skills") if i["args"].get("skill") == "approach"]
    assert approach_end["name"] == "skill_finished"
