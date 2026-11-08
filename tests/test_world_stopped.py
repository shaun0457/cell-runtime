"""A world that cannot go on (a stalled or reset sim clock, lost ground truth) raises
`WorldStopped` from `step`; the run then ends aborted with the world's reason code, through the
same stop as the time limit: the running skill is cancelled and HOLD is sent."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from cellrt.eval import runner
from cellrt.eval.runner import run_unit
from cellrt.eval.scenarios import Scenario, load_scenarios
from cellrt.runtime.events import RunFinished, SignalSampled, SkillCancelled
from cellrt.runtime.store import EventStore
from cellrt.world.base import HOLD, Command, WorldStopped
from cellrt.world.mock_world import MockWorld

PHASE0 = Path(__file__).resolve().parents[1] / "eval" / "scenarios" / "phase0.yaml"
BASELINE: Scenario = next(s for s in load_scenarios(PHASE0) if s.id == "baseline")
STOP_AT_STEP = 5


class _StoppingMockWorld(MockWorld):
    """The mock, whose sim clock "stalls" on step STOP_AT_STEP; records every command."""

    commands: ClassVar[list[Command]] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._stop_count = 0  # MockWorld has a _steps of its own
        type(self).commands = []

    def command(self, cmd: Command) -> None:
        type(self).commands.append(cmd)
        super().command(cmd)

    def step(self, dt: float) -> float:
        self._stop_count += 1
        if self._stop_count == STOP_AT_STEP:
            raise WorldStopped("sim_clock_stalled")
        return super().step(dt)


def test_a_world_that_stops_ends_the_run_aborted_with_its_reason(
    store: EventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "MockWorld", _StoppingMockWorld)
    events = store.events(run_unit(BASELINE, "A", 0, store).run_id)

    end = events[-1].payload
    assert isinstance(end, RunFinished)
    assert (end.outcome, end.reason) == ("aborted", "sim_clock_stalled")
    samples = [e for e in events if isinstance(e.payload, SignalSampled)]
    assert len(samples) == STOP_AT_STEP - 1  # no sample for the step that did not happen
    cancelled = [e.payload for e in events if isinstance(e.payload, SkillCancelled)]
    assert [(c.skill, c.reason) for c in cancelled] == [("approach", "terminated")]
    assert _StoppingMockWorld.commands[-1] == HOLD


@pytest.mark.parametrize("reason", ["", "Sim clock stalled", "stalled: /clock", "x" * 65, "9x"])
def test_a_stop_reason_must_be_a_short_lowercase_code(reason: str) -> None:
    with pytest.raises(ValueError):
        WorldStopped(reason)


def test_a_stop_reason_keeps_the_code() -> None:
    assert WorldStopped("sim_time_reset").reason == "sim_time_reset"
