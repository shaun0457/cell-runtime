"""Waiting on a planner that takes real time (#47): the arm holds, the mock's sim clock stands
still, and the call has a wall-time deadline. A call past its deadline is cancelled and recorded;
the step fails without running anything. Tests use short real waits driven by the event loop's
timer (`call_later`), never a real model."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import math
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import cellrt.executor.runner as runner_module
from cellrt.eval.runner import run_unit
from cellrt.eval.scenarios import load_scenarios
from cellrt.executor.runner import DrainError
from cellrt.planner.base import MAX_PLANNER_DEADLINE_S, FailureContext
from cellrt.planner.llm import LlmPlannerConfig
from cellrt.replay import replay_lines
from cellrt.runtime.events import Event, PlannerTimedOut
from cellrt.runtime.store import EventStore

PHASE0 = Path(__file__).resolve().parents[1] / "eval" / "scenarios" / "phase0.yaml"
YAW = {s.id: s for s in load_scenarios(PHASE0)}["yaw_mismatch"]
SWITCH_TO_ACT = {"kind": "switch_skill", "skill": "insert_act"}


async def _after(seconds: float) -> None:
    """Wait real time through the loop's timer: what a network call looks like to the loop."""
    done = asyncio.Event()
    asyncio.get_running_loop().call_later(seconds, done.set)
    await done.wait()


@dataclass
class _Slow:
    """Answers after a short real wait."""

    delay_s: float
    deadline_s: float = 5.0
    answer: object = field(default_factory=lambda: dict(SWITCH_TO_ACT))

    async def triage(self, ctx: FailureContext) -> object:
        await _after(self.delay_s)
        return self.answer


@dataclass
class _Silent:
    """Never answers; records that its cleanup ran when the call was cancelled."""

    deadline_s: float = 0.05
    released: list[bool] = field(default_factory=list)

    async def triage(self, ctx: FailureContext) -> object:
        try:
            await asyncio.Event().wait()
        finally:
            self.released.append(True)
        raise AssertionError("unreachable")  # pragma: no cover


@dataclass
class _SlowToRelease:
    """Never answers; when cancelled, takes a short real wait to clean up (closing a
    connection), then lets the cancellation through."""

    cleanup_s: float
    deadline_s: float = 0.05
    released: list[bool] = field(default_factory=list)

    async def triage(self, ctx: FailureContext) -> object:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await _after(self.cleanup_s)
            self.released.append(True)
            raise
        raise AssertionError("unreachable")  # pragma: no cover


@dataclass
class _Stubborn:
    """Never answers and swallows the first cancellation: it breaks the contract. (It gives in
    to the second one, so the event loop can still shut down.)"""

    deadline_s: float = 0.05

    async def triage(self, ctx: FailureContext) -> object:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.Event().wait()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover


def _run(store: EventStore, planner: object) -> list[Event]:
    run = run_unit(YAW, "C", 0, store, planner=planner)  # type: ignore[arg-type]
    return store.events(run.run_id)


def _types(events: list[Event]) -> list[str]:
    return [e.type for e in events]


def _one(events: list[Event], event_type: str) -> Event:
    [event] = [e for e in events if e.type == event_type]
    return event


def test_a_planner_that_waits_on_real_time_is_awaited_and_the_run_carries_on(
    store: EventStore,
) -> None:
    events = _run(store, _Slow(delay_s=0.02))

    assert events[-1].payload.outcome == "success"
    assert [e.payload.action for e in events if e.type == "recovery_started"] == [
        "switch_skill:insert_act"
    ]


def test_the_sim_clock_stands_still_while_the_planner_thinks(store: EventStore) -> None:
    events = _run(store, _Slow(delay_s=0.03))

    called, decided = _one(events, "planner_called"), _one(events, "planner_decided")
    assert called.ts == decided.ts
    between = [e.type for e in events if called.seq < e.seq < decided.seq]
    assert "signal_sampled" not in between


def test_a_planner_that_never_answers_is_cancelled_at_its_deadline(store: EventStore) -> None:
    planner = _Silent(deadline_s=0.05)

    events = _run(store, planner)

    timed_out = _one(events, "planner_timed_out").payload
    assert isinstance(timed_out, PlannerTimedOut)
    assert (timed_out.skill, timed_out.deadline_s) == ("insert_scripted", 0.05)
    assert timed_out.waited_s >= 0.05
    assert planner.released == [True]  # the call was cancelled and its cleanup ran
    for absent in ("planner_decided", "planner_output_rejected", "recovery_started"):
        assert absent not in _types(events)
    finished = events[-1].payload
    assert finished.outcome == "aborted"
    assert "planner timed out" in finished.reason


def test_a_cancelled_call_may_take_real_time_to_clean_up(store: EventStore) -> None:
    planner = _SlowToRelease(cleanup_s=0.05)

    events = _run(store, planner)

    assert planner.released == [True]
    assert events[-1].payload.outcome == "aborted"


