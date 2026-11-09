"""Settings B and C on the AIC adapter (#59): the mock's trees from the AIC skills, the executor
in `live` mode, and the setting and planner from the environment. On a fake cell whose arm
follows the commanded targets."""

from __future__ import annotations

import asyncio
import itertools
import logging
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aic_fakes import FakeNode
from nim_fakes import FAKE_KEY, FakeCreate, completion

from adapters.aic.convert import MotionCommand, Pose
from adapters.aic.geometry import gripper_target, z_offset_at
from adapters.aic.run import PolicySetupError, policy_setup, run_goal
from adapters.aic.skills import (
    ALIGN_HEIGHT_M,
    RETRACT_M,
    descent_from,
    descent_offsets,
    speed_scale,
)
from cellrt.executor.recovery import RecoveryConfig
from cellrt.monitor.classifier import MonitorConfig
from cellrt.planner.base import FailureContext
from cellrt.planner.fixed_planner import FixedPlanner
from cellrt.runtime.events import (
    Event,
    MonitorVerdict,
    PlannerCalled,
    PlannerDecided,
    RecoveryStarted,
    RunFinished,
    RunStarted,
    SignalSampled,
    SkillFinished,
    SkillStarted,
)
from cellrt.runtime.store import EventStore
from cellrt.runtime.task import Task

TASK = Task(id="trial_1_task_1", time_limit=180.0)
SC_JAM_M = 0.01346  # #28: the SC plug stuck at its entrance
BLOCKED_M = 0.006  # a plug that stops 6 mm above the port, under no force to speak of


def _events(store: EventStore) -> list[Event]:
    (run,) = store.runs()
    return store.events(run.run_id)


def _end(store: EventStore) -> RunFinished:
    end = _events(store)[-1].payload
    assert isinstance(end, RunFinished)
    return end


def _payloads(store: EventStore, kind: type) -> list[Any]:
    return [e.payload for e in _events(store) if isinstance(e.payload, kind)]


# --- the descent from where it is ----------------------------------------------------------


def test_run_once_at_full_speed_the_descent_is_cheatcodes_own() -> None:
    align, insert = descent_offsets()

    assert descent_from(align, None) == align
    assert descent_from(insert, align[-1]) == insert
    assert descent_from(insert, ALIGN_HEIGHT_M) == insert  # align ended correcting XY at 10 mm


def test_after_a_retract_the_descent_goes_on_from_the_new_height() -> None:
    align, _ = descent_offsets()

    after = descent_from(align, 0.0145)

    # 0.5 mm steps from 14.5 mm, ending on align's last offset (10 mm).
    assert after == pytest.approx(
        [0.014, 0.0135, 0.013, 0.0125, 0.012, 0.0115, 0.011, 0.0105, 0.01]
    )
    assert after[-1] == align[-1]


def test_a_slow_descent_takes_quarter_millimetre_steps() -> None:
    _, insert = descent_offsets()

    slow = descent_from(insert, ALIGN_HEIGHT_M, 0.5)

    # 25 mm from 10 mm to -15 mm, 0.25 mm per step.
    assert len(slow) == 100
    assert slow[0] == pytest.approx(0.00975)
    assert all(a - b == pytest.approx(0.00025) for a, b in itertools.pairwise(slow))
    assert slow[-1] == insert[-1]


def test_a_descent_from_below_its_end_goes_back_up_to_the_end() -> None:
    align, _ = descent_offsets()

    assert descent_from(align, 0.005) == [align[-1]]


# --- setting B -------------------------------------------------------------------------------


def test_setting_b_recovers_a_stuck_insert_with_retract_align_and_insert_again(
    store: EventStore,
) -> None:
    # #28's SC plug jammed at its entrance under 12.5 N: INSERT_STUCK, whose plan runs retract
    # and align before the insert again, as often as its budget (2) allows; then the step fails.
    node = FakeNode(task=TASK, seat_height_m=SC_JAM_M, raw_force=(0.0, 0.0, 13.5))

    assert run_goal(node, TASK, store, setting="B") is False

    (started,) = _payloads(store, RunStarted)
    assert started.setting == "B"
    assert _events(store)[0].run_id.endswith("/B/0/a1")
    recoveries = [(r.failure_class, r.action, r.source) for r in _payloads(store, RecoveryStarted)]
    assert recoveries == [("INSERT_STUCK", "retract_realign", "monitor")] * 2
    skills = [s.skill for s in _payloads(store, SkillStarted)]
    assert skills == [
        "approach",
        "align",
        *["insert_scripted", "retract", "align"] * 2,
        "insert_scripted",
    ]
    assert _end(store).reason == (
        "insert_scripted failed: INSERT_STUCK (INSERT_STUCK recovery budget used up)"
    )


