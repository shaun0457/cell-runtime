"""Task plans (ADR 023): which registered skills a task runs, in what order, checked before the
tree is built.

A plan is a flat list of skill names; the tree wraps each step in the setting's recovery, so a
plan says what the task needs, never how a failure is recovered. Each plannable skill has a step
contract: the one fact it needs before it runs and the one fact it gives. A plan is valid when
every name is registered and has a contract, each step's need was given by an earlier step, no
fact is given twice, and the goal (`verified`) is given. The check knows the order a nominal
insertion takes, not whether a plan will work.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from itertools import islice
from types import MappingProxyType
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cellrt.planner.base import PlannerTrace, planner_deadline
from cellrt.planner.nim import ModelCallCapReached
from cellrt.runtime.scrub import scrub
from cellrt.runtime.task import Task
from cellrt.skills.registry import SkillRegistry

MAX_PLAN_STEPS = 8
# A rejected plan keeps at most MAX_PLAN_STEPS + 1 names (one more shows it was too long), each
# scrubbed and cut to this many characters.
MAX_STEP_CHARS = 64
# A model's answer longer than this is refused unparsed (eight names fit in far less).
MAX_ANSWER_CHARS = 4_000
# At most this many parse codes are kept for one answer.
MAX_PARSE_CODES = 10
GOAL = "verified"
# The steps every task runs when no task planner is given (`--tree fixed`).
FIXED_STEPS = ("approach", "align", "insert_scripted", "verify")


@dataclass(frozen=True)
class StepContract:
    needs: str | None  # None: the step can run first
    gives: str


# Keyed by skill name, so neither the `Skill` protocol nor any skill changes; the mock and the
# AIC skills share these names. A registered skill without a row (a recovery helper such as
# `retract`) cannot be planned.
STEP_CONTRACTS: Mapping[str, StepContract] = MappingProxyType(
    {
        "approach": StepContract(None, "at_port"),
        "align": StepContract("at_port", "aligned"),
        "insert_scripted": StepContract("aligned", "inserted"),
        "insert_act": StepContract("aligned", "inserted"),
        "verify": StepContract("inserted", GOAL),
    }
)


class TaskPlanner(Protocol):
    """Turns a task into a plan, once per run, before the tree is built and before any motion.

    `plan` may return any iterable of names; at most MAX_PLAN_STEPS + 1 are read, and anything
    that is not a string is refused. It is waited on for at most `deadline_s` of wall time if
    the planner has that attribute, else for DEFAULT_PLANNER_DEADLINE_S, as for the UNKNOWN
    planner (ADR 016). `describe()` and the deadline count in the run's `tree` config part.
    """

    @property
    def name(self) -> str: ...  # recorded with the plan

    async def plan(self, task: Task, registry: SkillRegistry) -> Iterable[str] | TracedPlan: ...

    def describe(self) -> Mapping[str, object]: ...


class TaskPlan(BaseModel):
    """A model's answer, parsed strictly: 1 to MAX_PLAN_STEPS names and nothing else."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    steps: list[str] = Field(min_length=1, max_length=MAX_PLAN_STEPS)


@dataclass(frozen=True)
class TracedPlan:
    """A model task planner's raw answer (JSON text, or a structure a client already parsed)
    with the trace of the call that produced it. Parsed into `TaskPlan`, then checked."""

    raw: object
    trace: PlannerTrace


@dataclass(frozen=True)
class PlanResult:
    steps: tuple[str, ...]  # the plan if valid; else what was proposed, scrubbed and cut
    errors: tuple[str, ...]  # empty if the plan is valid
    # The model call behind the answer, if there was one, as the log keeps it (scrubbed, cut).
    trace: PlannerTrace | None = None


def verify_plan(steps: tuple[str, ...], registry: SkillRegistry) -> tuple[str, ...]:
    """The plan's error codes, in step order (empty if it is valid). A code names a step by its
    position, never by the text a planner wrote."""
    if not steps:
        return ("empty",)
    if len(steps) > MAX_PLAN_STEPS:
        return ("too_long",)
    errors: list[str] = []
    given: set[str] = set()
    for i, name in enumerate(steps, start=1):
        if name not in registry:
            errors.append(f"step {i}: unregistered")
            continue
        contract = STEP_CONTRACTS.get(name)
        if contract is None:
            errors.append(f"step {i}: no_contract")
            continue
        if contract.needs is not None and contract.needs not in given:
            errors.append(f"step {i}: needs_unmet")
        if contract.gives in given:
            errors.append(f"step {i}: repeated")
        # Given even when the need was unmet, so one misplaced step is reported once.
        given.add(contract.gives)
    if GOAL not in given:
        errors.append("goal_not_reached")
    return tuple(errors)


