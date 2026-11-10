"""Run one unit of work — (scenario, setting, seed) — and record it in the event store."""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import hashlib
import json
import random
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
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
from cellrt.planner.nim import ModelCallCapReached
from cellrt.planner.rule_planner import RulePlanner
from cellrt.planner.task_plan import CAP_REACHED, PlanResult, TaskPlanner, plan_task
from cellrt.runtime.clock import SimClock
from cellrt.runtime.events import (
    SCHEMA_VERSION,
    Outcome,
    RunFinished,
    RunStarted,
    TaskPlanned,
    TaskPlanRejected,
    TaskPlanResponded,
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
# A run's plan event comes after `run_started` and its call's trace: a resumed eval reads at most
# this many events of a finished run to reuse its plan.
PLAN_EVENT_LIMIT = 3
# The parts a task plan cannot depend on: how the run recovers. Two runs that differ only in
# these share a plan in an eval (#63 review F1).
NOT_PLAN_PARTS = frozenset({"setting", "recovery", "planner"})


@dataclass(frozen=True)
class RunResult:
    run_id: str
    # None: unfinished. The task plan call gave no answer (it timed out or raised), so the run
    # has no result; an eval runs the unit again, as it does an interrupted one.
    outcome: Outcome | None


@dataclass(frozen=True)
class EvalRuns:
    run_ids: list[str]  # one finished run per unit, in eval order
    ran: int  # units run now
    skipped: int  # units already finished under the same configuration
    gave_up: list[str]  # units ("scenario/setting/seed") interrupted MAX_ATTEMPTS times
    unfinished: list[str]  # units run now whose task plan call gave no answer


@dataclass(frozen=True)
class _SharedPlan:
    """A task plan one run asked for, reused by the other settings of its scenario and seed."""

    origin: str  # the run whose planner call made it
    result: PlanResult  # without the call's trace, which only the origin records


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
    result, _ = _run_attempt(scenario, setting, seed, attempt, store, config, None)
    return result


def _run_attempt(
    scenario: Scenario,
    setting: Setting,
    seed: int,
    attempt: int,
    store: EventStore,
    config: _UnitConfig,
    shared: _SharedPlan | None,
) -> tuple[RunResult, _SharedPlan | None]:
    run_id = f"{scenario.id}/{setting}/{seed}/a{attempt}"
    return asyncio.run(_run(run_id, scenario, setting, seed, attempt, store, config, shared))


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

    With a task planner, the settings of one (scenario, seed) run one plan: the first unit to
    run asks the planner, the others reuse its answer and record `reused_from`, so the settings
    differ only in how they recover (#63 review F1). A resumed eval reuses the plan of a finished
    run made under the same configuration apart from setting, recovery and planner. A plan call
    that gave no answer is not asked again in the same eval, so an eval makes at most one plan
    call per (scenario, seed).

    A unit whose task plan call gave no answer is left unfinished, like an interrupted one, and
    runs again next time. A task plan call that meets the model call cap stops the eval:
    `ModelCallCapReached` propagates, and the units after it do not run.
    """
    if len({s.id for s in scenarios}) != len(scenarios):
        raise ValueError("scenario ids must be unique")
    finished: dict[tuple[str, str, int, str], str] = {}
    interrupted: Counter[tuple[str, str, int, str]] = Counter()
    # Attempts per unit under any configuration, read once: the next attempt's number.
    attempts: Counter[tuple[str, str, int]] = Counter()
    # Finished runs per (scenario, seed), in store order (by setting, then attempt): where a
    # resumed eval looks for a plan.
    done_by_seed: defaultdict[tuple[str, int], list[str]] = defaultdict(list)
    for run in store.runs():  # oldest attempt first: keep the first finished one
        attempts[(run.scenario_id, run.setting, run.seed)] += 1
        key = (run.scenario_id, run.setting, run.seed, run.config_hash)
        if run.finished:
            finished.setdefault(key, run.run_id)
            done_by_seed[(run.scenario_id, run.seed)].append(run.run_id)
        else:
            interrupted[key] += 1
    # One plan per (scenario, seed) in this eval; every setting has the same plan parts.
    # Keyed by the plan parts too, so a plan is never shared across configurations that differ
    # in anything a plan may depend on (today every setting of an eval has the same ones).
    plans: dict[tuple[str, int, str], _SharedPlan | None] = {}
    run_ids: list[str] = []
    gave_up: list[str] = []
    unfinished: list[str] = []
    ran = skipped = 0
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
            parts = config.parts(scenario, setting)
            digest, plan_parts = hash_parts(parts), plan_digest(parts)
            for seed in range(seeds):
                key = (scenario.id, setting, seed, digest)
                unit = f"{scenario.id}/{setting}/{seed}"
                if (done := finished.get(key)) is not None:
                    run_ids.append(done)
                    skipped += 1
                elif interrupted[key] >= MAX_ATTEMPTS:
                    gave_up.append(unit)
                else:
                    shared = None
                    if config.tree is not None:
                        if (plan_key := (scenario.id, seed, plan_parts)) not in plans:
                            found = done_by_seed[(scenario.id, seed)]
                            plans[plan_key] = _stored_plan(store, found, plan_parts)
                        shared = plans[plan_key]
                    attempt = attempts[(scenario.id, setting, seed)] + 1
                    result, used = _run_attempt(
                        scenario, setting, seed, attempt, store, config, shared
                    )
                    if used is not None:
                        plans[(scenario.id, seed, plan_parts)] = used
                    ran += 1
                    if result.outcome is None:
                        unfinished.append(unit)
                    else:
                        run_ids.append(result.run_id)
    return EvalRuns(
        run_ids=run_ids, ran=ran, skipped=skipped, gave_up=gave_up, unfinished=unfinished
    )


async def _run(
    run_id: str,
    scenario: Scenario,
    setting: Setting,
    seed: int,
    attempt: int,
    store: EventStore,
    config: _UnitConfig,
    shared: _SharedPlan | None,
) -> tuple[RunResult, _SharedPlan | None]:
    """Run one attempt; with a task planner, also return the plan it ran, for the eval to reuse.
    `shared` is a plan another run asked for: this run records it and asks no planner."""
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
    used: _SharedPlan | None = None
    if config.tree is not None:
        # Before the tree is built and before anything moves: a rejected plan ends the run here.
        if shared is None:
            planned = await _ask(config.tree, scenario, registry, recorder, store)
            used = _SharedPlan(origin=run_id, result=dataclasses.replace(planned, trace=None))
        else:
            planned, used = shared.result, shared
        reused_from = None if shared is None else shared.origin
        if planned.errors:
            recorder.emit(
                TaskPlanRejected(
                    planner=config.tree.name,
                    steps=list(planned.steps),
                    errors=list(planned.errors),
                    reused_from=reused_from,
                )
            )
            if planned.unanswered:  # a failed call is no result: the unit runs again
                store.flush()
                return RunResult(run_id=run_id, outcome=None), used
            result = _finish(run_id, store, recorder, clock, "aborted", PLAN_REJECTED)
            return result, used
        recorder.emit(
            TaskPlanned(
                planner=config.tree.name, steps=list(planned.steps), reused_from=reused_from
            )
        )
        steps = planned.steps
    root = build_tree(setting, registry, env, recovery, planner, steps)
    outcome, reason = await run_task(root, env, scenario.task)
    return _finish(run_id, store, recorder, clock, outcome, reason), used


async def _ask(
    tree: TaskPlanner,
    scenario: Scenario,
    registry: SkillRegistry,
    recorder: Recorder,
    store: EventStore,
) -> PlanResult:
    """Ask the task planner, and record the call's trace, whatever the verdict."""
    try:
        planned = await plan_task(tree, scenario.task, registry)
    except ModelCallCapReached:
        # The command's budget is spent: the log says why, the unit stays unfinished, and the
        # eval stops (#63 review F2).
        recorder.emit(TaskPlanRejected(planner=tree.name, steps=[], errors=[CAP_REACHED]))
        store.flush()
        raise
    if (trace := planned.trace) is not None:  # a model call, already stored (`plan_task`)
        recorder.emit(TaskPlanResponded(planner=tree.name, **trace.event_fields()))
    return planned


def _stored_plan(store: EventStore, run_ids: Sequence[str], plan_parts: str) -> _SharedPlan | None:
    """The plan of the first finished run in `run_ids` (store order) made under the same plan
    parts, naming the run that asked for it; None if none of them has one. A unit that already
    finished with a plan of its own (from `cellrt run`, or before schema 14) keeps it."""
    for run_id in run_ids:
        events = store.events(run_id, limit=PLAN_EVENT_LIMIT)
        started = events[0].payload if events else None
        if not isinstance(started, RunStarted) or plan_digest(started.config_parts) != plan_parts:
            continue
        for event in events[1:]:
            match event.payload:
                case TaskPlanned() as p:
                    result = PlanResult(tuple(p.steps), ())
                    return _SharedPlan(origin=p.reused_from or run_id, result=result)
                case TaskPlanRejected() as p:
                    result = PlanResult(tuple(p.steps), tuple(p.errors))
                    return _SharedPlan(origin=p.reused_from or run_id, result=result)
    return None


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
        "recovery": None if setting == "A" else recovery.as_dict(),
        **monitor_inputs(params, monitor, registry),
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


def monitor_inputs(
    params: MockParams, monitor: MonitorConfig, registry: SkillRegistry
) -> dict[str, object]:
    """The parts of a run's configuration its Monitor verdicts depend on: the world (its
    period), the Monitor's thresholds, and the skills (the profile each declares). One source
    for `config_parts` and for a Monitor replay that checks a run against them (ADR 022)."""
    return {
        "world": dataclasses.asdict(params),
        "monitor": monitor.as_dict(),
        "skills": {n: skill_config(registry.get(n)) for n in sorted(registry.names())},
    }


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


def plan_digest(parts: Mapping[str, str]) -> str:
    """One hash over the parts a task plan may depend on: all but NOT_PLAN_PARTS. Runs that
    share it can share a plan."""
    return _digest({name: part for name, part in parts.items() if name not in NOT_PLAN_PARTS})


def part_digest(value: object) -> str:
    """The hash of one part's value, as `config_parts` stores it: so a reader can check that a
    configuration it holds is the one a run used (a Monitor replay, ADR 022)."""
    return _digest(value)


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