def test_setting_b_runs_align_again_at_half_speed_after_a_force_limit(store: EventStore) -> None:
    # Over 20 N for 0.1 s, 10 s into the goal (half way down align); with the trigger at 0.05 s
    # the Monitor calls FORCE_LIMIT. The plan: retract, then align again at half speed, which
    # goes on down from the retracted height in 0.25 mm steps instead of 0.5 mm.
    start_s = 100.0  # the fake's sim time when the goal arrives

    def squeeze(stamp: float) -> tuple[float, float, float]:
        return (0.0, 0.0, 26.0) if start_s + 10.0 <= stamp < start_s + 10.1 else (0.0, 0.0, 1.5)

    node = FakeNode(task=TASK, sim_s=start_s, force_at=squeeze)
    one_sample = MonitorConfig(force_trigger_s=0.05)

    run_goal(node, TASK, store, setting="B", monitor=one_sample)

    recoveries = [(r.skill, r.failure_class, r.action) for r in _payloads(store, RecoveryStarted)]
    assert recoveries[0] == ("align", "FORCE_LIMIT", "retract_slow")
    events = _events(store)
    starts = [i for i, e in enumerate(events) if isinstance(e.payload, SkillStarted)]
    names = [events[i].payload.skill for i in starts]
    assert names[:4] == ["approach", "align", "retract", "align"]
    slow_from = starts[3]
    slow_to = next(
        i for i in range(slow_from, len(events)) if isinstance(events[i].payload, SkillFinished)
    )
    retracted = [e.payload for e in events[:slow_from] if isinstance(e.payload, SignalSampled)][-1]
    heights = [retracted.height_mm] + [
        e.payload.height_mm
        for e in events[slow_from:slow_to]
        if isinstance(e.payload, SignalSampled) and e.payload.height_mm > 1.0
    ]
    drops = [a - b for a, b in itertools.pairwise(heights)]
    assert len(drops) > 10
    # From where the retract left the plug, with no jump (down to where align had got to, or
    # back up to the approach height): the first step may come a tick late, then 0.25 mm each.
    assert -0.01 <= drops[0] <= 0.26
    assert all(d == pytest.approx(0.25, abs=0.01) for d in drops[1:])


def test_the_z_offset_at_a_pose_commands_that_pose() -> None:
    port = Pose(position=(0.4, 0.1, 0.05), orientation=(1.0, 0.0, 0.0, 0.0))
    plug = Pose(position=(0.401, 0.099, 0.12), orientation=(1.0, 0.0, 0.0, 0.0))
    gripper = Pose(position=(0.401, 0.099, 0.17), orientation=(1.0, 0.0, 0.0, 0.0))

    z_offset = z_offset_at(port, plug, gripper)
    target, _ = gripper_target(port, plug, gripper, z_offset_m=z_offset)

    # CheatCode's target is port z + z_offset - (gripper z - plug z): 0.05 + 0.17 - 0.05.
    assert z_offset == pytest.approx(0.17)
    assert target.position[2] == pytest.approx(gripper.position[2])


@dataclass
class _Floor(FakeNode):
    """An arm that cannot go below `floor_z`: the TCP stops there while its targets go on
    down, as a pressed plug holds the TCP above its target under the sim's stiffness."""

    floor_z: float = 0.18

    def send(self, motion: MotionCommand) -> None:
        super().send(motion)
        x, y, z = self.tcp.position
        if z < self.floor_z:
            self.tcp = Pose(position=(x, y, self.floor_z), orientation=self.tcp.orientation)


def _retract_top(z: list[float]) -> int:
    """The index of the last target of the first run of 0.5 mm rises: the retract's top."""
    rise = RETRACT_M / 10

    def rises(i: int) -> bool:
        return z[i] - z[i - 1] == pytest.approx(rise, abs=1e-9)

    i = next(i for i in range(1, len(z)) if rises(i))
    first = i
    while i + 1 < len(z) and rises(i + 1):
        i += 1
    assert i - first >= 8  # the retract's ten targets, nine rises at least
    return i


