"""One behaviour tree per evaluation setting."""

from __future__ import annotations

from typing import Literal

import py_trees

from cellrt.executor.behaviours import ExecEnv, SkillBehaviour
from cellrt.skills.registry import SkillRegistry

Setting = Literal["A"]
SETTINGS: tuple[Setting, ...] = ("A",)
SKILL_SEQUENCE = ("approach", "align", "insert_scripted", "verify")


def build_tree(
    setting: Setting, registry: SkillRegistry, env: ExecEnv
) -> py_trees.behaviour.Behaviour:
    if setting != "A":
        raise ValueError(f"unknown setting: {setting!r} (known: {', '.join(SETTINGS)})")
    # Setting A: no recovery. The first failure fails the sequence and ends the task.
    return py_trees.composites.Sequence(
        name="setting_A",
        memory=True,
        children=[SkillBehaviour(registry.get(name), env) for name in SKILL_SEQUENCE],
    )
