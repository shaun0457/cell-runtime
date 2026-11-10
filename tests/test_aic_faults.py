"""#60: an injected fault in the AIC cell that the Monitor classifies UNKNOWN. The link to the
arm controller drops when the plug tip first comes within `onset_height_mm` of the port, for
`duration_s` of sim time: every motion command in between is lost, so the plug hangs where it
was. With fakes (tests/aic_fakes.py): the plug hangs 40 mm under the TCP and the fake keeps
CheatCode's tip-offset quirk, so the descent reaches the port during align, as in the sim."""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass, field
from typing import Any

import pytest
from aic_fakes import FakeNode

from adapters.aic.convert import MotionCommand, Pose
from adapters.aic.faults import (
    MAX_ONSET_HEIGHT_MM,
    MAX_SPEC_CHARS,
    SPEC_REFUSED,
    CommandDropout,
    FaultSpec,
)
from adapters.aic.run import PolicySetupError, fault_setup, run_goal
from adapters.aic.skills import DESCENT_STEP_M
from cellrt.planner.base import FailureContext
from cellrt.runtime.events import (
    Event,
    FaultCleared,
    FaultInjected,
    HoldChecked,
    MonitorVerdict,
    PlannerCalled,
    PlannerOutputRejected,
    RunFinished,
    RunStarted,
    SignalSampled,
    SkillStarted,
)
from cellrt.runtime.store import EventStore
from cellrt.runtime.task import Task

TASK = Task(id="trial_1_task_1", time_limit=180.0)


def _dropout(duration_s: float, onset_height_mm: float = 20.0) -> CommandDropout:
    return CommandDropout(
        fault_id="command_dropout", onset_height_mm=onset_height_mm, duration_s=duration_s
    )


def _events(store: EventStore) -> list[Event]:
    (run,) = store.runs()
    return list(store.events(run.run_id))


def _payloads(store: EventStore, kind: type) -> list[Any]:
    return [e.payload for e in _events(store) if isinstance(e.payload, kind)]


def _end(store: EventStore) -> RunFinished:
    (end,) = _payloads(store, RunFinished)
    return end


@dataclass
class _Timed(FakeNode):
    """Keeps the node's sim time at every motion it receives."""

    sent_at: list[float] = field(default_factory=list)

    def send(self, motion: MotionCommand) -> None:
        super().send(motion)
        self.sent_at.append(self.sim_s)


# --- the fault ---------------------------------------------------------------------------------


def test_a_dropout_loses_every_command_for_its_duration_then_lets_them_through(
    store: EventStore,
) -> None:
    # 3 s down from when the tip is 20 mm above the port, half way down align: afterwards the
    # arm goes on from align's next target and the plug still seats.
    node = _Timed(task=TASK)

    assert run_goal(node, TASK, store, fault=_dropout(3.0)) is True

    (injected,) = _payloads(store, FaultInjected)
    (cleared,) = _payloads(store, FaultCleared)
    for fault in (injected, cleared):
        assert (fault.fault_id, fault.kind, fault.label) == (
            "command_dropout",
            "command_dropout",
            "UNKNOWN",
        )
        assert fault.params == {"onset_height_mm": 20.0, "duration_s": 3.0}
    events = _events(store)
    on = next(i for i, e in enumerate(events) if isinstance(e.payload, FaultInjected))
    off = next(i for i, e in enumerate(events) if isinstance(e.payload, FaultCleared))
    assert events[off].ts - events[on].ts == pytest.approx(3.0, abs=0.051)
    heights = [e.payload.height_mm for e in events[:on] if isinstance(e.payload, SignalSampled)]
    assert heights[-1] <= 20.0 < heights[-2]  # it starts on the first sample within 20 mm
    # Nothing reached the arm while the link was down, and commands went on afterwards. Gaps
    # after the clear do not count: verify holds still for its 5 s settle by design.
    cleared_at = events[off].ts + node.sim_s - events[-1].ts  # in the node's sim time
    gaps = [b - a for a, b in zip(node.sent_at, node.sent_at[1:], strict=False) if a < cleared_at]
    assert max(gaps) == pytest.approx(3.0, abs=0.11)
    assert sum(a > cleared_at for a in node.sent_at) > 10
    # The link was back when the run ended, so its HOLD reached the arm and is confirmed.
    (hold,) = _payloads(store, HoldChecked)
    assert hold.result == "hold_confirmed"


