"""Task plans (ADR 023): a task planner names the skills a task runs, the verifier checks them
against the registry and the step contracts before the tree is built, and the run records the
plan, or its rejection and an aborted run with nothing moved."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

import pytest

from cellrt.eval.metrics import summarize_run
from cellrt.eval.runner import MAX_ATTEMPTS, run_eval, run_unit
from cellrt.eval.scenarios import Scenario
from cellrt.executor.trees import SKILL_SEQUENCE
from cellrt.planner.task_plan import (
    MAX_PLAN_STEPS,
    MAX_STEP_CHARS,
    TablePlanner,
    plan_task,
    verify_plan,
)
from cellrt.replay import replay_lines
from cellrt.runtime.events import (
    Event,
    RunFinished,
    RunStarted,
    SkillStarted,
    TaskPlanned,
    TaskPlanRejected,
)
from cellrt.runtime.store import EventStore
from cellrt.runtime.task import Task
from cellrt.skills.mock import Align, Approach, InsertScripted, Verify, mock_registry
from cellrt.skills.registry import SkillRegistry
from cellrt.trace_export import export_trace

SC = Scenario(id="sc_task", task=Task(id="insert-sc-0", cable_type="sc", plug_type="sc"))


@dataclass(frozen=True)
class Scripted:
    """A task planner that answers with fixed steps, whatever the task."""

    steps: Sequence[str]
    name: str = "scripted"

    async def plan(self, task: Task, registry: SkillRegistry) -> Iterable[str]:
        return self.steps

    def describe(self) -> Mapping[str, object]:
        return {"steps": list(self.steps)}


@dataclass(frozen=True)
class Raising:
    error: type[Exception] = RuntimeError
    name: str = "raising"

    async def plan(self, task: Task, registry: SkillRegistry) -> Iterable[str]:
        raise self.error("upstream said api_key=sk-abcdefghijklmnop and more")

    def describe(self) -> Mapping[str, object]:
        return {}


@dataclass
class Hanging:
    """Never answers: waits on an event nobody sets, and notes that it was cancelled."""

    deadline_s: float = 0.05
    name: str = "hanging"
    cancelled: bool = False

    async def plan(self, task: Task, registry: SkillRegistry) -> Iterable[str]:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return ()

    def describe(self) -> Mapping[str, object]:
        return {}


@dataclass(frozen=True)
class Endless:
    name: str = "endless"

    async def plan(self, task: Task, registry: SkillRegistry) -> Iterable[str]:
        return itertools.repeat("approach")

    def describe(self) -> Mapping[str, object]:
        return {}


@dataclass(frozen=True)
class Cancelling:
    name: str = "cancelling"

    async def plan(self, task: Task, registry: SkillRegistry) -> Iterable[str]:
        raise asyncio.CancelledError

    def describe(self) -> Mapping[str, object]:
        return {}


def _skills_started(events: Sequence[Event]) -> list[str]:
    return [e.payload.skill for e in events if isinstance(e.payload, SkillStarted)]


# --- the verifier ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "steps",
    [
        ("approach", "align", "insert_scripted", "verify"),
        ("approach", "align", "insert_act", "verify"),
    ],
)
def test_a_plan_in_contract_order_that_reaches_verified_is_valid(steps: tuple[str, ...]) -> None:
    assert verify_plan(steps, mock_registry()) == ()


@pytest.mark.parametrize(
    ("steps", "errors"),
    [
        ((), ("empty",)),
        (("approach",) * (MAX_PLAN_STEPS + 1), ("too_long",)),
        # not a skill: nothing is given, so verify's need goes unmet too
        (
            ("approach", "align", "screw_in", "verify"),
            ("step 3: unregistered", "step 4: needs_unmet"),
        ),
        # a recovery helper is registered but has no contract: it cannot be planned
        (("approach", "align", "retract", "insert_scripted", "verify"), ("step 3: no_contract",)),
        (("approach", "insert_scripted", "align", "verify"), ("step 2: needs_unmet",)),
        (("approach", "align", "insert_scripted"), ("goal_not_reached",)),
        (("approach", "align", "insert_scripted", "insert_act", "verify"), ("step 4: repeated",)),
    ],
)
def test_each_way_a_plan_can_be_wrong_has_its_code(
    steps: tuple[str, ...], errors: tuple[str, ...]
) -> None:
    assert verify_plan(steps, mock_registry()) == errors


def test_a_skill_with_a_contract_but_not_in_the_registry_is_unregistered() -> None:
    registry = SkillRegistry([Approach(), Align(), InsertScripted(), Verify()])

    errors = verify_plan(("approach", "align", "insert_act", "verify"), registry)

    assert errors == ("step 3: unregistered", "step 4: needs_unmet")


# --- runs -----------------------------------------------------------------------------------


def test_with_the_fixed_tree_a_run_records_no_plan_and_has_no_tree_part(
    store: EventStore, baseline: Scenario
) -> None:
    done = run_unit(baseline, "B", 0, store)
    events = store.events(done.run_id)

    started = events[0].payload
    assert isinstance(started, RunStarted)
    assert "tree" not in started.config_parts
    assert not any(e.type.startswith("task_plan") for e in events)
    assert _skills_started(events) == list(SKILL_SEQUENCE)


def test_a_table_plan_is_the_first_event_after_run_started_and_its_skills_run_in_order(
    store: EventStore,
) -> None:
    done = run_unit(SC, "B", 0, store, tree=TablePlanner())
    events = store.events(done.run_id)

    planned = events[1].payload
    assert isinstance(planned, TaskPlanned)
    assert (planned.planner, planned.steps) == (
        "table",
        ["approach", "align", "insert_act", "verify"],
    )
    assert _skills_started(events) == planned.steps
    assert done.outcome == "success"
    started = events[0].payload
    assert isinstance(started, RunStarted) and "tree" in started.config_parts


def test_the_table_keeps_the_fixed_steps_for_a_plug_it_has_no_row_for(
    store: EventStore, baseline: Scenario
) -> None:
    done = run_unit(baseline, "A", 0, store, tree=TablePlanner())

    planned = store.events(done.run_id)[1].payload
    assert isinstance(planned, TaskPlanned) and planned.steps == list(SKILL_SEQUENCE)


def test_two_tables_with_different_rows_are_different_configurations(store: EventStore) -> None:
    one = run_unit(SC, "A", 0, store, tree=TablePlanner())
    other = run_unit(SC, "A", 0, store, tree=TablePlanner({"sc": "insert_scripted"}))

    parts = [store.events(r.run_id)[0].payload for r in (one, other)]
    assert all(isinstance(p, RunStarted) for p in parts)
    assert parts[0].config_parts["tree"] != parts[1].config_parts["tree"]  # type: ignore[union-attr]


def test_an_eval_with_a_task_planner_resumes_under_it_and_reruns_without_it(
    store: EventStore, baseline: Scenario
) -> None:
    first = run_eval([baseline], ["A"], 2, store, tree=TablePlanner())
    again = run_eval([baseline], ["A"], 2, store, tree=TablePlanner())
    fixed = run_eval([baseline], ["A"], 2, store)

    assert (first.ran, again.ran, again.skipped, fixed.ran) == (2, 0, 2, 2)


@pytest.mark.parametrize("setting", ["A", "B", "C"])
def test_an_invalid_plan_ends_the_run_aborted_before_any_skill_starts(
    store: EventStore, baseline: Scenario, setting: str
) -> None:
    tree = Scripted(("approach", "insert_scripted", "align", "verify"))

    done = run_unit(baseline, setting, 0, store, tree=tree)  # type: ignore[arg-type]
    events = store.events(done.run_id)

    assert [e.type for e in events] == ["run_started", "task_plan_rejected", "run_finished"]
    rejected, finished = events[1].payload, events[2].payload
    assert isinstance(rejected, TaskPlanRejected)
    assert (rejected.planner, rejected.errors) == ("scripted", ["step 2: needs_unmet"])
    assert rejected.steps == list(tree.steps)
    assert isinstance(finished, RunFinished)
    assert (finished.outcome, finished.reason, finished.steps, finished.duration) == (
        "aborted",
        "task plan rejected",
        0,
        0.0,
    )
    assert done.outcome == "aborted"
    assert summarize_run(events) is not None


def test_a_task_planner_that_raises_leaves_the_unit_unfinished_keeping_only_the_error_type(
    store: EventStore, baseline: Scenario
) -> None:
    # No answer is not a rejected plan: the unit has no result, so no metric counts it, and an
    # eval runs it again (#63 review F2).
    done = run_unit(baseline, "B", 0, store, tree=Raising())
    events = store.events(done.run_id)

    assert [e.type for e in events] == ["run_started", "task_plan_rejected"]
    rejected = events[1].payload
    assert isinstance(rejected, TaskPlanRejected)
    assert (rejected.errors, rejected.steps) == (["planner_error: RuntimeError"], [])
    assert done.outcome is None
    assert summarize_run(events) is None
    assert not any("sk-abc" in e.model_dump_json() for e in events)


def test_a_rejected_plan_is_stored_cut_and_scrubbed(store: EventStore, baseline: Scenario) -> None:
    tree = Scripted(("token=" + "s3cr3t" * 10, *("x" * 500,) * 50))

    done = run_unit(baseline, "A", 0, store, tree=tree)

    rejected = store.events(done.run_id)[1].payload
    assert isinstance(rejected, TaskPlanRejected) and rejected.errors == ["too_long"]
    assert len(rejected.steps) == MAX_PLAN_STEPS + 1
    assert all(len(s) <= MAX_STEP_CHARS for s in rejected.steps)
    assert "s3cr3t" not in rejected.steps[0]


def test_replay_and_trace_show_the_plan_and_the_rejection(
    store: EventStore, baseline: Scenario
) -> None:
    ok = store.events(run_unit(SC, "B", 0, store, tree=TablePlanner()).run_id)
    bad = store.events(run_unit(baseline, "B", 0, store, tree=Scripted(("verify",))).run_id)

    assert "table: approach -> align -> insert_act -> verify" in list(replay_lines(ok))[1]
    assert "scripted rejected: step 1: needs_unmet" in list(replay_lines(bad))[1]
    for events, shown, not_shown in ((ok, "task_planned", "task_plan_rejected"),
                                     (bad, "task_plan_rejected", "task_planned")):  # fmt: skip
        names = {e.get("name") for e in export_trace(events)["traceEvents"]}
        assert shown in names and not_shown not in names


def test_a_step_that_is_not_a_name_is_a_rejection_that_keeps_only_its_type(
    store: EventStore, baseline: Scenario
) -> None:
    tree = Scripted((["approach"], None, "align"))  # type: ignore[arg-type]

    done = run_unit(baseline, "B", 0, store, tree=tree)

    rejected = store.events(done.run_id)[1].payload
    assert isinstance(rejected, TaskPlanRejected)
    assert rejected.errors == ["step 1: not_a_name", "step 2: not_a_name"]
    assert rejected.steps == ["<list>", "<NoneType>", "align"]
    assert done.outcome == "aborted"


def test_a_task_planner_that_never_answers_is_cancelled_at_its_deadline(
    store: EventStore, baseline: Scenario
) -> None:
    tree = Hanging(deadline_s=0.05)

    done = run_unit(baseline, "B", 0, store, tree=tree)

    events = store.events(done.run_id)
    rejected = events[1].payload
    assert isinstance(rejected, TaskPlanRejected) and rejected.errors == ["planner_timed_out"]
    assert tree.cancelled
    assert [e.type for e in events] == ["run_started", "task_plan_rejected"]
    assert done.outcome is None  # unfinished: an eval runs it again


def test_a_timeout_raised_inside_the_planner_is_its_error_not_the_deadline(
    store: EventStore, baseline: Scenario
) -> None:
    done = run_unit(baseline, "B", 0, store, tree=Raising(TimeoutError))

    rejected = store.events(done.run_id)[1].payload
    assert isinstance(rejected, TaskPlanRejected)
    assert rejected.errors == ["planner_error: TimeoutError"]


@dataclass
class Flaky:
    """Raises on its first `failures` calls, then answers with the fixed steps."""

    failures: int
    name: str = "flaky"
    calls: int = 0

    async def plan(self, task: Task, registry: SkillRegistry) -> Iterable[str]:
        self.calls += 1
        if self.calls <= self.failures:
            raise ConnectionError("upstream reset")
        return SKILL_SEQUENCE

    def describe(self) -> Mapping[str, object]:
        return {}


def test_an_eval_runs_a_unit_whose_plan_call_failed_again_when_resumed(
    store: EventStore, baseline: Scenario
) -> None:
    tree = Flaky(failures=1)

    first = run_eval([baseline], ["A"], 1, store, tree=tree)
    again = run_eval([baseline], ["A"], 1, store, tree=tree)

    assert (first.run_ids, first.unfinished, first.ran, first.skipped) == (
        [],
        ["baseline/A/0"],
        1,
        0,
    )
    assert (again.ran, again.skipped, again.unfinished) == (1, 0, [])
    [run_id] = again.run_ids
    assert run_id.endswith("/a2") and summarize_run(store.events(run_id)) is not None


def test_a_unit_whose_plan_call_keeps_failing_is_given_up_after_max_attempts(
    store: EventStore, baseline: Scenario
) -> None:
    tree = Flaky(failures=MAX_ATTEMPTS + 1)

    evals = [run_eval([baseline], ["A"], 1, store, tree=tree) for _ in range(MAX_ATTEMPTS + 1)]

    assert [e.unfinished for e in evals[:MAX_ATTEMPTS]] == [["baseline/A/0"]] * MAX_ATTEMPTS
    assert (evals[-1].ran, evals[-1].gave_up) == (0, ["baseline/A/0"])
    assert tree.calls == MAX_ATTEMPTS


@pytest.mark.parametrize("deadline", [0, -1.0, 120.5, float("nan")])
def test_a_bad_task_planner_deadline_is_refused_before_a_run_is_recorded(
    store: EventStore, baseline: Scenario, deadline: float
) -> None:
    with pytest.raises(ValueError, match="deadline_s"):
        run_unit(baseline, "A", 0, store, tree=Hanging(deadline_s=deadline))

    assert list(store.runs()) == []


def test_an_endless_answer_is_read_only_up_to_one_step_past_the_limit(
    store: EventStore, baseline: Scenario
) -> None:
    done = run_unit(baseline, "A", 0, store, tree=Endless())

    rejected = store.events(done.run_id)[1].payload
    assert isinstance(rejected, TaskPlanRejected) and rejected.errors == ["too_long"]
    assert rejected.steps == ["approach"] * (MAX_PLAN_STEPS + 1)


def test_cancelling_the_run_while_it_plans_is_not_turned_into_a_rejection() -> None:
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(plan_task(Cancelling(), Task(), mock_registry()))


def test_a_rejected_unit_is_finished_and_skipped_when_the_eval_resumes(
    store: EventStore, baseline: Scenario
) -> None:
    tree = Scripted(("verify",))

    first = run_eval([baseline], ["A", "B"], 2, store, tree=tree)
    again = run_eval([baseline], ["A", "B"], 2, store, tree=tree)

    assert (first.ran, again.ran, again.skipped, again.gave_up) == (4, 0, 4, [])
