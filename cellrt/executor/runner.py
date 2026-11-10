"""The main loop: single-threaded lockstep between the tree, the skill tasks and the world.

Each step: tick the tree → let every skill task run until it waits on the clock → step the
world → advance the clock by the time the step reports → record a signal sample. asyncio's
ready queue is FIFO and nothing else runs on the loop, so the same inputs always give the same
schedule.

While a planner call waits on outside work (a model's answer), the arm holds, and the world does
what `ExecEnv.planner_wait` says:
- `frozen` (the default, the mock): the loop does not step the world or advance the clock. It
  waits on the call in wall time, up to the call's deadline, then ticks the tree again (ADR 016),
  so sim-time results do not depend on how long the model took.
- `live` (a simulator or a cell, whose time does not stop): the loop keeps stepping, sampling
  and recording, and checks the call's wall deadline on each step without blocking; the time
  limit applies as on any step (ADR 020). An answer, or a deadline passed, is acted on at the
  next tick, before another step.
"""

from __future__ import annotations

import asyncio
import math

import py_trees
from py_trees.common import Status

from cellrt.executor.behaviours import ExecEnv, OutsideWait, SkillBehaviour
from cellrt.executor.recovery import Recovering
from cellrt.executor.samples import sampled
from cellrt.runtime.events import (
    BtNodeChanged,
    FaultCleared,
    FaultInjected,
    Outcome,
)
from cellrt.runtime.task import Task
from cellrt.world.base import WorldStopped

MAX_DRAIN_YIELDS = 100
# Wall time a cancelled planner call gets to finish its cleanup (closing a connection) once the
# run has ended. A call still running after that ignored its cancellation (ADR 016).
CANCEL_GRACE_S = 1.0
# A world step may take longer than the period, never shorter: a step that reports less (or no
# number) would stall the clock, so neither the time limit nor any skill timeout would end the
# run.
STEP_TOLERANCE_S = 1e-9


class DrainError(RuntimeError):
    """A skill task kept running without ever waiting on the clock, or a task (a planner call
    that ignored its cancellation) outlived the run."""


async def run_task(
    root: py_trees.behaviour.Behaviour, env: ExecEnv, task: Task
) -> tuple[Outcome, str]:
    statuses: dict[int, str] = {}  # keyed by node identity: names repeat in recovery trees
    step = 0
    while True:
        root.tick_once()
        _record_status_changes(root, statuses, env)
        if root.status in (Status.SUCCESS, Status.FAILURE):
            await _finish(env)
            if root.status == Status.SUCCESS:
                return "success", "task complete"
            if crashed := _raised(root):
                return "aborted", crashed
            if stopped := _planner_aborted(root):
                return "aborted", stopped
            return "failed", _failure_reason(root)
        await drain(env)
        if env.tracker.take_held():
            waits = env.tracker.outside_waits()
            if env.planner_wait == "frozen":
                if waits:
                    await _wait_outside(waits, env)
                continue  # tick again: the world does not move while the planner thinks
            # live: the world steps on while the arm holds. A call that has just answered is
            # read, and one past its deadline cancelled, on the next tick, before another step.
            if not waits or _check_deadlines(waits, env):
                continue
        try:
            elapsed = env.world.step(env.period_s)
        except WorldStopped as stopped:
            return await _stop(root, statuses, env, stopped.reason)
        if not (math.isfinite(elapsed) and elapsed >= env.period_s - STEP_TOLERANCE_S):
            return await _stop(root, statuses, env, "world step time invalid")
        env.clock.advance(elapsed)
        step += 1
        _record_faults(env)
        _record_signal(env, step)
        if env.clock.now() >= task.time_limit:
            return await _stop(root, statuses, env, "time limit reached")


async def _stop(
    root: py_trees.behaviour.Behaviour, statuses: dict[int, str], env: ExecEnv, reason: str
) -> tuple[Outcome, str]:
    """End the run from outside the tree: stop every running node (each skill is cancelled and
    HOLD is sent), then check that no skill task is left."""
    root.stop(Status.INVALID)
    _record_status_changes(root, statuses, env)
    await _finish(env)
    return "aborted", reason