def test_a_call_that_ignores_its_cancellation_fails_loudly_after_the_grace(
    store: EventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_module, "CANCEL_GRACE_S", 0.05)

    with pytest.raises(DrainError, match="planner@insert_scripted"):
        _run(store, _Stubborn())


def test_an_answer_after_the_deadline_is_never_acted_on(store: EventStore) -> None:
    events = _run(store, _Slow(delay_s=0.5, deadline_s=0.05))

    assert "planner_timed_out" in _types(events)
    assert "planner_decided" not in _types(events)
    assert "insert_act" not in [e.payload.skill for e in events if e.type == "skill_started"]


def test_the_deadline_is_bounded(store: EventStore) -> None:
    LlmPlannerConfig(model="m", deadline_s=MAX_PLANNER_DEADLINE_S)
    bad_values: tuple[object, ...] = (
        0.0,
        -1.0,
        MAX_PLANNER_DEADLINE_S + 0.001,
        math.nan,
        math.inf,
        True,
        "30",
    )
    for bad in bad_values:
        with pytest.raises(ValueError):
            LlmPlannerConfig(model="m", deadline_s=bad)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            _run(store, _Slow(delay_s=0.0, deadline_s=bad))


def test_the_deadline_counts_in_the_config_hash(store: EventStore) -> None:
    from cellrt.planner.llm import ChatReply, ChatRequest, LlmPlanner

    class _Answers:
        def describe(self) -> dict[str, str]:
            return {"provider": "scripted"}

        async def complete(self, request: ChatRequest) -> ChatReply:
            return ChatReply("m", '{"kind": "retry"}', None, None, None, "tool_calls")

    def config_hash(deadline_s: float) -> str:
        planner = LlmPlanner(_Answers(), LlmPlannerConfig(model="m", deadline_s=deadline_s))
        return _run(store, planner)[0].payload.config_hash

    assert config_hash(10.0) != config_hash(20.0)

    def plain_hash(deadline_s: float) -> str:  # a planner without describe()
        return _run(store, _Slow(delay_s=0.0, deadline_s=deadline_s))[0].payload.config_hash

    assert plain_hash(10.0) != plain_hash(20.0)


def test_replay_shows_a_timed_out_call(store: EventStore) -> None:
    events = _run(store, _Silent(deadline_s=0.05))

    [line] = [ln for ln in replay_lines(events) if "planner_timed_out" in ln]
    assert "insert_scripted" in line and "0.05s" in line and "cancelled" in line


async def _yield_to_loop(times: int) -> None:
    """Give the loop `times` turns without any timer: a call that awaits, deterministically."""
    loop = asyncio.get_running_loop()
    for _ in range(times):
        turn = loop.create_future()
        loop.call_soon(turn.set_result, None)
        await turn


@dataclass
class _Yields:
    """Answers after a fixed number of loop turns, within the drain or after it."""

    times: int
    deadline_s: float = 5.0

    async def triage(self, ctx: FailureContext) -> object:
        await _yield_to_loop(self.times)
        return dict(SWITCH_TO_ACT)


@dataclass
class _Instant:
    """Answers in its first step, without awaiting anything, like the rule planner."""

    async def triage(self, ctx: FailureContext) -> object:
        return dict(SWITCH_TO_ACT)


def _between(events: list[Event], first: str, last: str) -> list[str]:
    a, b = _one(events, first), _one(events, last)
    return [e.type for e in events if a.seq < e.seq < b.seq]


@pytest.mark.parametrize("times", [1, 50, 150])
def test_a_call_that_awaits_holds_the_world_however_soon_it_answers(
    store: EventStore, times: int
) -> None:
    events = _run(store, _Yields(times))

    called, decided = _one(events, "planner_called"), _one(events, "planner_decided")
    assert called.ts == decided.ts
    assert "signal_sampled" not in _between(events, "planner_called", "planner_decided")
    assert events[-1].payload.outcome == "success"


def test_a_call_that_answers_in_its_first_step_costs_one_step_as_in_phase_0(
    store: EventStore,
) -> None:
    events = _run(store, _Instant())

    assert _between(events, "planner_called", "planner_decided").count("signal_sampled") == 1


@dataclass
class _FailsInCleanup:
    """Never answers; when cancelled, its cleanup fails with a secret in the message."""

    deadline_s: float = 0.05

    async def triage(self, ctx: FailureContext) -> object:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("upstream said: api_key=SECRET-123 user=alice") from None
        raise AssertionError("unreachable")  # pragma: no cover


def test_a_cancelled_call_that_fails_in_cleanup_leaks_nothing(
    store: EventStore, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    events = _run(store, _FailsInCleanup())
    gc.collect()

    assert events[-1].payload.outcome == "aborted"
    out, err = capsys.readouterr()
    for text in (caplog.text, out, err, str([e.payload for e in events])):
        assert "SECRET" not in text
