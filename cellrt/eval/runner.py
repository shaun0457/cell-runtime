"""Run one unit of work — (scenario, setting, seed) — and record it in the event store."""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import hashlib
import json
import random
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from cellrt.eval.faults import materialize
from cellrt.eval.scenarios import Scenario
from cellrt.executor.behaviours import ExecEnv, PlannerWait
from cellrt.executor.recovery import RecoveryConfig
from cellrt.executor.runner import run_task
from cellrt.executor.trees import SKILL_SEQUENCE, Setting, build_tree
from cellrt.monitor.classifier import MonitorConfig
from cellrt.planner.base import Planner, planner_deadline
from cellrt.planner.rule_planner import RulePlanner
from cellrt.planner.task_plan import TaskPlanner, plan_task
from cellrt.runtime.clock import SimClock
from cellrt.runtime.events import (
    SCHEMA_VERSION,
    Outcome,
    RunFinished,
    RunStarted,
    TaskPlanned,
    TaskPlanRejected,
)
from cellrt.runtime.recorder import Recorder
from cellrt.runtime.store import EventStore
from cellrt.skills.base import Skill
from cellrt.skills.mock import mock_registry
from cellrt.skills.registry import SkillRegistry
from cellrt.world.mock_world import MockParams, MockWorld

# A unit interrupted this many times under one configuration is given up on, so a unit that
# crashes the eval every time cannot block the units after it on every restart.
MAX_ATTEMPTS = 3
# The core package: its source is part of every run's configuration.
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
# The adapters and the locked dependency versions count too. Both live beside the core package
# in a checkout; an installed copy without them gets a fixed "absent" value instead.
ADAPTERS_ROOT = PACKAGE_ROOT.parent / "adapters"
LOCK_FILE = PACKAGE_ROOT.parent / "uv.lock"
# `run_finished.reason` of a run whose task plan was rejected (ADR 023).
PLAN_REJECTED = "task plan rejected"


@dataclass(frozen=True)
class RunResult:
    run_id: str
    outcome: Outcome


@dataclass(frozen=True)
class EvalRuns:
    run_ids: list[str]  # one finished run per unit, in eval order
    ran: int  # units run now
    skipped: int  # units already finished under the same configuration
    gave_up: list[str]  # units ("scenario/setting/seed") interrupted MAX_ATTEMPTS times


@dataclass(frozen=True)
class _UnitConfig:
    """Everything a unit runs with besides scenario and seed, defaults filled in."""

    registry: SkillRegistry
    params: MockParams
    recovery: RecoveryConfig
    monitor: MonitorConfig
    planner: Planner | None
    # The mock always runs `frozen` (ADR 020); the field keeps the hash and the run in step.
    planner_wait: PlannerWait = "frozen"
    # None: the fixed steps, with no plan recorded (ADR 023).
    tree: TaskPlanner | None = None

    @classmethod
    def resolve(
        cls,
        setting: Setting,
        registry: SkillRegistry | None,
        params: MockParams | None,
        recovery: RecoveryConfig | None,
        monitor: MonitorConfig | None,
        planner: Planner | None,
        tree: TaskPlanner | None = None,
    ) -> _UnitConfig:
        if planner is not None and setting != "C":
            raise ValueError(f"a planner is used only in setting C, not {setting!r}")
        for asked in (planner, tree):
            if asked is not None:
                planner_deadline(asked)  # refuse a bad deadline before a run is recorded
        return cls(
            registry or mock_registry(),
            params or MockParams(),
            recovery or RecoveryConfig(),
            monitor or MonitorConfig(),
            (planner or RulePlanner()) if setting == "C" else None,
            tree=tree,
        )

    def hash(self, scenario: Scenario, setting: Setting) -> str:
        return hash_parts(self.parts(scenario, setting))

    def parts(self, scenario: Scenario, setting: Setting) -> dict[str, str]:
        return config_parts(
            scenario,
            setting,
            self.params,
            self.recovery,
            self.monitor,
            self.registry,
            self.planner,
            planner_wait=self.planner_wait,
            tree=self.tree,
        )


def run_unit(
    scenario: Scenario,
    setting: Setting,
    seed: int,
    store: EventStore,
    *,
    registry: SkillRegistry | None = None,
    params: MockParams | None = None,
    recovery: RecoveryConfig | None = None,
    monitor: MonitorConfig | None = None,
    planner: Planner | None = None,
    tree: TaskPlanner | None = None,
) -> RunResult:
    """Run one unit as its next attempt. Setting C asks `planner` (the rule planner by
    default) about UNKNOWN; `tree` plans the task's steps before the run moves (the fixed
    steps by default)."""
    config = _UnitConfig.resolve(setting, registry, params, recovery, monitor, planner, tree)
    attempt = store.next_attempt(scenario.id, setting, seed)
    return _run_attempt(scenario, setting, seed, attempt, store, config)