def test_setting_a_ends_on_the_unknown_of_a_long_dropout(store: EventStore) -> None:
    # 30 s down: align runs out its targets into the void, insert_scripted starts with the plug
    # hanging 20 mm above the port, and after 1 s without progress, at low force, the Monitor
    # calls it UNKNOWN. A does not recover.
    node = FakeNode(task=TASK)

    assert run_goal(node, TASK, store, setting="A", fault=_dropout(30.0)) is False

    (verdict,) = _payloads(store, MonitorVerdict)
    assert (verdict.skill, verdict.failure_class, verdict.rule) == (
        "insert_scripted",
        "UNKNOWN",
        "insert_stalled",
    )
    assert _end(store).reason == "insert_scripted failed: UNKNOWN"
    assert _payloads(store, FaultCleared) == []  # the run ended while the link was down


def test_a_run_that_ends_while_the_link_is_down_does_not_confirm_a_hold(
    store: EventStore,
) -> None:
    # The run's HOLD is lost with every other command, so the arm stays at its last target.
    # It may well be still there, but nothing the run sent stopped it: the stop is not
    # confirmed, and the check does not wait for one.
    node = FakeNode(task=TASK)

    assert run_goal(node, TASK, store, setting="A", fault=_dropout(30.0)) is False

    assert _payloads(store, FaultCleared) == []
    (hold,) = _payloads(store, HoldChecked)
    assert (hold.result, hold.samples, hold.waited_s) == ("hold_unconfirmed", 0, 0.0)


def test_setting_c_asks_the_planner_about_it(store: EventStore) -> None:
    node = FakeNode(task=TASK)

    assert run_goal(node, TASK, store, setting="C", fault=_dropout(30.0)) is False

    (called,) = _payloads(store, PlannerCalled)
    assert (called.skill, called.failure_class) == ("insert_scripted", "UNKNOWN")
    # The rule planner has no rule for UNKNOWN in insert_scripted without insert_act.
    assert _end(store).reason == (
        "insert_scripted failed: UNKNOWN (planner aborted: no rule for UNKNOWN in insert_scripted)"
    )


async def _next_turn() -> None:
    loop = asyncio.get_running_loop()
    turn: asyncio.Future[None] = loop.create_future()
    loop.call_soon(turn.set_result, None)
    await turn


@dataclass
class _RetriesAfter:
    """Answers retry once the cell's sim time has moved on `sim_s` since the call, and keeps
    how many commands the arm had received by then."""

    node: FakeNode
    sim_s: float
    deadline_s: float = 30.0
    answered_at: int | None = None  # len(node.sent) when it answered

    async def triage(self, ctx: FailureContext) -> object:
        until = self.node.sim_s + self.sim_s
        while self.node.sim_s < until:
            await _next_turn()
        self.answered_at = len(self.node.sent)
        return {"kind": "retry"}


def _moves_after_the_answer(
    node: FakeNode, planner: _RetriesAfter
) -> list[tuple[tuple[float, float, float], tuple[float, float, float]]]:
    """(where the TCP was, where it was sent) for every position target after the retry."""
    assert planner.answered_at is not None
    sent = list(zip(node.tcp_at_send, node.sent, strict=True))[planner.answered_at :]
    return [(at.position, m.position_m) for at, m in sent if m.mode == "position"]


@dataclass
class _Slips(FakeNode):
    """The plug slips `slip_m` along x on the TCP while its tip is within `below_m` of the
    port, which is where the dropout starts: its XY error stays while no command gets through."""

    slip_m: float = 0.0008
    below_m: float = 0.02

    @property
    def plug(self) -> Pose:
        plug = super().plug
        x, y, z = plug.position
        if z - self.port.position[2] > self.below_m:
            return plug
        return Pose(position=(x + self.slip_m, y, z), orientation=plug.orientation)


def test_setting_c_can_retry_once_the_link_is_back(store: EventStore) -> None:
    # 15 s down: the stall is called about 10 s in; the planner answers retry 8 s later, after
    # the link came back, and insert_scripted goes on and seats the plug.
    node = FakeNode(task=TASK)

    ok = run_goal(
        node, TASK, store, setting="C", planner=_RetriesAfter(node, 8.0), fault=_dropout(15.0)
    )

    assert ok is True
    events = _events(store)
    kinds = [e.payload.type for e in events]
    assert kinds.index("fault_cleared") < kinds.index("planner_decided")


