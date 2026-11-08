"""The one bridge between synchronous py_trees ticks and async skills.

On its first tick a `SkillBehaviour` starts the skill as an asyncio task and returns RUNNING.
Later ticks return RUNNING until the task is done, then map the result to SUCCESS / FAILURE.
If the tree interrupts the behaviour, `terminate()` cancels the task.

One skill owns the command channel at a time: each run commands through its own lease, revoked
when the skill ends or is stopped, and the executor then sends HOLD (stop adding motion;
unloading force is a recovery action, not part of the hand-over). The next skill's first
command replaces HOLD in the same step.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Coroutine, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

import py_trees
from py_trees.common import Status

from cellrt.executor.samples import signals_of
from cellrt.monitor.classifier import SAFETY_CLASSES, Monitor, MonitorConfig, Verdict
from cellrt.planner.base import FailureContext, Planner
from cellrt.planner.schemas import Decision, Rejection, validate_decision
from cellrt.runtime.clock import SimClock, to_ns
from cellrt.runtime.events import (
    CommandDropped,
    MonitorVerdict,
    PlannerCalled,
    PlannerDecided,
    PlannerOutputRejected,
    PreconditionFailed,
    SkillCancelled,
    SkillFinished,
    SkillProgress,
    SkillStarted,
)
from cellrt.runtime.recorder import Recorder
from cellrt.skills.base import Skill, SkillContext, SkillResult
from cellrt.skills.registry import SkillRegistry
from cellrt.world.base import HOLD, CommandLease, FaultSource, World, WorldView

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
        return len(self.live_names())

    def live_names(self) -> list[str]:
        self._tasks = {t for t in self._tasks if not t.done()}
        return sorted(t.get_name() for t in self._tasks)


@dataclass
class ExecEnv:
    world: World
    clock: SimClock
    recorder: Recorder
    period_s: float
    tracker: TaskTracker = field(default_factory=TaskTracker)
    monitor: Monitor = field(init=False)
    # Ground truth of injected faults, read only by the runner to record it.
    fault_source: FaultSource | None = None
    monitor_config: MonitorConfig = field(default_factory=MonitorConfig)
    # The task's time limit on the clock, so the planner can be told how much time is left.
    time_limit_s: float = math.inf

    def __post_init__(self) -> None:
        recorder = self.recorder
        self.monitor = Monitor(
            self.period_s, self.monitor_config, force_time=lambda: recorder.state.over_limit_s
        )


class SkillBehaviour(py_trees.behaviour.Behaviour):
    """Bridge for one skill. `failure` holds the last failure class (None after a success)."""

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
        self._lease: CommandLease | None = None
        self._started_ns = 0
        self._progress_mark = 0.0
        self.failure: str | None = None
        self.failure_source: Literal["monitor", "skill"] | None = None
        self.verdict: Verdict | None = None  # the Monitor's verdict, when it decided the failure
        self.attempt = 0  # which run of the skill this is (1 for the first)
        self.note: str | None = None  # why it failed when there is no failure class
        self.error: str | None = None  # exception type, if the skill raised (a bug, not a failure)

    def initialise(self) -> None:
        self._task = None
        self._progress_mark = 0.0
        self.failure = None
        self.failure_source = None
        self.verdict = None
        self.note = None
        self.error = None

    def update(self) -> Status:
        if self._task is None:
            state = self._env.recorder.state
            if not self.skill.preconditions(state):
                attempt = state.skill_attempts.get(self.skill.name, 0) + 1
                self._env.recorder.emit(PreconditionFailed(skill=self.skill.name, attempt=attempt))
                self.note = "preconditions not met"
                return Status.FAILURE
            self._start()
            return Status.RUNNING
        # The Monitor is asked first, even on the tick the skill completes. It judges the sample
        # as logged (see `executor.samples`): every verdict can be checked from the log.
        last = self._env.recorder.state.last_signal
        if last is None:
            raise RuntimeError("a skill is ticked again before any sample was logged")
        verdict = self._env.monitor.observe(signals_of(last))
        if self._task.done():
            if verdict and (verdict.failure_class in SAFETY_CLASSES or not _succeeded(self._task)):
                # A safety verdict overrides the skill's "done"; any verdict overrides the
                # skill's own account of a failure. Other verdicts lose to a success.
                self._record_verdict(verdict)
                self._finish()
                return Status.FAILURE
            result = self._finish()
            self.failure = result.failure
            self.failure_source = "skill" if result.failure else None
            if not result.ok:
                reason = result.detail.get("reason")
                self.note = reason if isinstance(reason, str) else "skill reported failure"
            return Status.SUCCESS if result.ok else Status.FAILURE
        if verdict:
            self._record_verdict(verdict)
            self._cancel("preempted")
            return Status.FAILURE
        elapsed_ns = self._env.clock.now_ns() - self._started_ns
        if elapsed_ns >= to_ns(self.skill.timeout_s):
            verdict = self._env.monitor.classify_timeout(elapsed_ns / 1e9, self.skill.timeout_s)
            self._record_verdict(verdict)
            self._cancel("timeout")
            return Status.FAILURE
        return Status.RUNNING

    def terminate(self, new_status: Status) -> None:
        if self._task is None:
            return
        if self._task.done():
            # Finished after the last tick but before the tree was stopped: keep its result.
            self._finish()
            return
        self._cancel("terminated")

    def _cancel(self, reason: Literal["terminated", "timeout", "preempted"]) -> None:
        assert self._task is not None
        self._task.cancel()
        self._task = None
        self._release()
        self._env.recorder.emit(SkillCancelled(skill=self.skill.name, reason=reason))

    def _release(self) -> None:
        """Revoke the run's command access and hold the arm still."""
        if self._lease is not None:
            self._lease.revoke()
            self._lease = None
        self._env.world.command(HOLD)

    def _record_verdict(self, verdict: Verdict) -> None:
        self.failure = verdict.failure_class.value
        self.failure_source = "monitor"
        self.verdict = verdict
        self._env.recorder.emit(
            MonitorVerdict(
                skill=self.skill.name,
                failure_class=verdict.failure_class.value,
                rule=verdict.rule,
                evidence=verdict.evidence,
            )
        )

    def _finish(self) -> SkillResult:
        assert self._task is not None and self._task.done()
        task, self._task = self._task, None
        self._release()
        result, self.error = _result_of(task)
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
        self.attempt = attempt
        env.recorder.emit(SkillStarted(skill=self.skill.name, attempt=attempt, params=self._params))
        self._started_ns = env.clock.now_ns()
        env.monitor.reset(self.skill.monitor_profile)
        skill = self.skill.name
        self._lease = CommandLease(
            on_dropped=lambda: env.recorder.emit(CommandDropped(skill=skill, attempt=attempt))
        )
        lease = self._lease
        ctx = SkillContext(
            world=WorldView(env.world, lease),
            clock=env.clock,
            period_s=env.period_s,
            # Revoked with the commands: a stopped skill cannot report progress either, which
            # would land on the node's next attempt.
            progress=lambda fraction: self._progress(fraction) if lease.active else None,
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


class PlannerBehaviour(py_trees.behaviour.Behaviour):
    """Bridge for one planner call, on the same task mechanism as skills.

    `ask` sets the failure before the node runs. On its first tick it records `planner_called`
    and starts `triage` as a task; when the task is done it validates the output: a valid
    decision is recorded (`planner_decided`) and the node succeeds with `decision` set; anything
    else, a planner that raised included, is recorded (`planner_output_rejected`) and the node
    fails with `rejection` set.
    """

    def __init__(
        self,
        planner: Planner,
        registry: SkillRegistry,
        env: ExecEnv,
        options: frozenset[str],
        *,
        name: str = "planner",
    ) -> None:
        super().__init__(name)
        self._planner = planner
        self._registry = registry
        self.options = options  # skills a SwitchSkill may name for this step
        self._env = env
        self._task: asyncio.Task[object] | None = None
        self._ctx: FailureContext | None = None
        self.decision: Decision | None = None
        self.rejection: Rejection | None = None

    def ask(self, ctx: FailureContext) -> None:
        self._ctx = ctx

    def catalogue(self) -> dict[str, str]:
        """Registered skills and the Monitor profile each declares, for the planner's context."""
        return {n: self._registry.get(n).monitor_profile for n in sorted(self._registry.names())}

    def initialise(self) -> None:
        self._task = None
        self.decision = None
        self.rejection = None

    def update(self) -> Status:
        ctx = self._ctx
        assert ctx is not None, "ask() must set the failure before the planner node runs"
        if self._task is None:
            call = self._env.recorder.state.planner_calls + 1
            self._env.recorder.emit(
                PlannerCalled(
                    skill=ctx.skill,
                    failure_class=ctx.failure_class,
                    call=call,
                    context=ctx.as_dict(),
                )
            )
            self._task = self._env.tracker.spawn(self._planner.triage(ctx), name=self.name)
            return Status.RUNNING
        if not self._task.done():
            return Status.RUNNING
        task, self._task = self._task, None
        try:
            raw = task.result()
        except (Exception, asyncio.CancelledError) as exc:  # the type only: text may hold anything
            result: Decision | Rejection = Rejection(
                "", (f"<root>: planner_raised {type(exc).__name__}",)
            )
        else:
            try:
                result = validate_decision(raw, self._registry, ctx.skill, self.options)
            except Exception as exc:  # validation must not crash the run; only the type
                result = Rejection("", (f"<root>: validator_raised {type(exc).__name__}",))
        if isinstance(result, Rejection):
            self.rejection = result
            self._env.recorder.emit(
                PlannerOutputRejected(skill=ctx.skill, raw=result.raw, errors=list(result.errors))
            )
            return Status.FAILURE
        self.decision = result
        self._env.recorder.emit(PlannerDecided(skill=ctx.skill, decision=result.model_dump()))
        return Status.SUCCESS

    def terminate(self, new_status: Status) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        if not task.done():
            task.cancel()
        elif not task.cancelled():
            # Stopped before its result was read: mark the exception retrieved, so asyncio does
            # not print it (with its text) when the task is collected.
            task.exception()


def _succeeded(task: asyncio.Task[SkillResult]) -> bool:
    """Whether a finished skill task returned a successful result."""
    return not task.cancelled() and task.exception() is None and task.result().ok


def _result_of(task: asyncio.Task[SkillResult]) -> tuple[SkillResult, str | None]:
    """The skill's result, and the exception type if it raised.

    Only the type is recorded: an exception's text may hold data. A raising skill never crashes
    the loop; the run is aborted instead (see `executor.runner`). Nor does one that ended
    cancelled on its own (the executor clears the task before it cancels one).
    """
    try:
        return task.result(), None
    except (Exception, asyncio.CancelledError) as exc:
        error = type(exc).__name__
        return SkillResult(ok=False, detail={"error": error}), error