def test_after_a_retract_the_slow_align_goes_on_from_where_the_arm_is(store: EventStore) -> None:
    # In the sim a pressed plug holds the TCP F/k above its target (90 N/m), so the z_offset last
    # commanded can lie far below the arm. Here the arm stops at z 0.18 while align's targets go
    # on down, and 26 N for 0.6 s calls FORCE_LIMIT. The retract rises 5 mm from where the arm
    # is, and align at half speed goes on 0.25 mm below that, not from below the stopped arm.
    start_s = 100.0

    def squeeze(stamp: float) -> tuple[float, float, float]:
        return (0.0, 0.0, 26.0) if start_s + 10.0 <= stamp < start_s + 10.6 else (0.0, 0.0, 1.5)

    node = _Floor(task=TASK, sim_s=start_s, force_at=squeeze, floor_z=0.18)

    run_goal(node, TASK, store, setting="B", monitor=MonitorConfig(force_trigger_s=0.05))

    recoveries = [(r.skill, r.failure_class, r.action) for r in _payloads(store, RecoveryStarted)]
    assert recoveries[0] == ("align", "FORCE_LIMIT", "retract_slow")
    z = [m.position_m[2] for m in node.sent if m.mode == "position"]
    top = _retract_top(z)
    assert z[top] == pytest.approx(0.18 + 0.005, abs=1e-6)  # 5 mm above where the arm stopped
    after = next(v for v in z[top + 1 :] if v != pytest.approx(z[top], abs=1e-9))
    assert after == pytest.approx(z[top] - 0.00025, abs=1e-6)


@pytest.mark.parametrize("scale", [0.5, 0.75, 1.0])
def test_a_skill_takes_a_speed_scale_from_half_to_full(scale: float) -> None:
    assert speed_scale(SimpleNamespace(params={"speed_scale": scale})) == scale  # type: ignore[arg-type]


@pytest.mark.parametrize("scale", [0.49, 1.01])
def test_a_skill_refuses_a_speed_scale_outside_half_to_full(scale: float) -> None:
    with pytest.raises(ValueError, match="speed_scale out of range"):
        speed_scale(SimpleNamespace(params={"speed_scale": scale}))  # type: ignore[arg-type]


def test_a_recovery_slower_than_the_skills_allow_is_refused_before_the_run(
    store: EventStore,
) -> None:
    too_slow = RecoveryConfig(slow_speed_scale=0.25)

    with pytest.raises(ValueError, match="slow_speed_scale"):
        run_goal(FakeNode(task=TASK), TASK, store, setting="B", recovery=too_slow)
    assert store.runs() == []


# --- setting C -------------------------------------------------------------------------------


async def _next_turn() -> None:
    """Let the loop run everything else once."""
    loop = asyncio.get_running_loop()
    turn: asyncio.Future[None] = loop.create_future()
    loop.call_soon(turn.set_result, None)
    await turn


@dataclass
class _AbortsAfter:
    """Answers abort once the cell's sim time has moved on `sim_s` since the call."""

    node: FakeNode
    sim_s: float = 0.5
    deadline_s: float = 30.0
    # What the arm was sent while the call ran.
    sent_during: list[MotionCommand] | None = None

    async def triage(self, ctx: FailureContext) -> object:
        until = self.node.sim_s + self.sim_s
        first = len(self.node.sent)
        while self.node.sim_s < until:
            await _next_turn()
        self.sent_during = self.node.sent[first:]
        return {"kind": "abort", "reason": "blocked"}


def test_setting_c_asks_the_planner_on_an_unknown_while_the_cell_keeps_stepping(
    store: EventStore,
) -> None:
    # A plug that stops 6 mm above the port with no force behind it: no progress for 1 s and
    # no rule that explains it, so the Monitor calls it UNKNOWN and setting C asks the planner.
    # The arm holds while it thinks; the sim goes on and every step is recorded.
    node = FakeNode(task=TASK, seat_height_m=BLOCKED_M)
    planner = _AbortsAfter(node)

    assert run_goal(node, TASK, store, setting="C", planner=planner) is False

    # Nothing is sent while the call runs: the arm stays on the HOLD sent when the skill stopped.
    assert planner.sent_during == []
    (verdict,) = _payloads(store, MonitorVerdict)
    assert (verdict.skill, verdict.failure_class, verdict.rule) == (
        "insert_scripted",
        "UNKNOWN",
        "insert_stalled",
    )
    events = _events(store)
    called = next(i for i, e in enumerate(events) if isinstance(e.payload, PlannerCalled))
    decided = next(i for i, e in enumerate(events) if isinstance(e.payload, PlannerDecided))
    during = [
        e.payload.type for e in events[called + 1 : decided] if e.payload.type != "bt_node_changed"
    ]
    assert len(during) >= 10 and set(during) == {"signal_sampled"}  # 0.5 s of steps, nothing else
    assert events[decided].payload.waited_sim_s == pytest.approx(len(during) * 0.05)
    assert _end(store).reason == "insert_scripted failed: UNKNOWN (planner aborted: blocked)"


