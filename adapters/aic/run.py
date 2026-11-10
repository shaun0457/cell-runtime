"""The policy body: one AIC goal (one task) as one recorded run, ROS-free.

`RuntimePolicy.insert_cable` calls `run_goal` in aic_model's action thread. Each goal gets its
own asyncio loop (`asyncio.run`), its own `SimClock` starting at 0 and its own run in the
store: the setting asked for (A by default), seed 0, the next attempt number, and the scenario
the caller names (by default the task id). `RuntimePolicy` names one per goal with
`goal_scenario_id`: aic_engine sends no trial id, and in its sample config every trial's task
is `task_1`, and it takes the setting, the planner and the model planner's token budget and
reasoning from the environment (`policy_setup`), and the fault to inject, if any, from
`CELLRT_FAULT` (`fault_setup`, #60).

Settings B and C build the same trees as the mock, from the AIC skills plus a wait for the
F/T sensor. The executor runs in `live` mode (ADR 020): the sim does not stop while a planner
thinks, so the arm holds, the world keeps stepping and the Monitor keeps watching.

Once the world is built, however the run ends, the arm is held and the stop is then confirmed
(ADR 012): the linear TCP speed must read under 5 mm/s for 3 samples in a row within 1.0 s of
sim time; the result is recorded as `hold_checked` just before `run_finished`. If an injected
command dropout is still active, the HOLD is lost too and the arm stays at its last target
(#60): the check does not wait and records `hold_unconfirmed`. A run that stops
while the world is being built (which includes the wait for the cell to be ready, counted on
the run's clock) has sent no command, so it neither holds nor checks.
`run_finished.duration` is the run's time without the check, as for a mock run.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from typing import cast

import py_trees
from py_trees.common import Status

from adapters.aic.faults import FAULT_ENV, CommandDropout, FaultSpec, FaultSpecError
from adapters.aic.feedback import FeedbackRecorder
from adapters.aic.node import NodePort
from adapters.aic.skills import (
    SLOWEST_SCALE,
    STEP_S,
    AicAlign,
    AicApproach,
    AicInsertScripted,
    AicRetract,
    AicVerify,
    Descent,
)
from adapters.aic.world import POLL_WALL_S, SIM_STALL_WALL_S, AicWorld, Geometry
from cellrt.eval.runner import (
    adapters_fingerprint,
    code_fingerprint,
    hash_parts,
    lock_fingerprint,
    planner_config,
    skill_config,
)
from cellrt.executor.behaviours import ExecEnv, PlannerWait
from cellrt.executor.recovery import RecoveryConfig
from cellrt.executor.runner import (
    STEP_TOLERANCE_S,
    drain,
    record_fault_transitions,
    run_task,
)
from cellrt.executor.trees import SETTINGS, Setting, build_tree
from cellrt.monitor.classifier import MonitorConfig
from cellrt.planner.base import Planner
from cellrt.planner.factory import PLANNER_KINDS, build_planner
from cellrt.planner.llm import DEFAULT_MAX_TOKENS, MAX_TOKENS_LIMIT, REASONING_MODES, Reasoning
from cellrt.planner.nim import Connect, LlmSetupError, connect_openai
from cellrt.planner.rule_planner import RulePlanner
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
from cellrt.skills.mock import WaitSensor
from cellrt.skills.registry import SkillRegistry
from cellrt.world.base import HOLD, WorldStopped

PERIOD_S = STEP_S  # the camera rate and CheatCode's step
DEFAULT_SETTING: Setting = "A"  # no recovery: every verdict ends the task, visibly
DEFAULT_PLANNER = "rule"
SETTING_ENV = "CELLRT_SETTING"
PLANNER_ENV = "CELLRT_PLANNER"
MAX_TOKENS_ENV = "CELLRT_MAX_TOKENS"
REASONING_ENV = "CELLRT_REASONING"
_MAX_TOKENS = re.compile(r"[0-9]{1,5}")  # ASCII digits only, and short enough to read as an int
PLANNER_WAIT: PlannerWait = "live"  # the sim does not stop while a planner thinks (ADR 020)
SEED = 0
GEOMETRY_SOURCE = "ground_truth"
HOLD_SPEED_MM_S = 5.0
HOLD_SAMPLES = 3
HOLD_DEADLINE_S = 1.0
DEFAULT_SESSION = "aic"
_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
# After these the world's clock cannot be trusted to move: the stop is not waited for.
_CLOCK_STOPPED = frozenset({"sim_clock_stalled", "sim_time_reset", "node_error"})


class PolicySetupError(ValueError):
    """The policy's settings cannot be used. The message is fixed and safe to log."""