def _run_attempt(
    scenario: Scenario,
    setting: Setting,
    seed: int,
    attempt: int,
    store: EventStore,
    config: _UnitConfig,
) -> RunResult:
    run_id = f"{scenario.id}/{setting}/{seed}/a{attempt}"
    return asyncio.run(_run(run_id, scenario, setting, seed, attempt, store, config))


def run_eval(
    scenarios: Sequence[Scenario],
    settings: Sequence[Setting],
    seeds: int,
    store: EventStore,
    *,
    registry: SkillRegistry | None = None,
    params: MockParams | None = None,
    recovery: RecoveryConfig | None = None,
    monitor: MonitorConfig | None = None,
    planner: Planner | None = None,
    tree: TaskPlanner | None = None,
) -> EvalRuns:
    """Every (scenario, setting, seed) for seeds 0..seeds-1, one after another, in that order.

    Resumable: a unit that already has a finished attempt under the same configuration (code
    included) is skipped, and its first such attempt counts; any other unit runs as its next
    attempt, so one that was interrupted starts again from the beginning, unless it has been
    interrupted MAX_ATTEMPTS times under this configuration. Nothing is deleted from the log.
    A setting given twice runs once. Scenario ids must be unique: every run is keyed by them.
    """
    if len({s.id for s in scenarios}) != len(scenarios):
        raise ValueError("scenario ids must be unique")
    finished: dict[tuple[str, str, int, str], str] = {}
    interrupted: Counter[tuple[str, str, int, str]] = Counter()
    # Attempts per unit under any configuration, read once: the next attempt's number.
    attempts: Counter[tuple[str, str, int]] = Counter()
    for run in store.runs():  # oldest attempt first: keep the first finished one
        attempts[(run.scenario_id, run.setting, run.seed)] += 1
        key = (run.scenario_id, run.setting, run.seed, run.config_hash)
        if run.finished:
            finished.setdefault(key, run.run_id)
        else:
            interrupted[key] += 1
    run_ids: list[str] = []
    gave_up: list[str] = []
    ran = 0
    for scenario in scenarios:
        for setting in dict.fromkeys(settings):
            config = _UnitConfig.resolve(
                setting,
                registry,
                params,
                recovery,
                monitor,
                planner if setting == "C" else None,
                tree,
            )
            digest = config.hash(scenario, setting)
            for seed in range(seeds):
                key = (scenario.id, setting, seed, digest)
                if (done := finished.get(key)) is not None:
                    run_ids.append(done)
                elif interrupted[key] >= MAX_ATTEMPTS:
                    gave_up.append(f"{scenario.id}/{setting}/{seed}")
                else:
                    attempt = attempts[(scenario.id, setting, seed)] + 1
                    result = _run_attempt(scenario, setting, seed, attempt, store, config)
                    run_ids.append(result.run_id)
                    ran += 1
    return EvalRuns(run_ids=run_ids, ran=ran, skipped=len(run_ids) - ran, gave_up=gave_up)


async def _run(
    run_id: str,
    scenario: Scenario,
    setting: Setting,
    seed: int,
    attempt: int,
    store: EventStore,
    config: _UnitConfig,
) -> RunResult:
    registry, params, recovery = config.registry, config.params, config.recovery
    monitor, planner = config.monitor, config.planner
    clock = SimClock()
    # Keyed by scenario and seed only, never the setting: every setting faces the same world and
    # the same faults. Separate streams, so adding a fault does not change the world's noise.
    faults = materialize(scenario.faults, random.Random(f"{scenario.id}:{seed}:faults"))
    world = MockWorld(random.Random(f"{scenario.id}:{seed}"), params, faults)
    recorder = Recorder(run_id, store, clock)
    parts = config.parts(scenario, setting)
    recorder.emit(
        RunStarted(
            scenario_id=scenario.id,
            setting=setting,
            seed=seed,
            attempt=attempt,
            task=scenario.task,
            config_hash=hash_parts(parts),
            config_parts=parts,
            schema_version=SCHEMA_VERSION,
        )
    )
    env = ExecEnv(
        world=world,
        clock=clock,
        recorder=recorder,
        period_s=params.dt,
        fault_source=world,
        monitor_config=monitor,
        time_limit_s=scenario.task.time_limit,
        planner_wait=config.planner_wait,
    )
    steps: tuple[str, ...] = SKILL_SEQUENCE
    if config.tree is not None:
        # Before the tree is built and before anything moves: a rejected plan ends the run here.
        planned = await plan_task(config.tree, scenario.task, registry)
        if planned.errors:
            recorder.emit(
                TaskPlanRejected(
                    planner=config.tree.name,
                    steps=list(planned.steps),
                    errors=list(planned.errors),
                )
            )
            return _finish(run_id, store, recorder, clock, "aborted", PLAN_REJECTED)
        recorder.emit(TaskPlanned(planner=config.tree.name, steps=list(planned.steps)))
        steps = planned.steps
    root = build_tree(setting, registry, env, recovery, planner, steps)
    outcome, reason = await run_task(root, env, scenario.task)
    return _finish(run_id, store, recorder, clock, outcome, reason)


