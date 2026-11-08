"""Hand-over between skills: one skill owns the command channel at a time.

When the executor stops a skill (preemption, timeout), it revokes that skill's access to the
world and sends HOLD before the next skill runs, so a stopped skill cannot keep the arm moving.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from cellrt.eval.runner import run_unit
from cellrt.eval.scenarios import Scenario
from cellrt.monitor.profiles import MonitorProfile
from cellrt.replay import replay_lines
from cellrt.runtime.events import Event
from cellrt.runtime.state import RunState
from cellrt.runtime.store import EventStore
from cellrt.skills.base import SkillContext, SkillResult
from cellrt.skills.mock import Align, InsertScripted, Retract, Verify, WaitSensor
from cellrt.skills.registry import SkillRegistry
from cellrt.world.base import Velocity

QUIET_STEPS = 6


@dataclass
class _DescendsThenHangs:
    """First run: descends fast and never finishes (so it times out). Second run: sends no
    command at all for a few steps, then gives up, so whatever moves the plug meanwhile is
    whatever the first run left behind."""

    command_after_cancel: bool = False
    # Instead of hanging, the first run gives up on its own (still moving) with a class the
    # recovery table retries.
    gives_up_moving: bool = False
    runs: list[int] = field(default_factory=list)
    name: str = "approach"
    monitor_profile: MonitorProfile = "motion"
    timeout_s: float = 0.5

    def preconditions(self, state: RunState) -> bool:
        return True

    async def run(self, ctx: SkillContext) -> SkillResult:
        self.runs.append(len(self.runs) + 1)
        if len(self.runs) == 1:
            ctx.world.command(Velocity(vz=-20.0))
            if self.gives_up_moving:
                await ctx.clock.sleep(ctx.period_s)
                return SkillResult(ok=False, failure="TIMEOUT")
            try:
                while True:
                    await ctx.clock.sleep(ctx.period_s)
            except asyncio.CancelledError:
                if self.command_after_cancel:  # breaks the skill contract on purpose
                    for _ in range(3):
                        ctx.world.command(Velocity(vz=-20.0))
                raise
        for _ in range(QUIET_STEPS):
            await ctx.clock.sleep(ctx.period_s)
        return SkillResult(ok=False, detail={"reason": "probe done"})


def _run(store: EventStore, probe: _DescendsThenHangs) -> list[Event]:
    registry = SkillRegistry([probe, Align(), InsertScripted(), Verify(), Retract(), WaitSensor()])
    run = run_unit(Scenario(id="handover"), "B", seed=0, store=store, registry=registry)
    return store.events(run.run_id)


def _heights_during_second_run(events: list[Event]) -> list[float]:
    started = [e.seq for e in events if e.type == "skill_started"][1]
    finished = next(e.seq for e in events if e.type == "skill_finished" and e.seq > started)
    return [
        e.payload.height_mm
        for e in events
        if e.type == "signal_sampled" and started < e.seq < finished
    ]


def test_a_timed_out_skill_leaves_the_plug_held_not_moving(store: EventStore) -> None:
    events = _run(store, _DescendsThenHangs())

    assert [(e.payload.skill, e.payload.reason) for e in events if e.type == "skill_cancelled"] == [
        ("approach", "timeout")
    ]
    heights = _heights_during_second_run(events)
    assert len(heights) >= QUIET_STEPS
    assert len(set(heights)) == 1  # held: no motion left over from the cancelled run


def test_a_skill_that_ends_on_its_own_while_moving_also_leaves_the_plug_held(
    store: EventStore,
) -> None:
    events = _run(store, _DescendsThenHangs(gives_up_moving=True))

    assert [e.type for e in events if e.type == "skill_cancelled"] == []
    assert len(set(_heights_during_second_run(events))) == 1


def test_commands_from_a_stopped_skill_are_dropped_and_the_first_is_recorded(
    store: EventStore,
) -> None:
    events = _run(store, _DescendsThenHangs(command_after_cancel=True))  # three commands

    heights = _heights_during_second_run(events)
    assert len(set(heights)) == 1
    # Recorded against the run that sent them, not the retry that is running by then.
    assert [
        (e.payload.skill, e.payload.attempt) for e in events if e.type == "command_dropped"
    ] == [("approach", 1)]


def test_replay_shows_the_dropped_command(store: EventStore) -> None:
    events = _run(store, _DescendsThenHangs(command_after_cancel=True))

    lines = [line for line in replay_lines(events) if "command_dropped" in line]
    assert len(lines) == 1
    assert lines[0].endswith("approach attempt=1 commanded after its access was revoked (dropped)")
