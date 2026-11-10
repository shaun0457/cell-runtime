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
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, get_args

import py_trees
from py_trees.common import Status

from cellrt.executor.samples import signals_of
from cellrt.executor.wall import wall_now
from cellrt.monitor.classifier import SAFETY_CLASSES, Monitor, MonitorConfig, Verdict
from cellrt.monitor.profiles import MonitorProfile
from cellrt.planner.base import (
    MAX_STORED_PROMPT_CHARS,
    MAX_STORED_RESPONSE_CHARS,
    FailureContext,
    Planner,
    PlannerTrace,
    Traced,
    planner_deadline,
)
from cellrt.planner.schemas import (
    PRECONDITIONS_NOT_MET,
    Decision,
    Rejection,
    Retry,
    SwitchSkill,
    audit_text,
    validate_decision,
)
from cellrt.runtime.clock import SimClock, to_ns
from cellrt.runtime.events import (
    CommandDropped,
    MonitorVerdict,
    PlannerCalled,
    PlannerDecided,
    PlannerOutputRejected,
    PlannerPreempted,
    PlannerResponded,
    PlannerTimedOut,
    PreconditionFailed,
    SkillCancelled,
    SkillFinished,
    SkillProgress,
    SkillStarted,
)
from cellrt.runtime.recorder import Recorder
from cellrt.runtime.scrub import scrub
from cellrt.skills.base import Skill, SkillContext, SkillResult
from cellrt.skills.registry import SkillRegistry
from cellrt.world.base import HOLD, CommandLease, FaultSource, World, WorldView

PROGRESS_STEP = 0.25
# What the world does while a planner call runs (ADR 020): `frozen` stands still and the loop
# waits on the call in wall time (ADR 016, the mock); `live` keeps stepping while the arm holds
# (a simulator or a cell, whose time does not stop).
PlannerWait = Literal["frozen", "live"]
PLANNER_WAITS: tuple[PlannerWait, ...] = get_args(PlannerWait)
# The Monitor's rules during a live wait: the safety rules only (dropout and the force
# limit, ADR 006), which are the `motion` profile's. Holding still is no progress, so the
# insertion rules would call every hold a stall (ADR 020).
HOLD_PROFILE: MonitorProfile = "motion"
# A skill's description, as every planner call (and the log) carries it, is cut to this.
MAX_DESCRIPTION_CHARS = 300


@dataclass
class OutsideWait:
    """A task that waits on something outside the sim (a planner's network call), with its
    wall-time deadline. The loop sets `started` on the first tick the call holds the world
    (`frozen`: when it begins to wait on it; `live`: when it first checks its deadline), and
    `waited_s` when the deadline has passed; the planner node then cancels the task."""

    task: asyncio.Task[Any]
    deadline_s: float
    started: float | None = None  # wall time
    waited_s: float | None = None  # set once the deadline has passed


