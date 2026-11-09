"""The policy body: one AIC goal (one task) as one recorded run, ROS-free.

`RuntimePolicy.insert_cable` calls `run_goal` in aic_model's action thread. Each goal gets its
own asyncio loop (`asyncio.run`), its own `SimClock` starting at 0 and its own run in the
store: scenario = task id, setting A, seed 0, next attempt number.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

import py_trees

from adapters.aic.node import NodePort
from adapters.aic.skills import AicApproach
from adapters.aic.world import POLL_WALL_S, SIM_STALL_WALL_S, AicWorld
from cellrt.eval.runner import adapters_fingerprint, code_fingerprint, hash_parts, lock_fingerprint
from cellrt.executor.behaviours import ExecEnv, SkillBehaviour
from cellrt.executor.runner import run_task
from cellrt.monitor.classifier import MonitorConfig
from cellrt.runtime.clock import SimClock
from cellrt.runtime.events import SCHEMA_VERSION, Outcome, RunFinished, RunStarted
from cellrt.runtime.recorder import Recorder
from cellrt.runtime.store import EventStore
from cellrt.runtime.task import Task
from cellrt.world.base import WorldStopped

PERIOD_S = 0.05  # the camera rate and CheatCode's step
SETTING = "A"  # no recovery in Phase 1: every verdict ends the task, visibly
SEED = 0
GEOMETRY_SOURCE = "ground_truth"


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
    recorder = Recorder(f"{task.id}/{SETTING}/{SEED}/a{attempt}", store, clock)
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
        world = AicWorld(node, task, stall_wall_s=stall_wall_s, poll_wall_s=poll_wall_s)
    except WorldStopped as stopped:
        outcome, reason = "aborted", stopped.reason
    else:
        env = ExecEnv(
            world=world,
            clock=clock,
            recorder=recorder,
            period_s=PERIOD_S,
            monitor_config=monitor,
            time_limit_s=task.time_limit,
        )
        outcome, reason = await run_task(_tree(world, env), env, task)
    recorder.emit(
        RunFinished(
            outcome=outcome, reason=reason, steps=recorder.state.steps, duration=clock.now()
        )
    )
    store.flush()
    return outcome == "success"


def _tree(world: AicWorld, env: ExecEnv) -> py_trees.behaviour.Behaviour:
    # The tracer bullet runs approach only; the full setting-A sequence comes with the other
    # scripted skills (#27).
    approach = SkillBehaviour(AicApproach(world.geometry), env)
    return py_trees.composites.Sequence(name="aic_setting_A", memory=True, children=[approach])


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
            "geometry_source": GEOMETRY_SOURCE,
        },
        "recovery": None,
        "monitor": monitor.as_dict(),
        "skills": {"approach": AicApproach.config()},
        "code": code_fingerprint(),
        "adapters": adapters_fingerprint(),
        "lock": lock_fingerprint(),
        "planner": None,
    }
    return {name: _digest(value) for name, value in config.items()}


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()[:16]
