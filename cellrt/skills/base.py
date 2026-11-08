"""The skill contract: an async unit of work with preconditions, a timeout and a result."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from cellrt.runtime.clock import Clock
from cellrt.runtime.state import RunState
from cellrt.world.base import World


@dataclass(frozen=True)
class SkillResult:
    ok: bool
    failure: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)


ProgressFn = Callable[[float], None]


@dataclass(frozen=True)
class SkillContext:
    world: World
    clock: Clock
    period_s: float
    progress: ProgressFn
    params: Mapping[str, Any] = field(default_factory=dict)


class Skill(Protocol):
    """A skill must await `ctx.clock` to let time pass and must let `CancelledError` propagate.

    It may block only on the clock (one sleep at a time) and must not start tasks of its own:
    the main loop's lockstep relies on both (see `TaskTracker`).
    """

    @property
    def name(self) -> str: ...

    @property
    def timeout_s(self) -> float: ...

    def preconditions(self, state: RunState) -> bool: ...

    async def run(self, ctx: SkillContext) -> SkillResult: ...
