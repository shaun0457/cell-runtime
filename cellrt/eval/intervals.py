"""Intervals and the sign test for the Phase 2 tables (ADR 030). Pure, stdlib only.

- `wilson`: the Wilson score interval of a rate, at z = 1.96 (95%). Closed form, and it behaves
  at small n and near 0 or 1, where the normal interval does not.
- `sign_test`: the exact two-sided sign test. ADR 030 counts it over held-out scenes: a scene
  where B succeeded in more of its repeats than A is a win for B, one where it succeeded in
  fewer is a win for A, and ties are dropped before the test.
- `claim`: ADR 030's one pooled claim, which needs both the non-overlap of the two settings'
  intervals and the sign test's p < 0.05.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

Z_95 = 1.96  # ADR 030
ALPHA = 0.05  # ADR 030
NO_CLAIM: Final = "no detectable gain at this n"


def wilson(successes: int, n: int, z: float = Z_95) -> tuple[float, float] | None:
    """The Wilson interval (low, high) of successes / n; None for n = 0 (undefined)."""
    if n < 0 or not 0 <= successes <= n:
        raise ValueError("need 0 <= successes <= n")
    if n == 0:
        return None
    p = successes / n
    z2 = z * z
    denominator = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def sign_test(wins: int, losses: int) -> float:
    """The exact two-sided p-value of `wins` against `losses` (ties already dropped): twice
    the smaller binomial tail at p = 1/2, at most 1. With no untied pair, p is 1."""
    if wins < 0 or losses < 0:
        raise ValueError("counts must not be negative")
    n = wins + losses
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(wins, losses) + 1))
    return min(1.0, 2 * tail / 2**n)


def overlap(a: tuple[float, float] | None, b: tuple[float, float] | None) -> bool:
    """Whether two intervals share a point; an undefined interval overlaps everything."""
    if a is None or b is None:
        return True
    return a[0] <= b[1] and b[0] <= a[1]


def format_interval(interval: tuple[float, float] | None) -> str:
    """`[0.12, 0.34]`, or `-` for an undefined rate (ADR 030)."""
    return "-" if interval is None else f"[{interval[0]:.2f}, {interval[1]:.2f}]"


@dataclass(frozen=True)
class SceneTally:
    """One scene's successes and counted trials per setting. Each setting with at least one
    counted trial; the trials need not be equal, since a repeat lost to infrastructure is left
    out (ADR 026), so the scene goes to the setting with the higher success rate. With equal
    repeats that is the one that succeeded more often, ADR 030's rule."""

    scene: str
    a: int
    a_trials: int
    b: int
    b_trials: int

    def __post_init__(self) -> None:
        if not (0 <= self.a <= self.a_trials and 0 <= self.b <= self.b_trials):
            raise ValueError("need 0 <= successes <= trials")
        if self.a_trials < 1 or self.b_trials < 1:
            raise ValueError("a scene needs a counted trial in each setting")

    @property
    def b_better(self) -> bool:
        return self.b * self.a_trials > self.a * self.b_trials  # exact: no float rates

    @property
    def a_better(self) -> bool:
        return self.a * self.b_trials > self.b * self.a_trials


@dataclass(frozen=True)
class Claim:
    a: tuple[float, float] | None  # A's pooled Wilson interval
    b: tuple[float, float] | None
    b_wins: int  # scenes where B succeeded more often
    a_wins: int
    ties: int
    p: float
    verdict: Literal["B beats A", "A beats B", "no detectable gain at this n"]


def claim(
    a_successes: int, a_trials: int, b_successes: int, b_trials: int, scenes: Sequence[SceneTally]
) -> Claim:
    """ADR 030's pooled claim: one setting beats the other only when the trial-level intervals
    do not overlap and the sign test over scenes gives p < 0.05, both the same way round."""
    a, b = wilson(a_successes, a_trials), wilson(b_successes, b_trials)
    b_wins = sum(1 for s in scenes if s.b_better)
    a_wins = sum(1 for s in scenes if s.a_better)
    p = sign_test(b_wins, a_wins)
    verdict: Literal["B beats A", "A beats B", "no detectable gain at this n"] = NO_CLAIM
    if not overlap(a, b) and p < ALPHA and a is not None and b is not None:
        if b[0] > a[1] and b_wins > a_wins:
            verdict = "B beats A"
        elif a[0] > b[1] and a_wins > b_wins:
            verdict = "A beats B"
    return Claim(a, b, b_wins, a_wins, len(scenes) - b_wins - a_wins, p, verdict)
