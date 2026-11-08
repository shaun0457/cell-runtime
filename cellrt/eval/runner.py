"""Run one unit of work — (scenario, setting, seed) — and record it in the event store."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import random
from collections.abc import Sequence
from dataclasses import dataclass

from cellrt.eval.faults import materialize
from cellrt.eval.scenarios import Scenario
from cellrt.executor.behaviours import ExecEnv
from cellrt.executor.recovery import RecoveryConfig
from cellrt.executor.runner import run_task
from cellrt.executor.trees import Setting, build_tree
from cellrt.monitor.classifier import MonitorConfig
from cellrt.planner.base import Planner
from cellrt.planner.rule_planner import RulePlanner
from cellrt.runtime.clock import SimClock
from cellrt.runtime.events import Outcome, RunFinished, RunStarted
from cellrt.runtime.recorder import Recorder
from cellrt.runtime.store import EventStore
from cellrt.skills.base import Skill
from cellrt.skills.mock import mock_registry
from cellrt.skills.registry import SkillRegistry
from cellrt.world.mock_world import MockParams, MockWorld


@dataclass(frozen=True)
class RunResult:
    run_id: str
    outcome: Outcome


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
) -> RunResult:
    """Run one unit. Setting C asks `planner` (the rule planner by default) about UNKNOWN."""
    if planner is not None and setting != "C":
        raise ValueError(f"a planner is used only in setting C, not {setting!r}")
    attempt = store.next_attempt(scenario.id, setting, seed)
    run_id = f"{scenario.id}/{setting}/{seed}/a{attempt}"
    return asyncio.run(
        _run(
            run_id,
            scenario,
            setting,
            seed,
            attempt,
            store,
            registry or mock_registry(),
            params or MockParams(),
            recovery or RecoveryConfig(),
            monitor or MonitorConfig(),
            (planner or RulePlanner()) if setting == "C" else None,
        )
    )


def run_eval(
    scenarios: Sequence[Scenario],
    settings: Sequence[Setting],
    seeds: int,
    store: EventStore,
) -> list[str]:
    """Run every (scenario, setting, seed) for seeds 0..seeds-1, one after another, in that
    order; return the run ids. Each unit gets a fresh attempt (resuming is a later ticket)."""
    return [
        run_unit(scenario, setting, seed, store).run_id
        for scenario in scenarios
        for setting in settings
        for seed in range(seeds)
    ]


async def _run(
    run_id: str,
    scenario: Scenario,
    setting: Setting,
    seed: int,
    attempt: int,
    store: EventStore,
    registry: SkillRegistry,
    params: MockParams,
    recovery: RecoveryConfig,
    monitor: MonitorConfig,
    planner: Planner | None,
) -> RunResult:
    clock = SimClock()
    # Keyed by scenario and seed only, never the setting: every setting faces the same world and
    # the same faults. Separate streams, so adding a fault does not change the world's noise.
    faults = materialize(scenario.faults, random.Random(f"{scenario.id}:{seed}:faults"))
    world = MockWorld(random.Random(f"{scenario.id}:{seed}"), params, faults)
    recorder = Recorder(run_id, store, clock)
    recorder.emit(
        RunStarted(
            scenario_id=scenario.id,
            setting=setting,
            seed=seed,
            attempt=attempt,
            task=scenario.task,
            config_hash=config_hash(
                scenario, setting, params, recovery, monitor, registry, planner
            ),
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
    )
    root = build_tree(setting, registry, env, recovery, planner)
    outcome, reason = await run_task(root, env, scenario.task)
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


def config_hash(
    scenario: Scenario,
    setting: Setting,
    params: MockParams,
    recovery: RecoveryConfig,
    monitor: MonitorConfig,
    registry: SkillRegistry,
    planner: Planner | None = None,
) -> str:
    """Everything that defines the run besides the seed: a changed scenario is a different run.

    Recovery parameters count only for settings that recover; the Monitor runs in every setting;
    every skill counts with all its parameters, its Monitor profile included; setting C also
    counts the planner's type and budget (not how an instance of that type is set up).
    """
    config = {
        "scenario": scenario.model_dump(mode="json"),
        "setting": setting,
        "world": dataclasses.asdict(params),
        "recovery": None if setting == "A" else recovery.as_dict(),
        "monitor": monitor.as_dict(),
        "skills": {n: _skill_config(registry.get(n)) for n in sorted(registry.names())},
        "planner": None
        if planner is None
        else {
            "type": f"{type(planner).__module__}.{type(planner).__qualname__}",
            "budget": recovery.planner_budget,
        },
    }
    digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode("utf-8")).hexdigest()
    return digest[:16]


def _skill_config(skill: Skill) -> dict[str, object]:
    """A skill's identity and parameters: all fields of a dataclass skill, else its contract."""
    if dataclasses.is_dataclass(skill) and not isinstance(skill, type):
        return {"type": type(skill).__qualname__, **dataclasses.asdict(skill)}
    return {
        "type": type(skill).__qualname__,
        "timeout_s": skill.timeout_s,
        "monitor_profile": skill.monitor_profile,
    }