async def drain(env: ExecEnv) -> None:
    """Yield to the loop until every live task is waiting on the clock (or done).

    A planner call that answers in its first step finishes here, as in Phase 0. One still
    running after that step has awaited outside work: it holds the world this tick, even if it
    answers within the yields, so whether the world moves never depends on how fast the answer
    came (ADR 016). After the yields, live calls are fine as long as every other task waits on
    the clock; the loop then waits on them (`_wait_outside`, `frozen`) or checks their
    deadlines and steps on (`_check_deadlines`, `live`).
    """
    for i in range(MAX_DRAIN_YIELDS):
        if env.tracker.live() <= env.clock.pending_waiters():
            return
        await asyncio.sleep(0)
        if i == 0:
            # Every task started by this tick has now taken its first step (FIFO ready queue).
            env.tracker.hold_running()
    if env.tracker.clock_bound_live() <= env.clock.pending_waiters():
        return
    names = ", ".join(env.tracker.live_names())
    raise DrainError(
        f"tasks still runnable after {MAX_DRAIN_YIELDS} yields "
        f"(they must wait only on the clock): {names}"
    )


async def _wait_outside(waits: list[OutsideWait], env: ExecEnv) -> None:
    """Wait in wall time until a call finishes or the earliest deadline passes; mark every call
    whose deadline has passed, for its node to cancel on the next tick."""
    now = env.wall_clock()
    _start(waits, now)
    remaining = min(w.started + w.deadline_s for w in waits if w.started is not None) - now
    if remaining > 0:
        await asyncio.wait(
            [w.task for w in waits], timeout=remaining, return_when=asyncio.FIRST_COMPLETED
        )
    _mark_expired(waits, env.wall_clock())


def _check_deadlines(waits: list[OutsideWait], env: ExecEnv) -> bool:
    """Live mode: the same deadlines as `_wait_outside`, checked once per step, never waited
    on. Marks every call whose deadline has passed, for its node to cancel; True if any was."""
    now = env.wall_clock()
    _start(waits, now)
    _mark_expired(waits, now)
    return any(w.waited_s is not None for w in waits)


def _start(waits: list[OutsideWait], now: float) -> None:
    for wait in waits:
        if wait.started is None:
            wait.started = now


def _mark_expired(waits: list[OutsideWait], now: float) -> None:
    for wait in waits:
        assert wait.started is not None
        if not wait.task.done() and now >= wait.started + wait.deadline_s:
            wait.waited_s = now - wait.started


async def _finish(env: ExecEnv) -> None:
    """Drain once more, give cancelled planner calls up to CANCEL_GRACE_S of wall time to
    finish, and check that no task outlives the run."""
    await drain(env)
    if outside := env.tracker.outside_tasks():
        await asyncio.wait(outside, timeout=CANCEL_GRACE_S)
    if leftover := env.tracker.live_names():
        raise DrainError(f"tasks still alive after the run ended: {', '.join(leftover)}")


def _record_faults(env: ExecEnv) -> None:
    if env.fault_source is None:
        return
    for t in env.fault_source.take_fault_transitions():
        event_type = FaultInjected if t.active else FaultCleared
        env.recorder.emit(
            event_type(
                fault_id=t.fault.fault_id,
                kind=t.fault.kind,
                label=str(t.fault.label),
                params=dict(t.fault.params),
            )
        )


def _record_signal(env: ExecEnv, step: int) -> None:
    env.recorder.emit(sampled(step, env.world.signals()))


def _record_status_changes(
    root: py_trees.behaviour.Behaviour, statuses: dict[int, str], env: ExecEnv
) -> None:
    for node in root.iterate():
        status = node.status.value
        # Every node starts INVALID; only real transitions are recorded.
        if statuses.get(id(node), Status.INVALID.value) != status:
            statuses[id(node)] = status
            env.recorder.emit(BtNodeChanged(node=node.name, status=status))


def _raised(root: py_trees.behaviour.Behaviour) -> str | None:
    """Why the run is aborted if a skill raised, else None."""
    for node in root.iterate():
        if isinstance(node, SkillBehaviour) and node.error:
            return f"{node.skill.name} raised {node.error}"
    return None


def _planner_aborted(root: py_trees.behaviour.Behaviour) -> str | None:
    """Why the run is aborted if the planner stopped it (abort or escalate), timed out, or its
    output was rejected; else None."""
    for node in root.iterate():
        if isinstance(node, Recovering) and node.aborted and node.failure_reason:
            return node.failure_reason
    return None


def _failure_reason(root: py_trees.behaviour.Behaviour) -> str:
    # A recovering step knows why it gave up (iterate() visits children before their parent).
    for node in root.iterate():
        if isinstance(node, Recovering) and node.status == Status.FAILURE and node.failure_reason:
            return node.failure_reason
    for node in root.iterate():
        if isinstance(node, SkillBehaviour) and node.status == Status.FAILURE:
            why = node.failure or node.note
            return f"{node.name} failed: {why}" if why else f"{node.name} failed"
    return "tree failed"
