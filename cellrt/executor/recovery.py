"""Settings B and C: the behaviour tree recovers failures within budgets.

A `Recovering` node runs one step of the task. When a skill in it fails with a failure class
that has a recovery plan, and both the class budget and the task budget allow it, the node runs
the plan's skills and then the failed skill again. Budgets are shared by the whole task.

In setting C an insert step also has a planner: a failure the Monitor classified as UNKNOWN goes
to it (within the planner budget and the task budget), and its validated decision runs: switch
to another insert skill, retry the failed one, or abort.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import assert_never

import py_trees
from py_trees.common import Status

from cellrt.executor.behaviours import ExecEnv, PlannerBehaviour, SkillBehaviour
from cellrt.monitor.classifier import FailureClass
from cellrt.planner.base import FailureContext, Planner
from cellrt.planner.schemas import Abort, Retry, SwitchSkill
from cellrt.runtime.events import RecoveryStarted
from cellrt.runtime.recorder import Recorder
from cellrt.skills.registry import SkillRegistry


@dataclass(frozen=True)
class Plan:
    action: str
    before: tuple[str, ...]  # skills run before the failed skill is run again
    slow: bool = False  # run the step's skill again at reduced speed


def _default_plans() -> dict[str, Plan]:
    return {
        FailureClass.POSE_ERROR.value: Plan("realign", ("align",)),
        FailureClass.INSERT_STUCK.value: Plan("retract_realign", ("retract", "align")),
        FailureClass.FORCE_LIMIT.value: Plan("retract_slow", ("retract",), slow=True),
        FailureClass.SENSOR_DROPOUT.value: Plan("wait_sensor", ("wait_sensor",)),
        FailureClass.TIMEOUT.value: Plan("retry", ()),
    }


def _default_budgets() -> dict[str, int]:
    return {
        FailureClass.POSE_ERROR.value: 2,
        FailureClass.INSERT_STUCK.value: 2,
        FailureClass.FORCE_LIMIT.value: 1,
        FailureClass.SENSOR_DROPOUT.value: 1,
        FailureClass.TIMEOUT.value: 1,
    }


# Limits that keep the helper skills inside their own timeouts (wait_sensor 12 s, retract 4 s).
MAX_SENSOR_WAIT_S = 10.0
MAX_RETRACT_TO_MM = 20.0


@dataclass(frozen=True)
class RecoveryConfig:
    """Every recovery parameter, plans included; hashed into `run_started.config_hash`."""

    plans: Mapping[str, Plan] = field(default_factory=_default_plans)
    budgets: Mapping[str, int] = field(default_factory=_default_budgets)
    total_budget: int = 4
    slow_speed_scale: float = 0.5
    retract_to_mm: float = 5.0  # height above the port to retract to
    sensor_wait_s: float = 2.0  # longest wait for the sensor to come back
    sensor_valid_samples: int = 3  # valid samples in a row that count as "back"
    planner_budget: int = 2  # planner calls per task (setting C)

    def __post_init__(self) -> None:
        known = {c.value for c in FailureClass}
        if unknown := sorted((set(self.plans) | set(self.budgets)) - known):
            raise ValueError(f"unknown failure classes: {', '.join(unknown)}")
        if (
            self.total_budget < 0
            or self.planner_budget < 0
            or any(n < 0 for n in self.budgets.values())
        ):
            raise ValueError("budgets must not be negative")
        if not 0 < self.slow_speed_scale <= 1:
            raise ValueError("slow_speed_scale must be in (0, 1]")
        if not 0 < self.retract_to_mm <= MAX_RETRACT_TO_MM:
            raise ValueError(f"retract_to_mm must be in (0, {MAX_RETRACT_TO_MM}]")
        if not 0 < self.sensor_wait_s <= MAX_SENSOR_WAIT_S:
            raise ValueError(f"sensor_wait_s must be in (0, {MAX_SENSOR_WAIT_S}]")
        if self.sensor_valid_samples < 1:
            raise ValueError("sensor_valid_samples must be at least 1")
        # Frozen all the way down, so the hash cannot drift after a run started.
        object.__setattr__(self, "plans", MappingProxyType(dict(self.plans)))
        object.__setattr__(self, "budgets", MappingProxyType(dict(self.budgets)))

    def as_dict(self) -> dict[str, object]:
        """Every parameter but the planner budget, which counts only where there is a planner."""
        data: dict[str, object] = {
            f.name: getattr(self, f.name)
            for f in dataclasses.fields(self)
            if f.name != "planner_budget"
        }
        data["plans"] = {k: dataclasses.asdict(p) for k, p in sorted(self.plans.items())}
        data["budgets"] = dict(sorted(self.budgets.items()))
        return data


class RecoveryBudget:
    """The task's recovery budgets, checked against the recoveries in the folded run state.

    The counts are not kept here: they are a fold over `recovery_started` events, so they can be
    rebuilt from the log at any event (`state_at`).
    """

    def __init__(self, config: RecoveryConfig, recorder: Recorder) -> None:
        self.config = config
        self._recorder = recorder

    def why_not(self, failure_class: str) -> str | None:
        """Why no recovery is possible for `failure_class`, or None if one is allowed."""
        state = self._recorder.state
        if refusal := self.why_not_total():
            return refusal
        used = state.recoveries_by_class.get(failure_class, 0)
        if used >= self.config.budgets.get(failure_class, 0):
            return f"{failure_class} recovery budget used up"
        return None

    def why_not_total(self) -> str | None:
        """Why the task allows no more recoveries of any class, or None."""
        if self._recorder.state.recoveries_total >= self.config.total_budget:
            return "task recovery budget used up"
        return None

    def why_not_planner(self) -> str | None:
        """Why the planner may not be called again in this task, or None."""
        if self._recorder.state.planner_calls >= self.config.planner_budget:
            return "planner budget used up"
        return self.why_not_total()

    def next_counts(self, failure_class: str) -> tuple[int, int]:
        """The per-class and total count the next recovery of `failure_class` will have."""
        state = self._recorder.state
        used = state.recoveries_by_class.get(failure_class, 0)
        return used + 1, state.recoveries_total + 1


class Recovering(py_trees.composites.Composite):
    """One task step with recovery. Children: the step's skill, the plans' skills, and in
    setting C the planner and the skills it may switch to."""

    def __init__(
        self,
        primary: SkillBehaviour,
        slow: SkillBehaviour,
        helpers: Mapping[str, SkillBehaviour],
        budget: RecoveryBudget,
        env: ExecEnv,
        planner: PlannerBehaviour | None = None,
        alternatives: Mapping[str, tuple[SkillBehaviour, SkillBehaviour]] | None = None,
    ) -> None:
        """`alternatives`: per skill a planner may switch to, its node and its slow variant."""
        alternatives = dict(alternatives or {})
        switched = [node for pair in alternatives.values() for node in pair]
        children = [primary, slow, *helpers.values(), *switched]
        super().__init__(f"recover_{primary.name}", [*children, *([planner] if planner else [])])
        self._primary = primary
        self._slow = slow
        self._helpers = helpers
        self._budget = budget
        self._env = env
        self._planner = planner
        # The skills a planner may switch to, by name (the step's own skill included).
        self._options = {primary.skill.name: primary, **{n: p[0] for n, p in alternatives.items()}}
        # Which node a slow plan reruns, for each insert node of the step.
        self._slow_for = {primary: slow, slow: slow}
        for node, slow_node in alternatives.values():
            self._slow_for |= {node: slow_node, slow_node: slow_node}
        self._queue: list[py_trees.behaviour.Behaviour] = []
        self._consulted: SkillBehaviour | None = None  # the failure the planner is asked about
        self.failure_reason: str | None = None
        self.aborted = False  # the planner aborted, or its output was rejected

    def tick(self) -> Iterator[py_trees.behaviour.Behaviour]:
        if self.status != Status.RUNNING:
            for child in self.children:
                if child.status != Status.INVALID:
                    child.stop(Status.INVALID)
            self._queue = [self._primary]
            self._consulted = None
            self.failure_reason = None
            self.aborted = False
            self.initialise()
        while self._queue:
            node = self.current_child = self._queue[0]
            yield from node.tick()
            if node.status == Status.RUNNING:
                self.status = Status.RUNNING
                yield self
                return
            if node is self._planner:
                if not self._follow(self._planner):
                    self.stop(Status.FAILURE)
                    yield self
                    return
                continue
            if node.status == Status.SUCCESS:
                self._queue.pop(0)
                continue
            assert isinstance(node, SkillBehaviour)
            plan = self._plan_for(node)
            if plan is None:
                if self._consult(node):
                    continue
                self.stop(Status.FAILURE)
                yield self
                return
            again = self._slow_for.get(node, node) if plan.slow else node
            self._queue = [*(self._helpers[name] for name in plan.before), again, *self._queue[1:]]
        self.stop(Status.SUCCESS)
        yield self

    def _consult(self, node: SkillBehaviour) -> bool:
        """Hand a failure to the planner: only in setting C, only an UNKNOWN from the Monitor
        (never a bug, never a class a skill reported itself), and only within budgets."""
        if (
            self._planner is None
            or node.error
            or node.failure != FailureClass.UNKNOWN.value
            or node.failure_source != "monitor"
            or node.verdict is None
        ):
            return False
        skill = node.skill.name
        if refusal := self._budget.why_not_planner():
            self.failure_reason = f"{skill} failed: UNKNOWN ({refusal})"
            return False
        state = self._env.recorder.state
        config = self._budget.config
        self._planner.ask(
            FailureContext(
                skill=skill,
                attempt=node.attempt,
                failure_class=node.failure,
                rule=node.verdict.rule,
                evidence=node.verdict.evidence,
                time_left_s=max(0.0, self._env.time_limit_s - self._env.clock.now()),
                planner_calls_left=config.planner_budget - state.planner_calls,
                recoveries_left=config.total_budget - state.recoveries_total,
                catalogue=self._planner.catalogue(),
            )
        )
        self._consulted = node
        self._queue = [self._planner, *self._queue[1:]]
        return True

    def _follow(self, planner: PlannerBehaviour) -> bool:
        """Carry out the planner's decision; False (with a reason) if the step fails."""
        failed = self._consulted
        assert failed is not None
        skill = failed.skill.name
        decision = planner.decision
        if planner.status != Status.SUCCESS or decision is None:
            self.aborted = True
            self.failure_reason = f"{skill} failed: UNKNOWN (planner output rejected)"
            return False
        match decision:
            case Abort(reason=reason):
                self.aborted = True
                self.failure_reason = f"{skill} failed: UNKNOWN (planner aborted: {reason})"
                return False
            case Retry():
                again, action = failed, "retry"
            case SwitchSkill(skill=name):
                again, action = self._options[name], f"switch_skill:{name}"
            case _:
                assert_never(decision)
        unknown = FailureClass.UNKNOWN.value
        count_for_class, count_total = self._budget.next_counts(unknown)
        self._env.recorder.emit(
            RecoveryStarted(
                skill=skill,
                failure_class=unknown,
                action=action,
                count_for_class=count_for_class,
                count_total=count_total,
                source="monitor",
            )
        )
        self._queue = [again, *self._queue[1:]]
        return True

    def _plan_for(self, node: SkillBehaviour) -> Plan | None:
        """The recovery plan for `node`'s failure, or None (with a failure reason) if none."""
        failure = node.failure
        skill = node.skill.name
        if node.error:  # a bug in the skill: never recovered, the run is aborted
            self.failure_reason = f"{skill} raised {node.error}"
            return None
        plan = self._budget.config.plans.get(failure) if failure else None
        if failure is None or plan is None:
            why = failure or node.note
            self.failure_reason = f"{skill} failed: {why}" if why else f"{skill} failed"
            return None
        if refusal := self._budget.why_not(failure):
            self.failure_reason = f"{skill} failed: {failure} ({refusal})"
            return None
        # Every path that sets a failure class also sets its source.
        assert node.failure_source is not None
        count_for_class, count_total = self._budget.next_counts(failure)
        self._env.recorder.emit(
            RecoveryStarted(
                skill=skill,
                failure_class=failure,
                action=plan.action,
                count_for_class=count_for_class,
                count_total=count_total,
                source=node.failure_source,
            )
        )
        return plan