@pytest.mark.parametrize("onset_mm", [20.0, MAX_ONSET_HEIGHT_MM])
def test_a_retry_after_a_dropout_goes_on_down_from_where_the_arm_is(
    store: EventStore, onset_mm: float
) -> None:
    # #61 goal1 C-LLM #1: the descent's targets went on down while the link was down, so the
    # retry's first command took the plug from 11.6 mm above the port to 1 mm under it in
    # 0.15 s, at 86 N. The retry goes on from where the arm is: no target lies more than one
    # 0.5 mm step under the TCP. From the highest onset it still seats the plug within
    # insert_scripted's timeout: the dropout's UNKNOWN is the run's only verdict.
    node = FakeNode(task=TASK)
    planner = _RetriesAfter(node, 8.0)

    ok = run_goal(node, TASK, store, setting="C", planner=planner, fault=_dropout(15.0, onset_mm))

    assert ok is True
    assert [v.failure_class for v in _payloads(store, MonitorVerdict)] == ["UNKNOWN"]
    moves = _moves_after_the_answer(node, planner)
    assert len(moves) > 10
    assert max(at[2] - to[2] for at, to in moves) <= DESCENT_STEP_M + 1e-9


@dataclass
class _SlipsThenRetries(_RetriesAfter):
    """While the model thinks, the plug slips `slip_m` off the TCP axis in the gripper; the
    answer is still retry."""

    slip_m: float = 0.0015

    async def triage(self, ctx: FailureContext) -> object:
        self.node.plug_xy_offset_m = (self.slip_m, 0.0)
        return await super().triage(ctx)


def test_a_retry_that_would_insert_off_the_port_axis_is_rejected_on_arrival(
    store: EventStore,
) -> None:
    # insert_scripted runs only on a valid wrench with the plug within 1 mm of the port axis,
    # where align leaves it. Here the plug slips 1.5 mm in the gripper while the model thinks:
    # its retry would push the plug in off the axis, so it is rejected when it arrives, and
    # insertion does not run again.
    node = FakeNode(task=TASK)
    planner = _SlipsThenRetries(node, 8.0)

    ok = run_goal(node, TASK, store, setting="C", planner=planner, fault=_dropout(15.0))

    assert ok is False
    events = _events(store)
    rejected = [e.payload for e in events if isinstance(e.payload, PlannerOutputRejected)]
    assert rejected and rejected[0].errors == ["skill: preconditions_not_met"]
    called = next(i for i, e in enumerate(events) if isinstance(e.payload, PlannerCalled))
    assert not [
        e
        for e in events[called:]
        if isinstance(e.payload, SkillStarted) and e.payload.skill == "insert_scripted"
    ]


def test_a_retry_after_a_dropout_does_not_throw_the_arm_sideways(store: EventStore) -> None:
    # While the link is down the XY integrator adds the plug's 0.8 mm error with every lost
    # command, up to its limit: 7.5 mm of correction the arm never got. The retry starts the
    # integrator from where the arm is, so no target lies more than 0.5 mm to the side.
    node = _Slips(task=TASK)
    planner = _RetriesAfter(node, 8.0)

    ok = run_goal(node, TASK, store, setting="C", planner=planner, fault=_dropout(15.0))

    assert ok is True
    moves = _moves_after_the_answer(node, planner)
    assert len(moves) > 10
    assert max(math.hypot(to[0] - at[0], to[1] - at[1]) for at, to in moves) <= 0.0005


def test_the_fault_is_part_of_the_runs_configuration(tmp_path: Any) -> None:
    def started(fault: CommandDropout | None) -> RunStarted:
        name = "none" if fault is None else f"{fault.duration_s}"
        with EventStore(tmp_path / f"{name}.sqlite") as store:
            run_goal(FakeNode(task=TASK), TASK, store, fault=fault)
            (run,) = _payloads(store, RunStarted)
            return run

    plain, short, long = started(None), started(_dropout(3.0)), started(_dropout(30.0))

    assert len({plain.config_hash, short.config_hash, long.config_hash}) == 3
    # The fault is part of the scenario, as a mock scenario holds its faults.
    assert len({plain.config_parts["scenario"], short.config_parts["scenario"]}) == 2


