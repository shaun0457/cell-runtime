"""The main loop: single-threaded lockstep between the tree, the skill tasks and the world.

Each step: tick the tree → let every skill task run until it waits on the clock → step the
world → advance the clock → record a signal sample. asyncio's ready queue is FIFO and nothing
else runs on the loop, so the same inputs always give the same schedule.
"""

from __future__ import annotations

import asyncio

import py_trees
from py_trees.common import Status

from cellrt.executor.behaviours import ExecEnv, SkillBehaviour
from cellrt.runtime.events import (
    BtNodeChanged,
    FaultCleared,
    FaultInjected,
    Outcome,
    SignalSampled,
)
from cellrt.runtime.task import Task

MAX_DRAIN_YIELDS = 100


class DrainError(RuntimeError):
    """A skill task kept running without ever waiting on the clock."""


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
            return "failed", _failure_reason(root)
        await drain(env)
        env.world.step(env.period_s)
        env.clock.advance(env.period_s)
        step += 1
        _record_faults(env)
        _record_signal(env, step)
        if env.clock.now() >= task.time_limit:
            root.stop(Status.INVALID)
            _record_status_changes(root, statuses, env)
            await _finish(env)
            return "aborted", "time limit reached"


async def drain(env: ExecEnv) -> None:
    """Yield to the loop until every live skill task is waiting on the clock (or done)."""
    for _ in range(MAX_DRAIN_YIELDS):
        if env.tracker.live() <= env.clock.pending_waiters():
            return
        await asyncio.sleep(0)
    names = ", ".join(env.tracker.live_names())
    raise DrainError(
        f"skill tasks still runnable after {MAX_DRAIN_YIELDS} yields "
        f"(they must wait only on the clock): {names}"
    )


async def _finish(env: ExecEnv) -> None:
    """Drain once more and check that no skill task outlives the run."""
    await drain(env)
    if leftover := env.tracker.live_names():
        raise DrainError(f"skill tasks still alive after the run ended: {', '.join(leftover)}")


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
    s = env.world.signals()
    env.recorder.emit(
        SignalSampled(
            step=step,
            height_mm=s.height_mm,
            depth_mm=s.depth_mm,
            xy_error_mm=round(s.xy_error_mm, 6),
            force_n=s.force_n,
            torque_nm=s.torque_nm,
            wrench_valid=s.wrench_valid,
        )
    )


def _record_status_changes(
    root: py_trees.behaviour.Behaviour, statuses: dict[int, str], env: ExecEnv
) -> None:
    for node in root.iterate():
        status = node.status.value
        # Every node starts INVALID; only real transitions are recorded.
        if statuses.get(id(node), Status.INVALID.value) != status:
            statuses[id(node)] = status
            env.recorder.emit(BtNodeChanged(node=node.name, status=status))


def _failure_reason(root: py_trees.behaviour.Behaviour) -> str:
    for node in root.iterate():
        if isinstance(node, SkillBehaviour) and node.status == Status.FAILURE:
            why = node.failure or node.note
            return f"{node.name} failed: {why}" if why else f"{node.name} failed"
    return "tree failed"
