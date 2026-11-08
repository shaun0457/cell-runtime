"""The one bridge between synchronous py_trees ticks and async skills.

On its first tick a `SkillBehaviour` starts the skill as an asyncio task and returns RUNNING.
Later ticks return RUNNING until the task is done, then map the result to SUCCESS / FAILURE.
If the tree interrupts the behaviour, `terminate()` cancels the task.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Mapping
from dataclasses import dataclass, field
from typing import Any

import py_trees
from py_trees.common import Status

from cellrt.runtime.clock import SimClock
from cellrt.runtime.events import SkillCancelled, SkillFinished, SkillProgress, SkillStarted
from cellrt.runtime.recorder import Recorder
from cellrt.skills.base import Skill, SkillContext, SkillResult
from cellrt.world.base import World

PROGRESS_STEP = 0.25


class TaskTracker:
    """Every task the tree starts, so the loop can tell when all of them wait on the clock.

    The loop compares the number of live tasks with the number of pending clock sleeps, so two
    rules hold for everything that runs on the loop: every task is started through `spawn`, and
    a task waits on at most one clock sleep at a time and on nothing else.
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def spawn(self, coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._tasks.add(task)
        return task

    def live(self) -> int:
        self._tasks = {t for t in self._tasks if not t.done()}
        return len(self._tasks)


@dataclass
class ExecEnv:
    world: World
    clock: SimClock
    recorder: Recorder
    period_s: float
    tracker: TaskTracker = field(default_factory=TaskTracker)


class SkillBehaviour(py_trees.behaviour.Behaviour):
    def __init__(
        self,
        skill: Skill,
        env: ExecEnv,
        *,
        params: Mapping[str, Any] | None = None,
        name: str | None = None,
    ) -> None:
        super().__init__(name or skill.name)
        self.skill = skill
        self._env = env
        self._params = dict(params or {})
        self._task: asyncio.Task[SkillResult] | None = None
        self._progress_mark = 0.0

    def initialise(self) -> None:
        self._task = None
        self._progress_mark = 0.0

    def update(self) -> Status:
        if self._task is None:
            self._start()
            return Status.RUNNING
        if not self._task.done():
            return Status.RUNNING
        result = self._finish()
        return Status.SUCCESS if result.ok else Status.FAILURE

    def terminate(self, new_status: Status) -> None:
        if self._task is None:
            return
        if self._task.done():
            # Finished after the last tick but before the tree was stopped: keep its result.
            self._finish()
            return
        self._task.cancel()
        self._task = None
        self._env.recorder.emit(SkillCancelled(skill=self.skill.name, reason="terminated"))

    def _finish(self) -> SkillResult:
        assert self._task is not None and self._task.done()
        task, self._task = self._task, None
        result = _result_of(task)
        self._env.recorder.emit(
            SkillFinished(
                skill=self.skill.name,
                ok=result.ok,
                failure=result.failure,
                detail=dict(result.detail),
            )
        )
        return result

    def _start(self) -> None:
        env = self._env
        attempt = env.recorder.state.skill_attempts.get(self.skill.name, 0) + 1
        env.recorder.emit(SkillStarted(skill=self.skill.name, attempt=attempt, params=self._params))
        ctx = SkillContext(
            world=env.world,
            clock=env.clock,
            period_s=env.period_s,
            progress=self._progress,
            params=self._params,
        )
        self._task = env.tracker.spawn(self.skill.run(ctx), name=self.skill.name)

    def _progress(self, fraction: float) -> None:
        """Record progress at most once per `PROGRESS_STEP`, plus completion."""
        fraction = max(0.0, min(1.0, fraction))
        if fraction >= self._progress_mark + PROGRESS_STEP or (
            fraction == 1.0 and self._progress_mark < 1.0
        ):
            self._progress_mark = fraction
            self._env.recorder.emit(SkillProgress(skill=self.skill.name, fraction=fraction))


def _result_of(task: asyncio.Task[SkillResult]) -> SkillResult:
    """A skill that raises fails; only the exception type is recorded (its text may hold data)."""
    try:
        return task.result()
    except Exception as exc:  # any skill error becomes a failed result, never a crash
        return SkillResult(ok=False, detail={"error": type(exc).__name__})