async def plan_task(tree: TaskPlanner, task: Task, registry: SkillRegistry) -> PlanResult:
    """Ask the task planner, within its deadline, and check its answer. A planner that raises
    gives a rejection with the exception's type only: its message may carry upstream text. A
    model planner's answer (`TracedPlan`) is parsed into `TaskPlan` first, and its trace is
    kept whatever the verdict. A cancellation of the run itself is not caught."""
    # Wall time by design: nothing has moved yet, and the sim clock does not run (ADR 016).
    deadline = asyncio.timeout(planner_deadline(tree))  # noqa: TID251
    trace: PlannerTrace | None = None
    proposed: tuple[object, ...] | ParseRefused
    try:
        async with deadline:
            answer = await tree.plan(task, registry)
            if isinstance(answer, TracedPlan):
                if not isinstance(answer.trace, PlannerTrace):  # planner code: never echo it
                    return PlanResult((), ("trace_invalid",))
                trace, proposed = answer.trace.stored(), parse_plan(answer.raw)
            else:
                proposed = tuple(islice(answer, MAX_PLAN_STEPS + 1))
    except TimeoutError:
        if deadline.expired():
            return PlanResult((), ("planner_timed_out",))
        return PlanResult((), ("planner_error: TimeoutError",))
    except ModelCallCapReached:
        return PlanResult((), ("cap_reached",))
    except Exception as exc:  # an invalid answer is an event of the run, not a crash
        return PlanResult((), (f"planner_error: {type(exc).__name__}",))
    if isinstance(proposed, ParseRefused):
        return PlanResult((), proposed.codes, trace)
    if not_names := [i for i, s in enumerate(proposed, start=1) if not isinstance(s, str)]:
        codes = tuple(f"step {i}: not_a_name" for i in not_names)
        return PlanResult(_shown(proposed), codes, trace)
    steps = tuple(s for s in proposed if isinstance(s, str))
    if errors := verify_plan(steps, registry):
        return PlanResult(_shown(steps), errors, trace)
    return PlanResult(steps, (), trace)


@dataclass(frozen=True)
class ParseRefused:
    codes: tuple[str, ...]


def parse_plan(raw: object) -> tuple[str, ...] | ParseRefused:
    """A model's answer as steps, or the codes that refuse it: where (a field of `TaskPlan`, a
    list index, or `<key>` for a key the model made up, so its text never reaches a code) and
    the error type. JSON text over MAX_ANSWER_CHARS is refused unread."""
    try:
        if isinstance(raw, str):
            if len(raw) > MAX_ANSWER_CHARS:
                return ParseRefused(("answer: too_large",))
            plan = TaskPlan.model_validate_json(raw)
        else:
            plan = TaskPlan.model_validate(raw)
    except ValidationError as exc:
        found = exc.errors(include_url=False, include_input=False, include_context=False)
        return ParseRefused(
            tuple(f"{_where(e['loc'])}: {e['type']}" for e in found[:MAX_PARSE_CODES])
        )
    return tuple(plan.steps)


def _where(loc: tuple[int | str, ...]) -> str:
    if not loc:
        return "answer"
    parts = [str(p) if isinstance(p, int) or p in TaskPlan.model_fields else "<key>" for p in loc]
    return ".".join(parts)


def _shown(proposed: tuple[object, ...]) -> tuple[str, ...]:
    """What a rejected plan keeps: each name scrubbed and cut, anything else by its type."""
    return tuple(
        scrub(s if isinstance(s, str) else f"<{type(s).__name__}>")[:MAX_STEP_CHARS]
        for s in proposed
    )


def _default_rows() -> dict[str, str]:
    # Ours, after #28's SC plug jamming at the port mouth (ADR 023): whether insert_act frees it
    # in the sim is not known.
    return {"sc": "insert_act"}


@dataclass(frozen=True)
class TablePlanner:
    """The non-model baseline: the fixed steps, with the insert skill looked up by plug type, as
    a recipe table picks a program by product variant. A plug type without a row keeps
    `insert_scripted`."""

    insert_for: Mapping[str, str] = field(default_factory=_default_rows)

    def __post_init__(self) -> None:
        # Frozen all the way down, so the hash cannot drift after a run started.
        object.__setattr__(self, "insert_for", MappingProxyType(dict(self.insert_for)))

    @property
    def name(self) -> str:
        return "table"

    async def plan(self, task: Task, registry: SkillRegistry) -> Iterable[str]:
        insert = self.insert_for.get(task.plug_type, "insert_scripted")
        return tuple(insert if step == "insert_scripted" else step for step in FIXED_STEPS)

    def describe(self) -> Mapping[str, object]:
        return {"insert_for": dict(sorted(self.insert_for.items()))}