def policy_setup(
    environ: Mapping[str, str], *, connect: Connect = connect_openai
) -> tuple[Setting, Planner | None]:
    """The setting (`CELLRT_SETTING`: A, B or C, default A) and the planner (`CELLRT_PLANNER`:
    rule, fixed or llm, default rule) that `environ` names, with the model planner's tokens per
    call (`CELLRT_MAX_TOKENS`: 1 to 8192, default 1024) and reasoning (`CELLRT_REASONING`:
    default or off). Every variable is checked in every setting, so a typo shows at once; the
    last two are unset when blank. The planner is built for setting C only, so A and B need no
    key; `llm` reads its key from `NVIDIA_API_KEY` in `environ` and hands it to the SDK only.
    Raises PolicySetupError with a fixed message, which never repeats the value it refuses."""
    setting = environ.get(SETTING_ENV, DEFAULT_SETTING)
    if setting not in SETTINGS:
        raise PolicySetupError(f"{SETTING_ENV} must be one of: {', '.join(SETTINGS)}")
    kind = environ.get(PLANNER_ENV, DEFAULT_PLANNER)
    if kind not in PLANNER_KINDS:
        raise PolicySetupError(f"{PLANNER_ENV} must be one of: {', '.join(PLANNER_KINDS)}")
    max_tokens, reasoning = _model_settings(environ)
    if setting != "C":
        return cast(Setting, setting), None
    try:
        planner = build_planner(
            kind, max_tokens=max_tokens, reasoning=reasoning, env=environ, connect=connect
        )
    except LlmSetupError as exc:  # fixed texts, the key in none of them
        raise PolicySetupError(str(exc)) from None
    return "C", planner


def _model_settings(environ: Mapping[str, str]) -> tuple[int, Reasoning]:
    """`CELLRT_MAX_TOKENS` and `CELLRT_REASONING`, checked; the defaults where blank or unset."""
    raw = environ.get(MAX_TOKENS_ENV, "")
    max_tokens = DEFAULT_MAX_TOKENS
    if raw.strip():
        if not _MAX_TOKENS.fullmatch(raw) or not 0 < int(raw) <= MAX_TOKENS_LIMIT:
            raise PolicySetupError(
                f"{MAX_TOKENS_ENV} must be a whole number from 1 to {MAX_TOKENS_LIMIT}"
            )
        max_tokens = int(raw)
    given = environ.get(REASONING_ENV, "")
    reasoning = given if given.strip() else "default"  # exact, as CELLRT_MAX_TOKENS
    if reasoning not in REASONING_MODES:
        raise PolicySetupError(f"{REASONING_ENV} must be one of: {', '.join(REASONING_MODES)}")
    return max_tokens, cast(Reasoning, reasoning)


def fault_setup(environ: Mapping[str, str]) -> FaultSpec | None:
    """The fault `CELLRT_FAULT` names, or None if it is unset or blank. Raises
    PolicySetupError with `SPEC_REFUSED`, which never repeats the value it refuses."""
    raw = environ.get(FAULT_ENV, "")
    if not raw.strip():
        return None
    try:
        return FaultSpec.parse(raw)
    except FaultSpecError as exc:
        raise PolicySetupError(str(exc)) from None


def aic_registry(geometry: Callable[[], Geometry]) -> SkillRegistry:
    """The AIC skills under the names the trees use: CheatCode's phases sharing one descent,
    the retract, and the mock's wait for the F/T sensor, which only holds and reads whether
    the wrench is valid, as `AicWorld` reports it."""
    descent = Descent()
    return SkillRegistry(
        [
            AicApproach(geometry),
            AicAlign(geometry, descent),
            AicInsertScripted(geometry, descent),
            AicVerify(geometry),
            AicRetract(geometry, descent),
            WaitSensor(),
        ]
    )


def session_label(raw: str | None) -> str:
    """The engine config's name for scenario ids (`CELLRT_SESSION`), or the default; raises
    ValueError for anything but a plain name of 1 to 64 characters."""
    if raw is None:
        return DEFAULT_SESSION
    if not _SESSION.fullmatch(raw):
        raise ValueError(
            "CELLRT_SESSION must be 1-64 letters, digits, '_', '.' or '-', starting with a"
            " letter or digit"
        )
    return raw


def goal_scenario_id(session: str, goal: int, task_id: str) -> str:
    """One unit per goal of an engine run: the same engine config run again gives the same ids,
    so its runs are new attempts of the same units. Goal N is the N-th goal the policy received,
    which is the N-th task of the config only if the engine sent every goal before it: a trial
    the engine skips (its model not ready, aic_engine.cpp:730-737) or a task it never sends
    (after an earlier task's timeout, :1394-1400) shifts every later goal by one."""
    return f"{session}/goal{goal}/{task_id}"


