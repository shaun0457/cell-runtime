"""One behaviour tree per evaluation setting."""

from __future__ import annotations

from typing import Literal

import py_trees

from cellrt.executor.behaviours import ExecEnv, SkillBehaviour
from cellrt.executor.recovery import RecoveryConfig, build_recovering_steps
from cellrt.planner.base import Planner
from cellrt.planner.rule_planner import RulePlanner
from cellrt.skills.registry import SkillRegistry

Setting = Literal["A", "B", "C"]
SETTINGS: tuple[Setting, ...] = ("A", "B", "C")
SKILL_SEQUENCE = ("approach", "align", "insert_scripted", "verify")


def build_tree(
    setting: Setting,
    registry: SkillRegistry,
    env: ExecEnv,
    recovery: RecoveryConfig | None = None,
    planner: Planner | None = None,
) -> py_trees.behaviour.Behaviour:
    if setting == "A":
        # No recovery: the first failure fails the sequence and ends the task.
        children: list[py_trees.behaviour.Behaviour] = [
            SkillBehaviour(registry.get(name), env) for name in SKILL_SEQUENCE
        ]
    elif setting == "B":
        # Known failure classes are recovered within budgets; anything else fails the task.
        children = list(
            build_recovering_steps(SKILL_SEQUENCE, registry, env, recovery or RecoveryConfig())
        )
    elif setting == "C":
        # B, plus a planner for what the Monitor cannot classify (UNKNOWN) in an insert step.
        children = list(
            build_recovering_steps(
                SKILL_SEQUENCE,
                registry,
                env,
                recovery or RecoveryConfig(),
                planner or RulePlanner(),
            )
        )
    else:
        raise ValueError(f"unknown setting: {setting!r} (known: {', '.join(SETTINGS)})")
    return py_trees.composites.Sequence(name=f"setting_{setting}", memory=True, children=children)