def test_a_fault_that_starts_on_the_runs_last_sample_is_still_recorded(
    tmp_path: Any,
) -> None:
    # The onset is decided at a sample and reported after the next step; when the run ends
    # right at that sample (here its time limit), the end of the run records it.
    with EventStore(tmp_path / "first.sqlite") as store:
        run_goal(FakeNode(task=TASK), TASK, store, fault=_dropout(3.0))
        events = _events(store)
    on = next(i for i, e in enumerate(events) if isinstance(e.payload, FaultInjected))
    onset_s = [e.ts for e in events[:on] if isinstance(e.payload, SignalSampled)][-1]
    short = Task(id="trial_1_task_1", time_limit=onset_s)

    with EventStore(tmp_path / "short.sqlite") as store:
        assert run_goal(FakeNode(task=short), short, store, fault=_dropout(3.0)) is False
        assert _end(store).reason == "time limit reached"
        (injected,) = _payloads(store, FaultInjected)
        assert injected.params == {"onset_height_mm": 20.0, "duration_s": 3.0}


# --- choosing it from the environment ----------------------------------------------------------


def test_no_fault_unless_the_environment_names_one() -> None:
    assert fault_setup({}) is None
    assert fault_setup({"CELLRT_FAULT": "  "}) is None


def test_the_environment_names_the_fault_and_the_goals_that_get_it() -> None:
    spec = fault_setup(
        {
            "CELLRT_FAULT": json.dumps(
                {"kind": "command_dropout", "onset_height_mm": 20, "duration_s": 15, "goals": [2]}
            )
        }
    )

    assert isinstance(spec, FaultSpec)
    assert spec.for_goal(1) is None
    assert spec.for_goal(2) == _dropout(15.0)


def test_without_goals_every_goal_gets_the_fault() -> None:
    spec = fault_setup({"CELLRT_FAULT": '{"kind": "command_dropout", "duration_s": 15}'})

    assert spec is not None
    assert spec.for_goal(1) == spec.for_goal(7) == _dropout(15.0)  # 20 mm by default


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[1]",
        '{"kind": "sensor_dropout", "duration_s": 1}',
        '{"kind": "command_dropout"}',  # no duration
        '{"kind": "command_dropout", "duration_s": 0.04}',
        '{"kind": "command_dropout", "duration_s": 120.01}',
        '{"kind": "command_dropout", "duration_s": 1, "onset_height_mm": 0.99}',
        '{"kind": "command_dropout", "duration_s": 1, "onset_height_mm": 40.01}',
        '{"kind": "command_dropout", "duration_s": 1, "goals": [0]}',
        '{"kind": "command_dropout", "duration_s": 1, "goals": ' + str(list(range(1, 102))) + "}",
        '{"kind": "command_dropout", "duration_s": 1, "extra": "sk-not-a-key"}',
        '{"kind": "command_dropout", "duration_s": true}',  # strict: no bool as a number
        '{"kind": "command_dropout", "duration_s": "15"}',  # nor a string
        '{"kind": "command_dropout", "duration_s": 1, "goals": [true]}',
        '{"kind": "command_dropout", "duration_s": 1, "goals": [2.0]}',
        '{"kind": "command_dropout", "duration_s": 1, "goals": []}',
        '{"kind": "command_dropout", "duration_s": NaN}',
        '{"kind": "command_dropout", "duration_s": 1e400}',
        '{"kind": "command_dropout", "duration_s": 1, "x": ' + "[" * 400 + "]" * 400 + "}",
        '{"kind": "command_dropout", "duration_s": 1, "x": "' + "a" * MAX_SPEC_CHARS + '"}',
    ],
)
def test_a_bad_fault_is_refused_with_a_fixed_message(raw: str) -> None:
    with pytest.raises(PolicySetupError) as refused:
        fault_setup({"CELLRT_FAULT": raw})

    assert str(refused.value) == SPEC_REFUSED
    assert "sk-not-a-key" not in str(refused.value)


@pytest.mark.parametrize(
    "fields",
    [
        {"duration_s": 0.05, "onset_height_mm": 1.0},
        {"duration_s": 120.0, "onset_height_mm": 40.0, "goals": list(range(1, 101))},
    ],
)
def test_a_fault_at_its_bounds_is_taken(fields: dict[str, object]) -> None:
    spec = fault_setup({"CELLRT_FAULT": json.dumps({"kind": "command_dropout", **fields})})

    assert spec is not None
    assert len(json.dumps({"kind": "command_dropout", **fields})) <= MAX_SPEC_CHARS