def build_recovering_steps(
    skills: tuple[str, ...],
    registry: SkillRegistry,
    env: ExecEnv,
    config: RecoveryConfig,
    planner: Planner | None = None,
) -> list[Recovering]:
    """One `Recovering` per step. With a planner (setting C), each insert step also gets a
    planner node and the other registered insert skills as the options it may switch to."""
    budget = RecoveryBudget(config, env.recorder)
    helper_params = {
        "retract": {"to_height_mm": config.retract_to_mm},
        "wait_sensor": {
            "max_wait_s": config.sensor_wait_s,
            "valid_samples": config.sensor_valid_samples,
        },
    }
    helper_names = sorted({name for plan in config.plans.values() for name in plan.before})
    return [
        Recovering(
            SkillBehaviour(registry.get(name), env),
            SkillBehaviour(
                registry.get(name),
                env,
                params={"speed_scale": config.slow_speed_scale},
                name=f"{name}@slow",
            ),
            {
                h: SkillBehaviour(
                    registry.get(h), env, params=helper_params.get(h), name=f"{h}@{name}"
                )
                for h in helper_names
            },
            budget,
            env,
            *_planner_for(name, registry, env, planner, config.slow_speed_scale),
        )
        for name in skills
    ]


def _planner_for(
    step: str,
    registry: SkillRegistry,
    env: ExecEnv,
    planner: Planner | None,
    slow_scale: float,
) -> tuple[PlannerBehaviour | None, dict[str, tuple[SkillBehaviour, SkillBehaviour]]]:
    """The planner node and the switch options for a step (none unless it is an insert step)."""
    if planner is None or registry.get(step).monitor_profile != "insertion":
        return None, {}
    others = [
        n
        for n in sorted(registry.names())
        if n != step and registry.get(n).monitor_profile == "insertion"
    ]
    alternatives = {
        n: (
            SkillBehaviour(registry.get(n), env, name=f"{n}@{step}"),
            SkillBehaviour(
                registry.get(n), env, params={"speed_scale": slow_scale}, name=f"{n}@{step}@slow"
            ),
        )
        for n in others
    }
    node = PlannerBehaviour(
        planner, registry, env, frozenset({step, *others}), name=f"planner@{step}"
    )
    return node, alternatives