def test_the_setting_and_planner_count_in_the_config_hash(tmp_path: Path) -> None:
    def started(setting: str) -> RunStarted:
        with EventStore(tmp_path / f"{setting}.sqlite") as store:
            run_goal(FakeNode(task=TASK), TASK, store, setting=setting)  # type: ignore[arg-type]
            (run,) = _payloads(store, RunStarted)
            return run

    a, b, c = started("A"), started("B"), started("C")

    assert len({a.config_hash, b.config_hash, c.config_hash}) == 3
    assert a.config_parts["recovery"] != b.config_parts["recovery"]
    assert a.config_parts["planner"] == b.config_parts["planner"] != c.config_parts["planner"]


# --- the setting and planner from the environment ------------------------------------------


def test_the_policy_runs_setting_a_by_default() -> None:
    assert policy_setup({}) == ("A", None)


@pytest.mark.parametrize(
    ("environ", "message"),
    [
        ({"CELLRT_SETTING": "D"}, "CELLRT_SETTING must be one of: A, B, C"),
        ({"CELLRT_SETTING": "b"}, "CELLRT_SETTING must be one of: A, B, C"),
        (
            {"CELLRT_SETTING": "C", "CELLRT_PLANNER": "gpt; rm -rf /"},
            "CELLRT_PLANNER must be one of: rule, fixed, llm",
        ),
        (
            {"CELLRT_SETTING": "A", "CELLRT_PLANNER": "nim"},
            "CELLRT_PLANNER must be one of: rule, fixed, llm",
        ),
    ],
)
def test_a_bad_setting_or_planner_is_refused_with_a_fixed_message(
    environ: dict[str, str], message: str
) -> None:
    with pytest.raises(PolicySetupError) as refused:
        policy_setup(environ)

    assert str(refused.value) == message


def test_settings_a_and_b_build_no_planner_and_need_no_key() -> None:
    assert policy_setup({"CELLRT_SETTING": "B", "CELLRT_PLANNER": "llm"}) == ("B", None)


def test_setting_c_builds_the_planner_it_names() -> None:
    setting, planner = policy_setup({"CELLRT_SETTING": "C", "CELLRT_PLANNER": "fixed"})

    assert setting == "C"
    assert isinstance(planner, FixedPlanner)


def _connect(create: FakeCreate) -> Any:
    def connect(*, base_url: str, api_key: str, timeout_s: float) -> FakeCreate:
        return create

    return connect


def test_the_model_planner_without_a_key_is_refused_and_says_where_the_key_goes() -> None:
    with pytest.raises(PolicySetupError) as refused:
        policy_setup(
            {"CELLRT_SETTING": "C", "CELLRT_PLANNER": "llm"}, connect=_connect(FakeCreate([]))
        )

    assert "NVIDIA_API_KEY" in str(refused.value)


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        (completion(arguments='{"kind": "abort", "reason": "blocked"}'), "aborted: blocked"),
        (  # the SDK quoting the key in its error text
            RuntimeError(f"Illegal header value b'Bearer {FAKE_KEY}'"),
            "output rejected",
        ),
        (
            completion(arguments=f'{{"kind": "abort", "reason": "{FAKE_KEY}"}}', content=FAKE_KEY),
            "aborted: <redacted>",
        ),
    ],
    ids=["answers", "sdk_error_quotes_the_key", "model_echoes_the_key"],
)
def test_the_model_planners_key_reaches_neither_the_store_nor_the_log(
    answer: object, outcome: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    create = FakeCreate([answer])
    environ = {"CELLRT_SETTING": "C", "CELLRT_PLANNER": "llm", "NVIDIA_API_KEY": FAKE_KEY}
    setting, planner = policy_setup(environ, connect=_connect(create))
    node = FakeNode(task=TASK, seat_height_m=BLOCKED_M)

    with caplog.at_level(logging.DEBUG), EventStore(tmp_path / "aic.sqlite") as store:
        assert run_goal(node, TASK, store, setting=setting, planner=planner) is False
        assert _end(store).reason == f"insert_scripted failed: UNKNOWN (planner {outcome})"

    assert len(create.calls) == 1  # the model was asked
    stored = b"".join(path.read_bytes() for path in tmp_path.iterdir())
    assert FAKE_KEY.encode() not in stored
    assert FAKE_KEY not in caplog.text
    assert all(FAKE_KEY not in message for _, message in node.feedback)