class TaskTracker:
    """Every task the tree starts, so the loop can tell when all of them wait on the clock.

    The loop compares the number of live tasks with the number of pending clock sleeps, so two
    rules hold for everything that runs on the loop: every task is started through `spawn`, and
    a skill task waits on at most one clock sleep at a time and on nothing else. A task started
    with a deadline is an outside wait instead (a planner call): it may await anything and it
    never waits on the clock. While it runs, the loop either waits on it in wall time with the
    world standing still (`frozen`) or keeps stepping the world (`live`, ADR 020).
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._outside: dict[asyncio.Task[Any], OutsideWait] = {}
        self._held = False  # a call was still running after its first step, this tick

    def spawn(
        self, coro: Coroutine[Any, Any, Any], name: str, *, deadline_s: float | None = None
    ) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._tasks.add(task)
        if deadline_s is not None:
            self._outside[task] = OutsideWait(task, deadline_s)
            # A cancelled call may still fail in its cleanup, after its node let go of it: read
            # the exception so asyncio never logs it (its text may hold a URL or a header).
            task.add_done_callback(_retrieve_exception)
        return task

    def live(self) -> int:
        return len(self.live_names())

    def live_names(self) -> list[str]:
        self._tasks = {t for t in self._tasks if not t.done()}
        self._outside = {t: w for t, w in self._outside.items() if not t.done()}
        return sorted(t.get_name() for t in self._tasks)

    def clock_bound_live(self) -> int:
        """Live tasks that must wait only on the clock (every live task but outside waits)."""
        self.live_names()
        return sum(1 for t in self._tasks if t not in self._outside)

    def outside_waits(self) -> list[OutsideWait]:
        """Outside waits still running, not cancelled, whose deadline has not passed."""
        self.live_names()
        return [w for w in self._outside.values() if w.waited_s is None and not w.task.cancelling()]

    def hold_running(self) -> None:
        """Called once a tick, after every new task has taken its first step: a call still
        running then has awaited something, so the world holds this tick, however soon it
        answers. A call the loop was already waiting on that has just answered holds it too,
        so its answer is read before the world steps again. A call that answers in its first
        step (the rule planner) never holds it, nor does a cancelled call that returned
        anyway: its answer is never read."""
        # Before `outside_waits`, which forgets finished tasks.
        answered = any(
            w.started is not None
            and w.waited_s is None
            and w.task.done()
            and not w.task.cancelled()
            and not w.task.cancelling()
            for w in self._outside.values()
        )
        if answered or self.outside_waits():
            self._held = True

    def take_held(self) -> bool:
        """Whether a call held the world this tick; clears it for the next tick."""
        held, self._held = self._held, False
        return held

    def outside_tasks(self) -> list[asyncio.Task[Any]]:
        """Outside waits still running, cancelled ones included."""
        self.live_names()
        return list(self._outside)

    def wait_of(self, task: asyncio.Task[Any]) -> OutsideWait | None:
        """The outside wait of `task`, while it is tracked."""
        return self._outside.get(task)

    def expired(self, task: asyncio.Task[Any]) -> OutsideWait | None:
        """The outside wait of `task` if its deadline has passed, else None."""
        wait = self._outside.get(task)
        return wait if wait is not None and wait.waited_s is not None else None


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
    planner_wait: PlannerWait = "frozen"
    # Wall time, for planner deadlines and the time a call waited (a test passes a fake).
    wall_clock: Callable[[], float] = wall_now

    def __post_init__(self) -> None:
        if self.planner_wait not in PLANNER_WAITS:
            raise ValueError(f"planner_wait must be one of {', '.join(PLANNER_WAITS)}")
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
    fails with `rejection` set. The task is an outside wait with the planner's deadline: when
    the deadline passes the call is cancelled, `planner_timed_out` is recorded and the node
    fails with `timed_out` set (ADR 016). In `live` mode the Monitor watches the hold with
    the safety rules while the call runs: a verdict cancels the call, `monitor_verdict` and
    `planner_preempted` are recorded, and the node fails with `preempted` set (ADR 020).
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
        self.deadline_s = planner_deadline(planner)  # wall time per call
        self._env = env
        self._task: asyncio.Task[object] | None = None
        self._ctx: FailureContext | None = None
        self.decision: Decision | None = None
        self.rejection: Rejection | None = None
        self.timed_out = False  # the last call passed its deadline
        self.preempted: Verdict | None = None  # the verdict that cut the last call short
        self._wait: OutsideWait | None = None
        self._called_ns = 0  # clock time the call began
        self._watched_ts: float | None = None  # the newest sample the Monitor has judged

    def ask(self, ctx: FailureContext) -> None:
        self._ctx = ctx

    def catalogue(self) -> dict[str, str]:
        """Registered skills and the Monitor profile each declares, for the planner's context."""
        return {n: self._registry.get(n).monitor_profile for n in sorted(self._registry.names())}

    def descriptions(self) -> dict[str, str]:
        """What each registered skill does, in one line (empty for a skill without one), cut
        to MAX_DESCRIPTION_CHARS: it goes into every planner call and into the log."""
        return {
            n: str(getattr(self._registry.get(n), "description", ""))[:MAX_DESCRIPTION_CHARS]
            for n in sorted(self._registry.names())
        }

    def initialise(self) -> None:
        self._task = None
        self.decision = None
        self.rejection = None
        self.timed_out = False
        self.preempted = None
        self._wait = None

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
            self._task = self._env.tracker.spawn(
                self._planner.triage(ctx), name=self.name, deadline_s=self.deadline_s
            )
            self._wait = self._env.tracker.wait_of(self._task)
            self._called_ns = self._env.clock.now_ns()
            self._watched_ts = self._env.recorder.state.last_sample_ts
            if self._env.planner_wait == "live":
                self._env.monitor.reset(HOLD_PROFILE)
            return Status.RUNNING
        if self._env.planner_wait == "live" and (verdict := self._watch_hold()):
            return self._preempt(ctx, verdict)
        if not self._task.done():
            expired = self._env.tracker.expired(self._task)
            if expired is None:
                return Status.RUNNING
            return self._time_out(ctx, expired)
        task, self._task = self._task, None
        try:
            raw = task.result()
        except (Exception, asyncio.CancelledError) as exc:  # the type only: text may hold anything
            result: Decision | Rejection = Rejection(
                "", (f"<root>: planner_raised {type(exc).__name__}",)
            )
        else:
            result = self._check(ctx, raw)
        if isinstance(result, Rejection):
            self.rejection = result
            self._env.recorder.emit(
                PlannerOutputRejected(skill=ctx.skill, raw=result.raw, errors=list(result.errors))
            )
            return Status.FAILURE
        self.decision = result
        waited_sim_s = round((self._env.clock.now_ns() - self._called_ns) / 1e9, 6)
        self._env.recorder.emit(
            PlannerDecided(skill=ctx.skill, decision=result.model_dump(), waited_sim_s=waited_sim_s)
        )
        return Status.SUCCESS

    def _watch_hold(self) -> Verdict | None:
        """The Monitor's verdict on the newest sample, if the world stepped since the last
        one it judged (the sample before the call was judged by the failed skill)."""
        state = self._env.recorder.state
        if state.last_signal is None or state.last_sample_ts == self._watched_ts:
            return None
        self._watched_ts = state.last_sample_ts
        return self._env.monitor.observe(signals_of(state.last_signal))

    def _preempt(self, ctx: FailureContext, verdict: Verdict) -> Status:
        """The Monitor wins (ADR 006, ADR 020): cancel the call, whose answer is never read,
        record the verdict and the preemption, and fail the node with `preempted` set."""
        task, self._task = self._task, None
        assert task is not None
        if not task.done():
            task.cancel()
        elif not task.cancelled():
            task.exception()  # retrieved, so asyncio never prints it; the answer is not read
        self.preempted = verdict
        env = self._env
        started = self._wait.started if self._wait is not None else None
        waited_s = 0.0 if started is None else max(env.wall_clock() - started, 0.0)
        env.recorder.emit(
            MonitorVerdict(
                skill=ctx.skill,
                failure_class=verdict.failure_class.value,
                rule=verdict.rule,
                evidence=verdict.evidence,
            )
        )
        env.recorder.emit(
            PlannerPreempted(
                skill=ctx.skill,
                failure_class=verdict.failure_class.value,
                waited_s=round(waited_s, 6),
                waited_sim_s=round((env.clock.now_ns() - self._called_ns) / 1e9, 6),
            )
        )
        return Status.FAILURE

    def _time_out(self, ctx: FailureContext, wait: OutsideWait) -> Status:
        """The call passed its deadline: cancel it (any later answer is never read), record it,
        and fail the node like a rejected answer."""
        task, self._task = self._task, None
        assert task is not None
        task.cancel()
        self.timed_out = True
        assert wait.waited_s is not None
        self._env.recorder.emit(
            PlannerTimedOut(
                skill=ctx.skill,
                deadline_s=wait.deadline_s,
                waited_s=round(max(wait.waited_s, 0.0), 6),
            )
        )
        return Status.FAILURE

    def _check(self, ctx: FailureContext, raw: object) -> Decision | Rejection:
        """Record a traced answer's trace, then validate the answer. Nothing a planner returns
        may crash the run: a trace that cannot be recorded is a rejection."""
        if isinstance(raw, Traced):
            try:
                if not isinstance(raw.trace, PlannerTrace):
                    raise TypeError("not a PlannerTrace")
                responded = _responded(ctx.skill, raw.trace)
            except Exception:  # the trace came from planner code: reject, never echo it
                return Rejection("", ("<root>: trace_invalid",))
            self._env.recorder.emit(responded)
            raw = raw.raw
        try:
            result = validate_decision(raw, self._registry, ctx.skill, self.options)
        except Exception as exc:  # validation must not crash the run; only the type
            return Rejection("", (f"<root>: validator_raised {type(exc).__name__}",))
        # The answer is about the state the call began in; in `live` the world went on
        # (ADR 020). The skill it would run must still be able to start in this one.
        if isinstance(result, SwitchSkill):
            skill = result.skill
        elif isinstance(result, Retry):
            skill = ctx.skill
        else:
            return result
        if not self._registry.get(skill).preconditions(self._env.recorder.state):
            return Rejection(audit_text(raw), (PRECONDITIONS_NOT_MET,))
        return result

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


def _retrieve_exception(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
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


# A trace's short fields (provider, model, finish reason) are cut to this length.
MAX_TRACE_NAME_CHARS = 128


def _responded(skill: str, trace: PlannerTrace) -> PlannerResponded:
    """The trace as the log keeps it: every text scrubbed and cut. The prompt is our own text
    but may be long; the response came from outside."""

    def name(value: str | None) -> str | None:
        return None if value is None else scrub(value)[:MAX_TRACE_NAME_CHARS]

    return PlannerResponded(
        skill=skill,
        provider=name(trace.provider) or "unknown",
        model=name(trace.model) or "unknown",
        prompt=scrub(trace.prompt, max_chars=MAX_STORED_PROMPT_CHARS)[:MAX_STORED_PROMPT_CHARS],
        response=scrub(trace.response)[:MAX_STORED_RESPONSE_CHARS],
        prompt_tokens=trace.prompt_tokens,
        completion_tokens=trace.completion_tokens,
        finish_reason=name(trace.finish_reason),
        latency_s=trace.latency_s,
    )
