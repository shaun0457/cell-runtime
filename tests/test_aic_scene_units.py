"""A sim unit named by its scene and repeat (ADR 027): `run_goal` takes the seed and the scene's
digest, puts the seed in the run id and the digest in the scenario part of the configuration,
and leaves every other part as it was."""

from __future__ import annotations

import pytest
from aic_fakes import FakeNode

from adapters.aic.run import run_goal
from cellrt.eval.runner import part_digest
from cellrt.runtime.events import RunStarted
from cellrt.runtime.store import EventStore
from cellrt.runtime.task import Task

TASK = Task(id="nominal_sfp_r2", time_limit=180.0)
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def _started(store: EventStore, scenario_id: str) -> RunStarted:
    (run,) = [r for r in store.runs() if r.scenario_id == scenario_id]
    payload = store.events(run.run_id)[0].payload
    assert isinstance(payload, RunStarted)
    return payload


def test_the_seed_names_the_repeat_in_the_run_id(store: EventStore) -> None:
    assert run_goal(FakeNode(task=TASK), TASK, store, scenario_id="p/nominal_sfp", seed=2)

    (run,) = store.runs()
    assert run.run_id == "p/nominal_sfp/A/2/a1"
    assert _started(store, "p/nominal_sfp").seed == 2


def test_repeats_are_units_and_a_rerun_of_one_is_its_next_attempt(store: EventStore) -> None:
    for seed in (0, 1, 1):
        run_goal(FakeNode(task=TASK), TASK, store, scenario_id="p/nominal_sfp", seed=seed)

    assert sorted(r.run_id for r in store.runs()) == [
        "p/nominal_sfp/A/0/a1",
        "p/nominal_sfp/A/1/a1",
        "p/nominal_sfp/A/1/a2",
    ]


def test_two_scenes_differ_in_the_scenario_part_only(store: EventStore) -> None:
    run_goal(FakeNode(task=TASK), TASK, store, scenario_id="p/a", scene_digest=DIGEST_A)
    run_goal(FakeNode(task=TASK), TASK, store, scenario_id="p/b", scene_digest=DIGEST_B)

    a = _started(store, "p/a").config_parts
    b = _started(store, "p/b").config_parts
    assert {k for k in a if a[k] != b[k]} == {"scenario"}


def test_without_a_scene_the_scenario_part_is_the_task_alone(store: EventStore) -> None:
    run_goal(FakeNode(task=TASK), TASK, store, scenario_id="p/none")
    run_goal(FakeNode(task=TASK), TASK, store, scenario_id="p/scene", scene_digest=DIGEST_A)

    plain = _started(store, "p/none").config_parts
    scene = _started(store, "p/scene").config_parts
    # The hash runs before #78 recorded: the task's own fields and nothing else.
    assert plain["scenario"] == part_digest(TASK.model_dump(mode="json"))
    assert scene["scenario"] == part_digest({**TASK.model_dump(mode="json"), "scene": DIGEST_A})
    assert {k for k in plain if plain[k] != scene[k]} == {"scenario"}


@pytest.mark.parametrize("seed", [-1, True, 1.0])
def test_a_seed_that_is_not_a_whole_number_from_0_is_refused(
    store: EventStore, seed: object
) -> None:
    with pytest.raises(ValueError, match="seed"):
        run_goal(FakeNode(task=TASK), TASK, store, seed=seed)  # type: ignore[arg-type]

    assert store.runs() == []


@pytest.mark.parametrize("digest", ["", "A" * 64, "a" * 63, "g" * 64])
def test_a_scene_digest_that_is_not_64_hex_digits_is_refused(
    store: EventStore, digest: str
) -> None:
    with pytest.raises(ValueError, match="scene_digest"):
        run_goal(FakeNode(task=TASK), TASK, store, scene_digest=digest)

    assert store.runs() == []
