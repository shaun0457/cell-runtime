"""The policy body: one AIC goal (one task) as one recorded run, ROS-free.

`RuntimePolicy.insert_cable` calls `run_goal` in aic_model's action thread. Each goal gets its
own asyncio loop (`asyncio.run`), its own `SimClock` starting at 0 and its own run in the
store: scenario = task id, setting A, seed 0, next attempt number.

Once the world is built, however the run ends, the arm is held and the stop is then confirmed
(ADR 012): the linear TCP speed must read under 5 mm/s for 3 samples in a row within 1.0 s of
sim time; the result is recorded as `hold_checked` just before `run_finished`. A run that stops
while the world is being built (which includes the wait for the cell to be ready, counted on
the run's clock) has sent no command, so it neither holds nor checks.
`run_finished.duration` is the run's time without the check, as for a mock run.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json

import py_trees
from py_trees.common import Status

from adapters.aic.feedback import FeedbackRecorder
from adapters.aic.node import NodePort
from adapters.aic.skills import (
    STEP_S,
    AicAlign,
    AicApproach,
    AicInsertScripted,
    AicVerify,
    Descent,
)
from adapters.aic.world import POLL_WALL_S, SIM_STALL_WALL_S, AicWorld
from cellrt.eval.runner import adapters_fingerprint, code_fingerprint, hash_parts, lock_fingerprint
from cellrt.executor.behaviours import ExecEnv, SkillBehaviour
from cellrt.executor.runner import STEP_TOLERANCE_S, drain, run_task
from cellrt.monitor.classifier import MonitorConfig
from cellrt.runtime.clock import SimClock
from cellrt.runtime.events import (
    SCHEMA_VERSION,
    HoldChecked,
    Outcome,
    RunFinished,
    RunStarted,
)
from cellrt.runtime.store import EventStore
from cellrt.runtime.task import Task
from cellrt.world.base import HOLD, WorldStopped

PERIOD_S = STEP_S  # the camera rate and CheatCode's step
SETTING = "A"  # no recovery in Phase 1: every verdict ends the task, visibly
SEED = 0
GEOMETRY_SOURCE = "ground_truth"
HOLD_SPEED_MM_S = 5.0
HOLD_SAMPLES = 3
HOLD_DEADLINE_S = 1.0
# After these the world's clock cannot be trusted to move: the stop is not waited for.
_CLOCK_STOPPED = frozenset({"sim_clock_stalled", "sim_time_reset", "node_error"})


def run_goal(
    node: NodePort,
    task: Task,
    store: EventStore,
    *,
    stall_wall_s: float = SIM_STALL_WALL_S,
    poll_wall_s: float = POLL_WALL_S,
    monitor: MonitorConfig | None = None,
) -> bool:
    """Run `task` to its end in the calling thread and record it; True if the run succeeded.
    The calling thread must not be running an event loop."""
    return asyncio.run(
        _run_goal(node, task, store, stall_wall_s, poll_wall_s, monitor or MonitorConfig())
    )


async def _run_goal(
    node: NodePort,
    task: Task,
    store: EventStore,
    stall_wall_s: float,
    poll_wall_s: float,
    monitor: MonitorConfig,
) -> bool:
    attempt = store.next_attempt(task.id, SETTING, SEED)
    clock = SimClock()
    recorder = FeedbackRecorder(
        f"{task.id}/{SETTING}/{SEED}/a{attempt}", store, clock, node.send_feedback
    )
    parts = _config_parts(task, monitor, stall_wall_s, poll_wall_s)
    recorder.emit(
        RunStarted(
            scenario_id=task.id,
            setting=SETTING,
            seed=SEED,
            attempt=attempt,
            task=task,
            config_hash=hash_parts(parts),
            config_parts=parts,
            schema_version=SCHEMA_VERSION,
            geometry_source=GEOMETRY_SOURCE,
        )
    )
    outcome: Outcome
    try:
        world = AicWorld(
            node, task, stall_wall_s=stall_wall_s, poll_wall_s=poll_wall_s, on_wait=clock.advance
        )
    except WorldStopped as stopped:
        outcome, reason = "aborted", stopped.reason
        duration = clock.now()
    else:
        if clock.now() >= task.time_limit - STEP_TOLERANCE_S:
            # The wait for readiness used the whole time limit: end without moving.
            outcome, reason = "aborted", "time limit reached"
            duration = clock.now()
            recorder.emit(
                RunFinished(
                    outcome=outcome, reason=reason, steps=recorder.state.steps, duration=duration
                )
            )
            store.flush()
            return False
        env = ExecEnv(
            world=world,
            clock=clock,
            recorder=recorder,
            period_s=PERIOD_S,
            monitor_config=monitor,
            time_limit_s=task.time_limit,
        )
        root = _tree(world, env)
        try:
            outcome, reason = await run_task(root, env, task)
        except Exception as ex:  # a bug in the runtime: still stop the arm and end the run
            outcome, reason = "aborted", f"runtime raised {type(ex).__name__}"
            await _hold_after_crash(root, world, env)
        duration = clock.now()  # before the check, which only confirms the stop
        recorder.emit(_confirm_hold(world, clock, wait=reason not in _CLOCK_STOPPED))
    recorder.emit(
        RunFinished(outcome=outcome, reason=reason, steps=recorder.state.steps, duration=duration)
    )
    store.flush()
    return outcome == "success"


async def _hold_after_crash(
    root: py_trees.behaviour.Behaviour, world: AicWorld, env: ExecEnv
) -> None:
    """Stop every running skill (revoking its lease), send HOLD, then let the cancelled skill
    tasks finish, so nothing is recorded after `run_finished`. Best effort: the run is already
    failing."""
    others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    with contextlib.suppress(Exception):
        root.stop(Status.INVALID)
    with contextlib.suppress(Exception):
        world.command(HOLD)
    with contextlib.suppress(Exception):
        await drain(env)
    for t in others:  # take each task's exception, so asyncio never prints its text
        if t.done() and not t.cancelled():
            t.exception()


def _confirm_hold(world: AicWorld, clock: SimClock, *, wait: bool) -> HoldChecked:
    """Wait, at most HOLD_DEADLINE_S of sim time, for HOLD_SAMPLES slow samples in a row."""
    samples = below = 0
    max_speed = waited = 0.0
    while wait and waited < HOLD_DEADLINE_S - 1e-9:
        try:
            elapsed = world.settle(PERIOD_S)
        except WorldStopped:
            break
        clock.advance(elapsed)
        waited = round(waited + elapsed, 9)
        samples += 1
        speed = world.tcp_speed_mm_s()
        if speed is not None:
            max_speed = max(max_speed, speed)
        below = below + 1 if speed is not None and speed < HOLD_SPEED_MM_S else 0
        if below >= HOLD_SAMPLES:
            return HoldChecked(
                result="hold_confirmed", max_speed_mm_s=max_speed, samples=samples, waited_s=waited
            )
    return HoldChecked(
        result="hold_unconfirmed", max_speed_mm_s=max_speed, samples=samples, waited_s=waited
    )


def _tree(world: AicWorld, env: ExecEnv) -> py_trees.behaviour.Behaviour:
    """Setting A, as for the mock: approach, align, insert, verify; any failure ends the task."""
    descent = Descent()
    skills = [
        AicApproach(world.geometry),
        AicAlign(world.geometry, descent),
        AicInsertScripted(world.geometry, descent),
        AicVerify(world.geometry),
    ]
    children = [SkillBehaviour(skill, env) for skill in skills]
    return py_trees.composites.Sequence(name="aic_setting_A", memory=True, children=children)


def _config_parts(
    task: Task, monitor: MonitorConfig, stall_wall_s: float, poll_wall_s: float
) -> dict[str, str]:
    """One hash per part, as for mock runs (`cellrt.eval.runner.config_parts`)."""
    config = {
        "scenario": task.model_dump(mode="json"),
        "setting": SETTING,
        "world": {
            "type": "AicWorld",
            "period_s": PERIOD_S,
            "stall_wall_s": stall_wall_s,
            "poll_wall_s": poll_wall_s,  # how far a step can overshoot the period
            "hold": [HOLD_SPEED_MM_S, HOLD_SAMPLES, HOLD_DEADLINE_S],
            "geometry_source": GEOMETRY_SOURCE,
        },
        "recovery": None,
        "monitor": monitor.as_dict(),
        "skills": {
            skill.name: skill.config()
            for skill in (AicApproach, AicAlign, AicInsertScripted, AicVerify)
        },
        "code": code_fingerprint(),
        "adapters": adapters_fingerprint(),
        "lock": lock_fingerprint(),
        "planner": None,
    }
    return {name: _digest(value) for name, value in config.items()}


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()[:16]
