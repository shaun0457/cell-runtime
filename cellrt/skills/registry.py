"""Skills looked up by name (planner output is validated against this later)."""

from __future__ import annotations

from collections.abc import Iterable

from cellrt.skills.base import Skill


class SkillRegistry:
    def __init__(self, skills: Iterable[Skill] = ()) -> None:
        self._skills: dict[str, Skill] = {}
        for skill in skills:
            self.register(skill)

    def register(self, skill: Skill) -> None:
        if skill.name in self._skills:
            raise ValueError(f"skill already registered: {skill.name}")
        self._skills[skill.name] = skill

    def get(self, name: str) -> Skill:
        try:
            return self._skills[name]
        except KeyError:
            raise KeyError(f"unknown skill: {name}") from None

    def names(self) -> tuple[str, ...]:
        return tuple(self._skills)

    def __contains__(self, name: object) -> bool:
        return name in self._skills