def run_goal(
    node: NodePort,
    task: Task,
    store: EventStore,
    *,
    scenario_id: str | None = None,
    stall_wall_s: float = SIM_STALL_WALL_S,
    poll_wall_s: float = POLL_WALL_S,
    monitor: MonitorConfig | None = None,
    setting: Setting = DEFAULT_SETTING,
    planner: Planner | None = None,
    recovery: RecoveryConfig | None = None,
    fault: CommandDropout | None = None,
) -> bool:
    """Run `task` to its end in the calling thread and record it; True if the run succeeded.
    The calling thread must not be running an event loop. `planner` is setting C's (the rule
    planner if None) and is ignored in A and B. `fault` is injected into the cell (#60).
    A recovery slower than the skills allow raises ValueError before anything runs."""
    if setting not in SETTINGS:
        raise ValueError(f"setting must be one of: {', '.join(SETTINGS)}")
    recovery = recovery or RecoveryConfig()
    if not SLOWEST_SCALE <= recovery.slow_speed_scale <= 1.0:
        raise ValueError(f"slow_speed_scale must be from {SLOWEST_SCALE} to 1")
    goal = _Goal(
        setting=setting,
        planner=(planner or RulePlanner()) if setting == "C" else None,
        recovery=recovery,
        monitor=monitor or MonitorConfig(),
        stall_wall_s=stall_wall_s,
        poll_wall_s=poll_wall_s,
        fault=fault,
    )
    scenario = task.id if scenario_id is None else scenario_id
    return asyncio.run(_run_goal(node, task, scenario, store, goal))


@dataclasses.dataclass(frozen=True)
class _Goal:
    """How a goal runs: its setting, planner, recovery and Monitor, and the world's waits."""

    setting: Setting
    planner: Planner | None
    recovery: RecoveryConfig
    monitor: MonitorConfig
    stall_wall_s: float
    poll_wall_s: float
    fault: CommandDropout | None = None


async def _run_goal(
    node: NodePort, task: Task, scenario_id: str, store: EventStore, goal: _Goal
) -> bool:
    attempt = store.next_attempt(scenario_id, goal.setting, SEED)
    clock = SimClock()
    recorder = FeedbackRecorder(
        f"{scenario_id}/{goal.setting}/{SEED}/a{attempt}", store, clock, node.send_feedback
    )
    parts = _config_parts(task, goal)
    recorder.emit(
        RunStarted(
            scenario_id=scenario_id,
            setting=goal.setting,
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
            node,
            task,
            stall_wall_s=goal.stall_wall_s,
            poll_wall_s=goal.poll_wall_s,
            on_wait=clock.advance,
            fault=goal.fault,
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
            monitor_config=goal.monitor,
            time_limit_s=task.time_limit,
            planner_wait=PLANNER_WAIT,
            fault_source=world,
        )
        root = _tree(world, env, goal)
        try:
            outcome, reason = await run_task(root, env, task)
        except Exception as ex:  # a bug in the runtime: still stop the arm and end the run
            outcome, reason = "aborted", f"runtime raised {type(ex).__name__}"
            await _hold_after_crash(root, world, env)
        # A fault change decided at the run's last sample took effect, but no step reported it.
        record_fault_transitions(recorder, world.final_fault_transitions())
        duration = clock.now()  # before the check, which only confirms the stop
        # Wait for the arm to stop only when the clock moves and the HOLD reached the arm: a
        # dropout still active lost the HOLD too, so nothing the run sent stopped the arm.
        confirmable = reason not in _CLOCK_STOPPED and not world.commands_dropped()
        recorder.emit(_confirm_hold(world, clock, wait=confirmable))
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


def _tree(world: AicWorld, env: ExecEnv, goal: _Goal) -> py_trees.behaviour.Behaviour:
    """The mock's tree for the goal's setting, from the AIC skills."""
    registry = aic_registry(world.geometry)
    return build_tree(goal.setting, registry, env, goal.recovery, goal.planner)


def _no_geometry() -> Geometry:
    raise RuntimeError("the config is read from the skills' classes, not from a world")


def _config_parts(task: Task, goal: _Goal) -> dict[str, str]:
    """One hash per part, as for mock runs (`cellrt.eval.runner.config_parts`): recovery for
    the settings that recover, the planner (with the wait mode) for setting C. An injected
    fault is part of the scenario, as a mock scenario holds its faults, so runs of one
    setting stay one configuration for the agent report; without one the scenario part is
    the task alone, as before #60."""
    registry = aic_registry(_no_geometry)
    planner = goal.planner
    config = {
        "scenario": task.model_dump(mode="json")
        if goal.fault is None
        else {**task.model_dump(mode="json"), "faults": [goal.fault.config()]},
        "setting": goal.setting,
        "world": {
            "type": "AicWorld",
            "period_s": PERIOD_S,
            "stall_wall_s": goal.stall_wall_s,
            "poll_wall_s": goal.poll_wall_s,  # how far a step can overshoot the period
            "hold": [HOLD_SPEED_MM_S, HOLD_SAMPLES, HOLD_DEADLINE_S],
            "geometry_source": GEOMETRY_SOURCE,
        },
        "recovery": None if goal.setting == "A" else goal.recovery.as_dict(),
        "monitor": goal.monitor.as_dict(),
        "skills": {n: skill_config(registry.get(n)) for n in sorted(registry.names())},
        "code": code_fingerprint(),
        "adapters": adapters_fingerprint(),
        "lock": lock_fingerprint(),
        "planner": None
        if planner is None
        else planner_config(planner, goal.recovery, PLANNER_WAIT),
    }
    return {name: _digest(value) for name, value in config.items()}


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()[:16]
