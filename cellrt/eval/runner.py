"""Run one unit of work — (scenario, setting, seed) — and record it in the event store."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import random
from dataclasses import dataclass

from cellrt.eval.scenarios import Scenario
from cellrt.executor.behaviours import ExecEnv
from cellrt.executor.runner import run_task
from cellrt.executor.trees import Setting, build_tree
from cellrt.runtime.clock import SimClock
from cellrt.runtime.events import Outcome, RunFinished, RunStarted
from cellrt.runtime.recorder import Recorder
from cellrt.runtime.store import EventStore
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
) -> RunResult:
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
        )
    )


async def _run(
    run_id: str,
    scenario: Scenario,
    setting: Setting,
    seed: int,
    attempt: int,
    store: EventStore,
    registry: SkillRegistry,
    params: MockParams,
) -> RunResult:
    clock = SimClock()
    # Keyed by scenario and seed only: every setting faces exactly the same world.
    world = MockWorld(random.Random(f"{scenario.id}:{seed}"), params)
    recorder = Recorder(run_id, store, clock)
    recorder.emit(
        RunStarted(
            scenario_id=scenario.id,
            setting=setting,
            seed=seed,
            attempt=attempt,
            task=scenario.task,
            config_hash=config_hash(setting, params),
        )
    )
    env = ExecEnv(world=world, clock=clock, recorder=recorder, period_s=params.dt)
    root = build_tree(setting, registry, env)
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


def config_hash(setting: Setting, params: MockParams) -> str:
    config = {"setting": setting, "world": dataclasses.asdict(params)}
    digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode("utf-8")).hexdigest()
    return digest[:16]