def _finish(
    run_id: str,
    store: EventStore,
    recorder: Recorder,
    clock: SimClock,
    outcome: Outcome,
    reason: str,
) -> RunResult:
    recorder.emit(
        RunFinished(
            outcome=outcome,
            reason=reason,
            steps=recorder.state.steps,
            duration=clock.now(),
        )
    )
    store.flush()
    return RunResult(run_id=run_id, outcome=outcome)


def config_parts(
    scenario: Scenario,
    setting: Setting,
    params: MockParams,
    recovery: RecoveryConfig,
    monitor: MonitorConfig,
    registry: SkillRegistry,
    planner: Planner | None = None,
    *,
    planner_wait: PlannerWait = "frozen",
    tree: TaskPlanner | None = None,
) -> dict[str, str]:
    """Everything that defines the run besides the seed, as one hash per part: a changed
    scenario is a different run, and two settings that share a part show the same hash for it.

    Recovery parameters count only for settings that recover; the Monitor runs in every setting;
    every skill counts with all its parameters, its Monitor profile included; setting C also
    counts the planner's type, budget and wait mode (`frozen` or `live`, ADR 020), and the
    configuration a planner describes (a model planner's model, prompt version, sampling, mode
    and client); without a planner the wait mode changes nothing, so it does not count. A task
    planner adds a `tree` part (its type, name, deadline and configuration); the fixed steps
    add none, so runs without one keep their parts (ADR 023). The core source, the adapter
    source and the lock file count too, so changed code or a dependency upgrade never reuses an
    old result.
    """
    config = {
        "scenario": scenario.model_dump(mode="json"),
        "setting": setting,
        "world": dataclasses.asdict(params),
        "recovery": None if setting == "A" else recovery.as_dict(),
        "monitor": monitor.as_dict(),
        "skills": {n: skill_config(registry.get(n)) for n in sorted(registry.names())},
        "code": code_fingerprint(),
        "adapters": adapters_fingerprint(),
        "lock": lock_fingerprint(),
        "planner": None if planner is None else planner_config(planner, recovery, planner_wait),
    }
    if tree is not None:
        config["tree"] = {
            "type": f"{type(tree).__module__}.{type(tree).__qualname__}",
            "name": tree.name,
            "deadline_s": planner_deadline(tree),
            "config": dict(tree.describe()),
        }
    return {name: _digest(value) for name, value in config.items()}


def planner_config(
    planner: Planner, recovery: RecoveryConfig, planner_wait: PlannerWait
) -> dict[str, object]:
    """The planner's type, budget, deadline and wait mode, plus its own configuration if it
    describes one (a model planner: model, prompt version, sampling)."""
    config: dict[str, object] = {
        "type": f"{type(planner).__module__}.{type(planner).__qualname__}",
        "budget": recovery.planner_budget,
        "deadline_s": planner_deadline(planner),
        "wait": planner_wait,
    }
    describe = getattr(planner, "describe", None)
    if callable(describe):
        config["config"] = dict(describe())
    return config


def hash_parts(parts: dict[str, str]) -> str:
    """The run's config_hash: one hash over all its parts."""
    return _digest(parts)


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()[:16]


@functools.cache
def code_fingerprint(root: Path = PACKAGE_ROOT) -> str:
    """A hash of every `.py` file under `root`: paths (POSIX, relative) and contents, with line
    endings normalised, so a checkout on Windows and one on Linux give the same value."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py"), key=lambda p: p.relative_to(root).as_posix()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return digest.hexdigest()[:16]


@functools.cache
def adapters_fingerprint(root: Path = ADAPTERS_ROOT) -> str:
    """`code_fingerprint` of the adapter source, or a fixed value when there is none."""
    if not root.is_dir():
        return _digest("no adapters")
    return code_fingerprint.__wrapped__(root)


@functools.cache
def lock_fingerprint(path: Path = LOCK_FILE) -> str:
    """A hash of the lock file with line endings normalised, or a fixed value when there is
    none: a dependency upgrade changes every config hash."""
    if not path.is_file():
        return _digest("no lock file")
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()[:16]


def skill_config(skill: Skill) -> dict[str, object]:
    """A skill's identity and parameters: what it reports itself (`config()`, the AIC skills),
    else all fields of a dataclass skill, else its contract."""
    own = getattr(skill, "config", None)
    if callable(own):
        return dict(own())
    if dataclasses.is_dataclass(skill) and not isinstance(skill, type):
        return {"type": type(skill).__qualname__, **dataclasses.asdict(skill)}
    return {
        "type": type(skill).__qualname__,
        "timeout_s": skill.timeout_s,
        "monitor_profile": skill.monitor_profile,
    }
